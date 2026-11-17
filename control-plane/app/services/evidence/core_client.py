# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Client for the core's evidence API

Sealing, tree heads, verification, bundles, backfill and key rotation all
live in the Rust core, which holds the key material and the vectors — the
control plane never imports `cryptography` for any of it. The CP asks the
core over the internal URL (`proxy_internal_url()`), authenticated as the
system caller with the `BRUTOR_GATEWAY_SERVICE_API_KEY` bearer, exactly as
`proxy_signing.sign_manifest` does for manifests.

Every function here **never raises**. On failure it returns
`{"error": <code>, "detail": <bounded string>}`:

| code                   | when                                             |
|------------------------|--------------------------------------------------|
| `evidence_disabled`    | core answered 503 (no signing key configured)    |
| `tenant_key_unavailable` | core answered 503 and named it: the tenant's own signing key cannot be used |
| `invalid_record`       | core answered 422, or the request is malformed   |
| `payloads_unavailable` | core answered 409 (bundle asked for payloads)    |
| `core_error`           | any other non-2xx, or a non-object 2xx body      |
| `core_unreachable`     | transport failure (refused, DNS, timeout)        |

Callers surface the error: a declaration that could not be sealed is saved
with `record_id = NULL` and the UI says "not sealed".

`verify_record` predates this contract and keeps its own shape — an
unreachable verifier is a `verifier_unavailable` finding with `ok: false`
(a Verify panel that shows a green tick because the check could not run is
worse than one that says the verifier was down).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

import httpx

from app.constants import approval_decision
from app.constants.proxy import proxy_internal_url, gateway_service_api_key

# Timeouts (seconds). Verification is pure CPU on small inputs; sealing takes
# the tenant log lock; bundles force a head and walk proofs; backfill seals
# up to 10 000 rows.
DEFAULT_TIMEOUT_SECONDS = 5.0
SEAL_TIMEOUT_SECONDS = 15.0
VERIFY_BUNDLE_TIMEOUT_SECONDS = 30.0
BUNDLE_TIMEOUT_SECONDS = 120.0
BACKFILL_TIMEOUT_SECONDS = 300.0

#: The seal API takes at most this many records per call.
MAX_SEAL_RECORDS = 100
#: Backfill window row cap.
MAX_BACKFILL_LIMIT = 10_000

#: Error bodies are bounded: a proxy error page can be a whole HTML document.
MAX_DETAIL_CHARS = 300

VERIFIER_UNAVAILABLE = "verifier_unavailable"

ERROR_EVIDENCE_DISABLED = "evidence_disabled"
# The tenant's own signing key cannot be used (its seed does not decrypt, or
# does not match its key id): nothing is sealed for the tenant until the key
# is rotated. The core's own code, passed on as it is.
ERROR_TENANT_KEY_UNAVAILABLE = "tenant_key_unavailable"
ERROR_INVALID_RECORD = "invalid_record"
ERROR_PAYLOADS_UNAVAILABLE = "payloads_unavailable"
ERROR_CORE = "core_error"
ERROR_CORE_UNREACHABLE = "core_unreachable"

_STATUS_ERRORS = {
    503: ERROR_EVIDENCE_DISABLED,
    422: ERROR_INVALID_RECORD,
    409: ERROR_PAYLOADS_UNAVAILABLE,
}


# ---------------------------------------------------------------------------
# Shared request path
# ---------------------------------------------------------------------------


def _bounded(text: Any) -> str:
    return str(text).replace("\n", " ").strip()[:MAX_DETAIL_CHARS]


def _error(code: str, detail: Any) -> Dict[str, Any]:
    return {"error": code, "detail": _bounded(detail)}


def is_error(result: Mapping[str, Any]) -> bool:
    """True for a `{"error": …}` result from any function in this module."""
    return isinstance(result, Mapping) and bool(result.get("error"))


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = gateway_service_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key
    return headers


def _error_code(resp: httpx.Response) -> str:
    """The code of a refused request: by status, except that a 503 which
    names the tenant's key is that and not a disabled evidence service."""
    if resp.status_code == 503:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - HTML / empty bodies are expected here
            body = None
        if isinstance(body, dict) and body.get("error") == ERROR_TENANT_KEY_UNAVAILABLE:
            return ERROR_TENANT_KEY_UNAVAILABLE
    return _STATUS_ERRORS.get(resp.status_code, ERROR_CORE)


def _error_detail(resp: httpx.Response) -> str:
    """The core's `detail` (or `error`) when the body is JSON, else raw text."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - HTML / empty bodies are expected here
        body = None
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body.get("message")
        if detail:
            return f"HTTP {resp.status_code}: {detail}"
    return f"HTTP {resp.status_code}: {resp.text}"


def _call(
    method: str,
    path: str,
    *,
    body: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """One request to the core. Returns the JSON object, or an error dict."""
    try:
        resp = httpx.request(
            method,
            f"{proxy_internal_url()}{path}",
            json=body,
            headers=_headers(),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - never raise, by contract
        return _error(ERROR_CORE_UNREACHABLE, str(exc) or exc.__class__.__name__)

    if resp.status_code >= 400:
        return _error(_error_code(resp), _error_detail(resp))
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return _error(ERROR_CORE, f"non-JSON response (HTTP {resp.status_code})")
    if not isinstance(data, dict):
        return _error(ERROR_CORE, "core returned a non-object response")
    return data


def _post(path: str, body: Mapping[str, Any], timeout: float) -> Dict[str, Any]:
    return _call("POST", path, body=body, timeout=timeout)


def _compact(**fields: Any) -> Dict[str, Any]:
    """Drop None-valued optional fields — the core treats absent and null
    differently for some members (e.g. `audience`)."""
    return {k: v for k, v in fields.items() if v is not None}


def _seg(value: str) -> str:
    """URL-encode one path segment (tenant ids are operator-chosen)."""
    return quote(str(value), safe="")


# ---------------------------------------------------------------------------
# Verification (pure; no DB writes on the core side)
# ---------------------------------------------------------------------------


def _unavailable(detail: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "findings": [
            {
                "check": None,
                "code": VERIFIER_UNAVAILABLE,
                "severity": "error",
                "detail": detail,
            }
        ],
        "record_id": None,
    }


def verify_record(
    record: Dict[str, Any],
    statement_b64: Optional[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """POST the record (and its statement, base64) to the core verifier.

    Returns the core's response as-is:
    `{ok, findings: [{check, code, severity, detail}], record_id,
    effect_mode, statement: {verified, iss, sub, kid, error?}}`
    — or a `verifier_unavailable` finding when it cannot be reached.
    """
    data = _post(
        "/v1/evidence/verify/record",
        {"record": record, "statement_b64": statement_b64},
        timeout,
    )
    if is_error(data):
        prefix = (
            "verifier unreachable"
            if data["error"] == ERROR_CORE_UNREACHABLE
            else "verifier returned an error"
        )
        return _unavailable(f"{prefix}: {data['detail']}")
    data.setdefault("ok", False)
    data.setdefault("findings", [])
    data.setdefault("record_id", None)
    return data


def verify_statement(
    statement_b64: str,
    record: Optional[Dict[str, Any]] = None,
    *,
    trusted_kids: Optional[Sequence[str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """`{verified, iss, sub, kid, content_type, record_id?, error?}`."""
    return _post(
        "/v1/evidence/verify/statement",
        _compact(statement_b64=statement_b64, record=record, trusted_kids=_list(trusted_kids)),
        timeout,
    )


def verify_tree_head(
    statement_b64: str,
    previous_statement_b64: Optional[str] = None,
    *,
    trusted_kids: Optional[Sequence[str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """`{verified, log_id, tree_size, root, consistent_with_previous?, error?}`."""
    return _post(
        "/v1/evidence/verify/tree-head",
        _compact(
            statement_b64=statement_b64,
            previous_statement_b64=previous_statement_b64,
            trusted_kids=_list(trusted_kids),
        ),
        timeout,
    )


def verify_receipt(
    receipt_b64: str,
    statement_b64: str,
    *,
    pinned_key_hex: Optional[str] = None,
    witness_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """`{grade: witnessed|timestamped|unverified|invalid, tree_size?,
    leaf_index?, iat?, vendor_ext, errors[]}`. Pass at most one of
    `pinned_key_hex` / `witness_id`."""
    if pinned_key_hex and witness_id:
        return _error(ERROR_INVALID_RECORD, "pass pinned_key_hex or witness_id, not both")
    return _post(
        "/v1/evidence/verify/receipt",
        _compact(
            receipt_b64=receipt_b64,
            statement_b64=statement_b64,
            pinned_key_hex=pinned_key_hex,
            witness_id=witness_id,
        ),
        timeout,
    )


def verify_bundle(
    bundle: Dict[str, Any],
    *,
    trusted_kids: Optional[Sequence[str]] = None,
    timeout: float = VERIFY_BUNDLE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """`{ok, findings[], bundle_digest, records_verified, proofs_verified,
    heads_verified, missing[]}`."""
    return _post(
        "/v1/evidence/verify/bundle",
        _compact(bundle=bundle, trusted_kids=_list(trusted_kids)),
        timeout,
    )


def _list(values: Optional[Sequence[str]]) -> Optional[List[str]]:
    return None if values is None else [str(v) for v in values]


#: Row-chain verification walks up to 10 000 rows and hashes each one.
CHAIN_VERIFY_TIMEOUT_SECONDS = 30.0
#: Core cap on one verification scan (`audit_verify::DEFAULT_SCAN_LIMIT`).
MAX_CHAIN_SCAN = 10_000


def verify_chain(
    chain_id: str,
    *,
    from_seq: int = 0,
    limit: int = MAX_CHAIN_SCAN,
    tenant_id: Optional[str] = None,
    timeout: float = CHAIN_VERIFY_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Verify one row chain range on the core (`POST
    /v1/audit/chains/{id}/verify`). Returns the core's `ChainReport`
    (`verdict` ∈ verified|unattested|pruned|gap|broken|unchained|forged,
    `rows_checked`, `undeclared_gaps`, `truncated` …) or an error dict —
    an error is never a verdict."""
    limit = max(1, min(int(limit), MAX_CHAIN_SCAN))
    path = (
        f"/v1/audit/chains/{_seg(chain_id)}/verify"
        f"?from_seq={max(0, int(from_seq))}&limit={limit}"
    )
    data = _call("POST", path, timeout=timeout)
    if not is_error(data) and not isinstance(data.get("verdict"), str):
        return _error(ERROR_CORE, "chain report has no verdict")
    return data


# ---------------------------------------------------------------------------
# Writes (the core seals; the CP never signs)
# ---------------------------------------------------------------------------


def seal(
    tenant_id: str,
    records: Sequence[Dict[str, Any]],
    *,
    timeout: float = SEAL_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Seal CP-originated records (declarations, reports, incidents …).

    `records` follow the seal API's record shape (`record_type`, `verdict`, `kind`,
    `ai_system_id`, `context` (allow-listed keys only), `payload` (digested,
    never stored), `references`, `chain`, …). Returns
    `{"sealed": [{record_id, log_id, seq, statement_b64, record,
    payload_digest?}]}` in input order, or an error dict.
    """
    records = list(records or [])
    if not records:
        return {"sealed": []}
    if len(records) > MAX_SEAL_RECORDS:
        return _error(
            ERROR_INVALID_RECORD,
            f"at most {MAX_SEAL_RECORDS} records per seal call, got {len(records)}",
        )
    if not all(isinstance(r, Mapping) and r.get("record_type") for r in records):
        return _error(ERROR_INVALID_RECORD, "every record needs a record_type")
    data = _post("/v1/evidence/seal", {"tenant_id": tenant_id, "records": records}, timeout)
    if not is_error(data) and not isinstance(data.get("sealed"), list):
        return _error(ERROR_CORE, "seal response has no `sealed` list")
    return data


def decide_approval(
    tenant_id: str,
    approval_request_id: str,
    decision: str,
    *,
    approver_id: str,
    approver_username: str,
    note: Optional[str] = None,
    timeout: float = SEAL_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Ask the core to decide one tool approval request on an administrator's
    behalf (`POST /v1/internal/approvals/decide`).

    The control plane never writes the request itself: the decision row the
    core writes is the evidence of the human verdict. Returns the core's
    answer — `{"outcome": …}` plus, for `approved`/`rejected`, `status`,
    `resolved_by`, `resolved_at` and (approved) `approval_token` — or an error
    dict. An answer that is not exactly what was asked for is an error: an
    unknown outcome, a decided outcome that is the other decision, or a
    decided outcome without its members. Nothing is assumed decided."""
    if decision not in approval_decision.DECISIONS:
        return _error(ERROR_INVALID_RECORD, f"unknown decision {decision!r}")
    data = _post(
        "/v1/internal/approvals/decide",
        _compact(
            tenant_id=tenant_id,
            approval_request_id=approval_request_id,
            decision=decision,
            note=note,
            approver={"id": approver_id, "username": approver_username},
        ),
        timeout,
    )
    if is_error(data):
        return data
    outcome = data.get("outcome")
    if outcome not in approval_decision.OUTCOMES:
        return _error(ERROR_CORE, f"decide response has an unknown outcome {outcome!r}")
    if outcome in approval_decision.DECIDED:
        if outcome != approval_decision.DECISION_OUTCOME[decision]:
            return _error(ERROR_CORE, f"asked to {decision}, core answered {outcome!r}")
        if data.get("status") != outcome or not all(
            isinstance(data.get(k), str) and data.get(k) for k in ("resolved_by", "resolved_at")
        ):
            return _error(ERROR_CORE, "decide response is missing its decision members")
    return data


def force_tree_head(
    tenant_id: str, *, timeout: float = SEAL_TIMEOUT_SECONDS
) -> Dict[str, Any]:
    """Issue a head if anything in the tenant log is uncovered.
    `{"issued": bool, "head": {id, tree_size, root, statement_b64} | null}`."""
    return _post(f"/v1/evidence/logs/{_seg(tenant_id)}/tree-head", {}, timeout)


def build_bundle(
    tenant_id: str,
    selector: Dict[str, Any],
    *,
    audience: Optional[str] = None,
    payloads: str = "none",
    framework_id: Optional[str] = None,
    catalog_version: Optional[str] = None,
    obligation_key: Optional[str] = None,
    extensions: Optional[Dict[str, Any]] = None,
    closure_depth: int = 1,
    requested_by: Optional[str] = None,
    timeout: float = BUNDLE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Build (and persist, core-side) an evidence bundle.

    `selector` is one of `{"record_ids": […]}`, `{"root_task_id": …}` or
    `{"ai_system_id": …, "from": …, "to": …}`. A non-null `audience` seals
    a disclosure record. Returns `{bundle_id, bundle_digest, record_id,
    disclosure_id?, bundle}` or an error (`payloads_unavailable` when
    `payloads="all"` — bodies never enter records)."""
    if not isinstance(selector, Mapping) or not selector:
        return _error(ERROR_INVALID_RECORD, "selector is required")
    return _post(
        "/v1/evidence/bundles",
        _compact(
            tenant_id=tenant_id,
            selector=dict(selector),
            audience=audience,
            payloads=payloads,
            framework_id=framework_id,
            catalog_version=catalog_version,
            obligation_key=obligation_key,
            extensions=extensions,
            closure_depth=max(0, int(closure_depth)),
            requested_by=requested_by,
        ),
        timeout,
    )


def backfill(
    tenant_id: str,
    from_: str,
    to: str,
    *,
    limit: int = 1000,
    timeout: float = BACKFILL_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Seal governed rows in `[from_, to)` that have no `record_id`
    (records carry `context.backfilled: true`). `limit` is clamped to
    1..10 000."""
    limit = max(1, min(int(limit), MAX_BACKFILL_LIMIT))
    return _post(
        "/v1/evidence/backfill",
        {"tenant_id": tenant_id, "from": from_, "to": to, "limit": limit},
        timeout,
    )


def rotate_key(tenant_id: str, *, timeout: float = SEAL_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Create (first call) or rotate the tenant's own signing key. The
    rotation record is signed by the old key and references the new kid."""
    return _post(f"/v1/evidence/keys/{_seg(tenant_id)}/rotate", {}, timeout)


__all__ = [
    "verify_record",
    "verify_statement",
    "verify_tree_head",
    "verify_receipt",
    "verify_bundle",
    "seal",
    "force_tree_head",
    "build_bundle",
    "backfill",
    "rotate_key",
    "is_error",
    "VERIFIER_UNAVAILABLE",
    "ERROR_EVIDENCE_DISABLED",
    "ERROR_TENANT_KEY_UNAVAILABLE",
    "ERROR_INVALID_RECORD",
    "ERROR_PAYLOADS_UNAVAILABLE",
    "ERROR_CORE",
    "ERROR_CORE_UNREACHABLE",
    "MAX_SEAL_RECORDS",
    "MAX_BACKFILL_LIMIT",
    "DEFAULT_TIMEOUT_SECONDS",
    "SEAL_TIMEOUT_SECONDS",
    "BUNDLE_TIMEOUT_SECONDS",
    "VERIFY_BUNDLE_TIMEOUT_SECONDS",
    "BACKFILL_TIMEOUT_SECONDS",
]
