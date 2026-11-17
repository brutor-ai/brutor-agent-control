# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Manifest signing via the core proxy's audit key

The Ed25519 audit key lives in the Rust core (it signs audit-chain
checkpoints); the control plane never holds it. Anything the control
plane wants attested — an audit-export manifest, an assurance-report
certificate — is POSTed to the proxy's internal signing endpoint and
comes back with a signature and the public key.

Best-effort by design, and honest about it: an unsigned manifest still
carries its digest, which detects corruption and truncation. `signed:
false` plus `unsigned_reason` records which of the two a recipient is
holding — it is never implied.
"""

from __future__ import annotations

from typing import Any, Dict
from app.constants.proxy import proxy_internal_url, gateway_service_api_key

DEFAULT_TIMEOUT_SECONDS = 5.0


def sign_manifest(
    manifest: Dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Dict[str, Any]:
    """Return the manifest with a signature block, or with the reason there
    is none. Never raises: refusing to produce an artifact because the
    proxy was briefly unreachable would trade a complete evidence gap for
    a partial one."""
    out = dict(manifest)
    out["signed"] = False
    try:
        import httpx  # local import: callers are otherwise sync

        base = proxy_internal_url()
        headers = {"Content-Type": "application/json"}
        key = gateway_service_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["X-API-Key"] = key
        resp = httpx.post(
            f"{base}/v1/audit/export-manifest/sign",
            json=manifest,
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 501:
            # Deployment has no audit key. Expected, not a fault.
            out["unsigned_reason"] = "audit signing not configured"
            return out
        resp.raise_for_status()
        body = resp.json()
        out["signature"] = body["signature"]
        out["public_key"] = body["public_key"]
        out["canonical_version"] = body["canonical_version"]
        out["signed"] = True
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        # Bounded: an HTTP error can carry a whole HTML page, and a
        # manifest is evidence a recipient reads, not a stack-trace dump.
        reason = str(exc).replace("\n", " ")[:200]
        out["unsigned_reason"] = f"signing unavailable: {reason}"
    return out


__all__ = ["sign_manifest", "DEFAULT_TIMEOUT_SECONDS"]
