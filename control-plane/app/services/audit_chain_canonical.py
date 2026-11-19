# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Audit hash chain — reference verifier for the canonical row format

The Rust core seals every `proxy_logs` row as

    row_hash = SHA-256(canonical_bytes(row, prev_hash))

and verifies chains in the database (`services/audit_verify.rs`). This
module is the second, independent implementation of the canonical bytes, for
evidence that has left the database — exported rows, and third parties
re-implementing the format from the published docs. It depends on the
standard library only, so it can be lifted out and run anywhere.

Two implementations of one format is the thing the core's verifier warns
against (drift makes honest rows fail). What makes it safe here is that
drift cannot land quietly: `tests/test_audit_chain_v5.py` pins the same
fixed rows to the same hashes the core's `audit_chain::tests` pin and the
RFC 8785 vectors the core's `jcs::tests` pin, and `tests/test_audit_chain_v3.py`
parses the core's `V3_FIELDS`, `V4_ADDED_FIELDS`, `V5_ADDED_FIELDS`,
`CHAIN_EXCLUDED_COLUMNS` and version constants so the field lists and the
known format cannot diverge.

## The format

One format, `v5`: the one this release writes and the one this
verifier computes, like the core's `audit_chain::row_matches`. A row must
name it in `chain_version` and hash to its stored value under it; a row that
names another format, or none, does not verify. The tag is also the first
field inside the hashed bytes, so a row cannot be given the label without
having been sealed under it.

The bytes: `b"v5\\0"`, then for each field in `V3_FIELDS`, then each in
`V4_ADDED_FIELDS`, then each in `V5_ADDED_FIELDS`, and finally prev_hash:
NULL → `0x00`; present → `0x01 ‖ u32-BE byte length ‖ UTF-8`. Booleans are `"1"`/`"0"`, integers
decimal, the timestamp RFC 3339 UTC with exactly six fractional digits and
`Z` (truncated to microseconds — what `timestamptz` stores). The JSON
decision records (`V4_ADDED_FIELDS`) are each their RFC 8785 (JCS) text;
SQL NULL and a JSON `null` document both encode as absent. The credential
fields (`V5_ADDED_FIELDS`) are plain strings, like the `V3_FIELDS` scalars.
"""

from __future__ import annotations

import hashlib
import math
import struct
from decimal import Decimal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: The row format: the one this release writes and the one verified.
CANONICAL_VERSION = "v5"
#: Every row format the verifier computes. Mirrors `audit_chain::KNOWN_VERSIONS`.
KNOWN_VERSIONS = (CANONICAL_VERSION,)

GENESIS_HASH = "0" * 64

#: The scalar fields, in hash order. Mirrors
#: `audit_chain::V3_FIELDS` in the core; the test suite parses the Rust list
#: and compares.
V3_FIELDS: Tuple[str, ...] = (
    # Identity, time and provenance
    "id",
    "tenant_id",
    "timestamp",
    "source",
    # What was called
    "proxy_type",
    "proxy_subtype",
    "http_method",
    "endpoint",
    "target_url",
    "server_id",
    "idp_server_id",
    "mcp_method",
    "mcp_capability_name",
    "skill_id",
    "skill_execution_id",
    "llm_provider",
    "llm_model_requested",
    "llm_model_used",
    "oauth_flow_type",
    "oauth_scope",
    "rag_collection_ids",
    "prompt_fingerprint",
    # Who called it
    "api_key_id",
    "resource_group_id",
    "gateway_user_id",
    "idp_user_id",
    "actor_agent_id",
    "subject_id",
    "subject_kind",
    "is_auth_event",
    # Which software, which release
    "agent_impl_name",
    "agent_impl_version",
    "agent_impl_build",
    "agent_impl_source",
    "client_label",
    # Under what authority, and what was decided
    "agent_authz_decision",
    "agent_grant_id",
    "semantic_policy_id",
    "argument_policy_id",
    "guardrail_config_id",
    "guardrail_check",
    "guardrail_action",
    "guardrail_blocked",
    "argument_policy_denied",
    "pii_detected",
    "autonomy_level",
    "autonomy_provenance",
    "consumed_approval_request_id",
    "region_routed_to",
    "residency_class",
    # Outcome
    "http_status_code",
    "success",
    "error_type",
    "upstream_request_id",
    "a2a_task_state",
    # Run and call graph
    "root_task_id",
    "current_task_id",
    "parent_task_id",
    "delegation_depth",
    "trace_root_kind",
    "trace_continuity",
    "turn_id",
    "run_end_state",
    "deciding_model",
    # Content and tool provenance
    "request_content_hash",
    "response_content_hash",
    "server_version",
    "tool_definition_hash",
)

#: What follows V3_FIELDS: the JSON decision records, hashed as their
#: RFC 8785 text. Mirrors `audit_chain::V4_ADDED_FIELDS`.
V4_ADDED_FIELDS: Tuple[str, ...] = (
    "compliance_tags",
    "guardrails_skipped",
    "guardrails_degraded",
    "pre_action_state",
    "custom_metadata",
    "tags",
)

#: What follows V4_ADDED_FIELDS: the credential that made the call and the
#: identity judge's verdict, plain strings. Mirrors `audit_chain::V5_ADDED_FIELDS`.
V5_ADDED_FIELDS: Tuple[str, ...] = (
    "credential_kind",
    "credential_assurance",
    "credential_ref",
    "credential_policy_outcome",
)

#: The canonical field order of the format.
V5_FIELDS: Tuple[str, ...] = V3_FIELDS + V4_ADDED_FIELDS + V5_ADDED_FIELDS

JSON_FIELDS = frozenset(V4_ADDED_FIELDS)

BOOL_FIELDS = frozenset(
    {
        "guardrail_blocked",
        "argument_policy_denied",
        "success",
        "is_auth_event",
        "pii_detected",
    }
)
INT_FIELDS = frozenset({"http_status_code", "delegation_depth"})

#: Every proxy_logs column OUTSIDE the chain, and why. Mirrors
#: `audit_chain::CHAIN_EXCLUDED_COLUMNS` (reason = the Rust `Excluded`
#: variant); together with V5_FIELDS it must partition the ORM's columns.
CHAIN_EXCLUDED_COLUMNS: Dict[str, str] = {
    "chain_id": "ChainMechanics",
    "chain_seq": "ChainMechanics",
    "prev_hash": "ChainMechanics",
    "row_hash": "ChainMechanics",
    "chain_version": "ChainMechanics",
    "request_headers": "RemovableContent",
    "request_body_preview": "RemovableContent",
    "response_headers": "RemovableContent",
    "response_body_preview": "RemovableContent",
    "idp_user_email": "ErasableIdentifier",
    "idp_user_username": "ErasableIdentifier",
    "gateway_user_username": "ErasableIdentifier",
    "client_ip": "ErasableIdentifier",
    "user_agent": "ErasableIdentifier",
    "api_key_name": "Label",
    "correlation_id": "CallerCorrelation",
    "parent_request_id": "CallerCorrelation",
    "request_id": "CallerCorrelation",
    "mcp_request_id": "CallerCorrelation",
    "end_user_session_id": "CallerCorrelation",
    "trace_id": "CallerCorrelation",
    "span_id": "CallerCorrelation",
    "step_id": "CallerCorrelation",
    "step_name": "CallerCorrelation",
    "turn_seq": "CallerCorrelation",
    # The client's X-Brutor-Outcome-Id on the run-closing
    # step. The client's label for the run, judged at finalize and recorded
    # on the run; like the step name, not a fact the gateway attests.
    "outcome_id": "CallerCorrelation",
    "request_size_bytes": "Metering",
    "response_size_bytes": "Metering",
    "duration_ms": "Metering",
    "llm_prompt_tokens": "Metering",
    "llm_completion_tokens": "Metering",
    "llm_total_tokens": "Metering",
    "llm_estimated_cost_usd": "Metering",
    "media_images_generated": "Metering",
    "media_audio_chars": "Metering",
    "media_audio_seconds_input": "Metering",
    "media_video_seconds_output": "Metering",
    "llm_cache_hit": "Metering",
    "llm_cache_layer": "Metering",
    "llm_tokens_saved": "Metering",
    "llm_cache_similarity_score": "Metering",
    "prompt_cache_write_tokens": "Metering",
    "prompt_cache_read_tokens": "Metering",
    "prompt_cache_savings_usd": "Metering",
    "llm_context_tokens": "Metering",
    "rag_enabled": "Metering",
    "rag_chunks_retrieved": "Metering",
    "rag_context_tokens": "Metering",
    "rag_top_similarity_score": "Metering",
    "rag_retrieval_duration_ms": "Metering",
    "llm_streaming": "RequestShape",
    "llm_temperature": "RequestShape",
    "llm_max_tokens": "RequestShape",
    "llm_function_calling": "RequestShape",
    "llm_used_fallback": "RequestShape",
    "llm_fallback_reason": "RequestShape",
    "error_message": "FreeText",
    "record_id": "AfterSeal",
    "created_at": "AfterSeal",
    "bodies_pruned_at": "AfterSeal",
    "erased_at": "AfterSeal",
    "a2a_protocol_version": "AfterSeal",
}

#: Chain columns an exported row needs beside the covered fields.
CHAIN_COLUMNS: Tuple[str, ...] = ("chain_id", "chain_seq", "prev_hash", "row_hash", "chain_version")


# ---------------------------------------------------------------------------
# RFC 8785 — JSON Canonicalization Scheme
# ---------------------------------------------------------------------------
#
# Independent of the core's `services/jcs.rs`; both pin the RFC's vectors.
# Input is JSON data as `json.loads` (or a JSON/JSONB column) produces it:
# dict with str keys, list, str, int, float, bool, None.


def jcs_number(value: Any) -> str:
    """An IEEE-754 double as ECMAScript `Number.prototype.toString` prints
    it (RFC 8785 §3.2.2.3). Integers are numbers too: JCS serialises every
    number as the double it denotes, so 2**53 + 1 prints as 2**53."""
    if isinstance(value, bool):
        raise TypeError("a boolean is not a JSON number")
    try:
        f = float(value)
    except OverflowError as exc:
        raise ValueError(f"number out of IEEE-754 range: {value!r}") from exc
    if not math.isfinite(f):
        raise ValueError(f"JCS has no form for {value!r}")
    if f == 0.0:
        return "0"
    # repr() is the shortest round-tripping string, nearest the exact value,
    # ties to even — the digits ECMAScript chooses. Decimal splits it into
    # digits and exponent without another float conversion.
    _, digit_tuple, exponent = Decimal(repr(abs(f))).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    stripped = len("".join(map(str, digit_tuple))) - len(digits)
    k = len(digits)
    # value = 0.d1…dk × 10**n
    n = exponent + stripped + k
    sign = "-" if f < 0 else ""
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("-" if e < 0 else "+") + str(abs(e))
    return sign + body


_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def jcs_string(value: str) -> str:
    """ECMAScript `JSON.stringify` escaping (RFC 8785 §3.2.2.2)."""
    out = ['"']
    for ch in value:
        cp = ord(ch)
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        elif 0xD800 <= cp <= 0xDFFF:
            raise ValueError("lone surrogate: JCS input must be valid Unicode")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(name: str) -> bytes:
    # Big-endian UTF-16 bytes compare exactly as the code-unit sequence.
    return name.encode("utf-16-be", errors="strict")


def jcs_canonicalize(value: Any) -> str:
    """The RFC 8785 canonical text of a JSON value."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return jcs_string(value)
    if isinstance(value, (int, float)):
        return jcs_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(jcs_canonicalize(v) for v in value) + "]"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"JSON object keys are strings, got {type(k).__name__}")
        members = sorted(value.items(), key=lambda kv: _utf16_key(kv[0]))
        return "{" + ",".join(jcs_string(k) + ":" + jcs_canonicalize(v) for k, v in members) + "}"
    raise TypeError(f"not JSON data: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Field text
# ---------------------------------------------------------------------------


def _as_utc(value: Any) -> datetime:
    """A covered timestamp as an aware UTC datetime. Accepts a datetime
    (naive = UTC, as `timestamptz` reads back) or an ISO-8601 string."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError(f"timestamp must be a datetime or ISO string, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_timestamp(value: Any) -> str:
    """RFC 3339, UTC, `Z`, exactly six fractional digits."""
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _field_text(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if name in JSON_FIELDS:
        # SQL NULL and a JSON null document were handled above: both None.
        return jcs_canonicalize(value)
    if name == "timestamp":
        return canonical_timestamp(value)
    if name in BOOL_FIELDS:
        return "1" if value else "0"
    if name in INT_FIELDS:
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------


def _push(out: bytearray, value: Optional[str]) -> None:
    if value is None:
        out.append(0x00)
        return
    raw = value.encode("utf-8")
    out.append(0x01)
    out += struct.pack(">I", len(raw))
    out += raw


def canonical_row_bytes(row: Any, prev_hash: str) -> bytes:
    """The canonical bytes of a row under the format (see the module text)."""
    out = bytearray(CANONICAL_VERSION.encode("ascii") + b"\x00")
    for name in V5_FIELDS:
        _push(out, _field_text(name, _get(row, name)))
    _push(out, prev_hash)
    return bytes(out)


def hash_row(row: Any, prev_hash: str) -> str:
    """SHA-256 hex of the row's canonical bytes."""
    return hashlib.sha256(canonical_row_bytes(row, prev_hash)).hexdigest()


def row_matches(row: Any, chain_version: Optional[str], prev_hash: str, stored_hash: str) -> bool:
    """Does the row's content produce its stored hash? The row must name the
    format in `chain_version`; a row that names another format, or none,
    does not verify. Same as `audit_chain::row_matches` in the core."""
    return chain_version == CANONICAL_VERSION and hash_row(row, prev_hash) == stored_hash


# ---------------------------------------------------------------------------
# Verifying rows outside the database
# ---------------------------------------------------------------------------


@dataclass
class RowsReport:
    """What a set of exported rows proves on its own.

    Content is checked per row, under the one format the verifier computes:
    a row labelled with any other format, or none, is a bad row. Links are checked only between rows that are
    adjacent in the same chain (`chain_seq` n and n+1): a tenant export
    interleaves with other tenants' rows on the writer chain, so a missing
    sequence is expected there and is reported, not failed. Attesting the
    heads needs the signed checkpoints (or the witnessed tenant log).
    """

    rows_checked: int = 0
    rows_bad: List[str] = field(default_factory=list)
    links_checked: int = 0
    links_bad: List[str] = field(default_factory=list)
    unchained: int = 0
    versions: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.rows_bad and not self.links_bad


def verify_rows(rows: Iterable[Any]) -> RowsReport:
    """Check each row's content against its `row_hash` under its sealed
    format, and every link between consecutive rows of the same chain."""
    report = RowsReport()
    by_chain: Dict[str, List[Any]] = {}
    for row in rows:
        stored = _get(row, "row_hash")
        prev = _get(row, "prev_hash")
        if stored is None or prev is None:
            report.unchained += 1
            continue
        report.rows_checked += 1
        version = _get(row, "chain_version")
        label = "none" if version is None else version
        report.versions[label] = report.versions.get(label, 0) + 1
        if not row_matches(row, version, prev, stored):
            report.rows_bad.append(str(_get(row, "id")))
        chain = _get(row, "chain_id")
        if chain is not None and _get(row, "chain_seq") is not None:
            by_chain.setdefault(chain, []).append(row)
    for chain_rows in by_chain.values():
        chain_rows.sort(key=lambda r: int(_get(r, "chain_seq")))
        for before, after in zip(chain_rows, chain_rows[1:]):
            if int(_get(after, "chain_seq")) != int(_get(before, "chain_seq")) + 1:
                continue
            report.links_checked += 1
            if _get(after, "prev_hash") != _get(before, "row_hash"):
                report.links_bad.append(str(_get(after, "id")))
    return report


def chain_evidence(row: Any) -> Dict[str, Any]:
    """The self-verifiable chain block for one exported row: the chain
    columns plus every field the format covers, under their column names,
    JSON-safe (timestamp as ISO-8601 at full stored precision)."""
    fields: Dict[str, Any] = {}
    for name in V5_FIELDS:
        value = _get(row, name)
        if name == "timestamp" and value is not None:
            value = _as_utc(value).isoformat()
        fields[name] = value
    out: Dict[str, Any] = {c: _get(row, c) for c in CHAIN_COLUMNS}
    out["fields"] = fields
    return out
