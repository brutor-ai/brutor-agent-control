# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Shared test double for the core's evidence API, used
by the compliance suites.

`FakeCore` replaces `core_client.seal` / `core_client.build_bundle` and
records every request, so a test can assert WHAT was sealed (record type,
verdict, context, payload, chain) without a running core — and can switch
the core into the two failure modes every caller must survive:
`evidence_disabled` (no key configured, 503) and `core_unreachable`
(transport failure). Record ids are deterministic 64-hex strings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from app.services.evidence import core_client

MODE_OK = "ok"
MODE_DISABLED = core_client.ERROR_EVIDENCE_DISABLED
MODE_UNREACHABLE = core_client.ERROR_CORE_UNREACHABLE


class FakeCore:
    def __init__(self) -> None:
        self.mode = MODE_OK
        self.seal_calls: List[Dict[str, Any]] = []
        self.bundle_calls: List[Dict[str, Any]] = []
        self._n = 0

    # -- core_client.seal ------------------------------------------------
    def seal(self, tenant_id: str, records, **_kw) -> Dict[str, Any]:
        records = list(records)
        self.seal_calls.append({"tenant_id": tenant_id, "records": records})
        if self.mode != MODE_OK:
            return {"error": self.mode, "detail": f"fake core: {self.mode}"}
        sealed = []
        for r in records:
            # Mirror the core's contract checks the CP relies on.
            assert r.get("record_type", "").startswith("ai.brutor."), r
            self._n += 1
            rid = hashlib.sha256(f"{self._n}|{json.dumps(r, sort_keys=True, default=str)}".encode()).hexdigest()
            payload_digest = (
                hashlib.sha256(json.dumps(r["payload"], sort_keys=True, default=str).encode()).hexdigest()
                if "payload" in r
                else None
            )
            sealed.append({"record_id": rid, "log_id": "log-fake", "seq": self._n,
                           "statement_b64": "AA==", "record": {"record_id": rid},
                           **({"payload_digest": payload_digest} if payload_digest else {})})
        return {"sealed": sealed}

    # -- core_client.build_bundle -----------------------------------------
    def build_bundle(self, tenant_id: str, selector, **kwargs) -> Dict[str, Any]:
        self.bundle_calls.append({"tenant_id": tenant_id, "selector": selector, **kwargs})
        if self.mode != MODE_OK:
            return {"error": self.mode, "detail": f"fake core: {self.mode}"}
        self._n += 1
        return {"bundle_id": f"bdl-fake-{self._n}", "bundle_digest": "ab" * 32, "record_id": "cd" * 32,
                "bundle": {"manifest": {"kind": "ai.brutor/evidence-bundle"}, "extensions": kwargs.get("extensions")}}

    # -- inspection ---------------------------------------------------------
    def records(self, record_type: Optional[str] = None) -> List[Dict[str, Any]]:
        out = [r for call in self.seal_calls for r in call["records"]]
        return [r for r in out if record_type is None or r["record_type"] == record_type]

    def reset(self) -> None:
        self.mode = MODE_OK
        self.seal_calls.clear()
        self.bundle_calls.clear()


def install(monkeypatch) -> FakeCore:
    fake = FakeCore()
    monkeypatch.setattr(core_client, "seal", fake.seal)
    monkeypatch.setattr(core_client, "build_bundle", fake.build_bundle)
    return fake


__all__ = ["FakeCore", "MODE_DISABLED", "MODE_OK", "MODE_UNREACHABLE", "install"]
