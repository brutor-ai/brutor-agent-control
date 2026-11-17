# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Sealing CP-originated records

The control plane holds no key and does no cryptography: every record is
sealed by the core's `POST /v1/evidence/seal` through `core_client.seal`.
This module is the one place CP code builds those requests:

* `build_record(record_type, …)` — a seal-API record; `context` keys with a
  `None` value are dropped (the core admits only allow-listed, non-null
  scalars), `record_type` is mirrored into `context` exactly as the core
  does;
* `seal(tenant_id, record_type, …)` / `seal_many(tenant_id, records)` —
  never raise; a failure comes back as a `SealOutcome` with `error`
  (`evidence_disabled`, `core_unreachable`, `invalid_record`, …), one per
  input record, and the caller saves its row with `record_id = NULL`
  ("not sealed");
* `reference`, `references`, `text_digest`, `json_digest` — digest helpers
  (free text and structured payloads enter records only as digests).

Hooks for specific acts (profile declarations, contract promotions, run
aborts) live in `sealing_hooks.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from app.services.evidence import core_client

log = logging.getLogger("brutor.compliance.sealing")

# record_type vocabulary for records the CP seals.
RECORD_DECLARATION = "ai.brutor.declaration"
RECORD_EPOCH_BOUNDARY = "ai.brutor.epoch-boundary"
RECORD_NOTICE_CONFIG = "ai.brutor.notice-config"
RECORD_RUBRIC = "ai.brutor.rubric"
RECORD_ASSESSMENT = "ai.brutor.assessment"
RECORD_ADJUDICATION = "ai.brutor.adjudication"
RECORD_REPORT = "ai.brutor.report"
RECORD_INCIDENT = "ai.brutor.incident"
RECORD_DOCUMENTATION = "ai.brutor.documentation"
RECORD_RUN_ABORT = "ai.brutor.run-abort"

CP_RECORD_TYPES = frozenset({
    RECORD_DECLARATION,
    RECORD_EPOCH_BOUNDARY,
    RECORD_NOTICE_CONFIG,
    RECORD_RUBRIC,
    RECORD_ASSESSMENT,
    RECORD_ADJUDICATION,
    RECORD_REPORT,
    RECORD_INCIDENT,
    RECORD_DOCUMENTATION,
    RECORD_RUN_ABORT,
})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class SealOutcome:
    record_id: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[str] = None
    log_id: Optional[str] = None
    seq: Optional[int] = None
    payload_digest: Optional[str] = None

    @property
    def sealed(self) -> bool:
        return self.record_id is not None

    def status(self) -> Dict[str, Any]:
        """The `SealStatus` block every write response carries."""
        return {
            "sealed": self.sealed,
            "record_id": self.record_id,
            "seal_error": self.error,
            "seal_detail": self.detail,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"sealed": self.sealed, "record_id": self.record_id, "error": self.error, "detail": self.detail}


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def _canon(value: Any) -> Any:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 2 ** 53:
            return int(value)
        return value
    if isinstance(value, Mapping):
        # RFC 8785 sorts by UTF-16 code units.
        items = sorted(((str(k), v) for k, v in value.items()), key=lambda kv: kv[0].encode("utf-16-be"))
        return {k: _canon(v) for k, v in items}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    """JCS-shaped canonical JSON (sorted keys, no whitespace, UTF-8, no
    ASCII escaping). Integral floats are written as integers."""
    return json.dumps(_canon(value), separators=(",", ":"), ensure_ascii=False, sort_keys=False).encode("utf-8")


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def reference(ref_type: str, digest: Optional[str], purpose: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A record reference, or None when `digest` is not 64 lowercase hex
    (so callers can pass possibly-missing digests straight through)."""
    if not isinstance(digest, str) or not _HEX64.match(digest):
        return None
    ref: Dict[str, Any] = {"type": ref_type, "digest": digest}
    if purpose:
        ref["purpose"] = purpose
    return ref


def references(*refs: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    kept = [r for r in refs if r]
    return kept or None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def build_record(
    record_type: str,
    *,
    verdict: Optional[str] = None,
    kind: Optional[str] = None,
    ai_system_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    action_id: Optional[str] = None,
    disposition: Optional[Dict[str, Any]] = None,
    controls: Optional[Sequence[Dict[str, Any]]] = None,
    context: Optional[Mapping[str, Any]] = None,
    payload: Any = None,
    references: Optional[Sequence[Dict[str, Any]]] = None,  # noqa: F811 — keyword mirrors the API
    chain: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {"record_type": record_type}
    for k, v in (context or {}).items():
        if v is None or (isinstance(v, (list, tuple)) and not v):
            continue
        ctx[k] = list(v) if isinstance(v, tuple) else v
    record: Dict[str, Any] = {"record_type": record_type, "context": ctx}
    optional = {
        "verdict": verdict,
        "kind": kind,
        "ai_system_id": ai_system_id,
        "timestamp": timestamp,
        "action_id": action_id,
        "disposition": disposition,
        "controls": list(controls) if controls else None,
        "payload": payload,
        "references": list(references) if references else None,
        "chain": chain,
    }
    record.update({k: v for k, v in optional.items() if v is not None})
    return record


def seal_many(tenant_id: str, records: Sequence[Dict[str, Any]]) -> List[SealOutcome]:
    """Seal `records` (in core_client.MAX_SEAL_RECORDS chunks; each chunk
    all-or-nothing). Returns one outcome per record, in order. Never raises."""
    records = list(records or [])
    out: List[SealOutcome] = []
    for i in range(0, len(records), core_client.MAX_SEAL_RECORDS):
        chunk = records[i:i + core_client.MAX_SEAL_RECORDS]
        try:
            result = core_client.seal(tenant_id, chunk)
        except Exception as exc:  # noqa: BLE001 — core_client never raises; belt and braces
            result = {"error": core_client.ERROR_CORE, "detail": str(exc)[:300]}
        if core_client.is_error(result):
            error, detail = str(result.get("error")), result.get("detail")
            log.warning("seal of %d record(s) for tenant %s failed: %s %s", len(chunk), tenant_id, error, detail)
            out.extend(SealOutcome(error=error, detail=detail) for _ in chunk)
            continue
        sealed = result.get("sealed") or []
        for j in range(len(chunk)):
            entry = sealed[j] if j < len(sealed) and isinstance(sealed[j], dict) else {}
            rid = entry.get("record_id")
            if rid:
                out.append(SealOutcome(record_id=rid, log_id=entry.get("log_id"), seq=entry.get("seq"),
                                       payload_digest=entry.get("payload_digest")))
            else:
                out.append(SealOutcome(error=core_client.ERROR_CORE, detail="seal response carried no record_id"))
    return out


def seal(tenant_id: str, record_type: str, **fields: Any) -> SealOutcome:
    """Build and seal one record. Never raises."""
    return seal_many(tenant_id, [build_record(record_type, **fields)])[0]


__all__ = [
    "CP_RECORD_TYPES",
    "RECORD_ADJUDICATION",
    "RECORD_ASSESSMENT",
    "RECORD_DECLARATION",
    "RECORD_DOCUMENTATION",
    "RECORD_EPOCH_BOUNDARY",
    "RECORD_INCIDENT",
    "RECORD_NOTICE_CONFIG",
    "RECORD_REPORT",
    "RECORD_RUBRIC",
    "RECORD_RUN_ABORT",
    "SealOutcome",
    "build_record",
    "canonical_bytes",
    "json_digest",
    "reference",
    "references",
    "seal",
    "seal_many",
    "text_digest",
]
