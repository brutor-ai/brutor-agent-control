# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Client for the Rust core's check evaluator (`/v1/assurance/checks/*`).

One engine: the core compiles and evaluates the bounded CEL profile;
this module only transports. Service-to-service auth is the same
`BRUTOR_GATEWAY_SERVICE_API_KEY` + `X-Tenant-ID` pair the model health
probes use. Authoring fails CLOSED: if the core is unreachable a spec
cannot be validated, so it cannot be saved — a spec that was never
compile-checked must never reach the runtime.
"""

from typing import Any, Dict, Optional

import httpx
from app.constants.proxy import proxy_internal_url, gateway_service_api_key


class CoreUnavailable(Exception):
    """The evaluator could not be reached — authoring must stop."""


def _base_url() -> str:
    return proxy_internal_url()


def _headers(tenant_id: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "X-Tenant-ID": tenant_id}
    key = gateway_service_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def compile_expression(
    tenant_id: str, expression: str, output: Optional[str] = None
) -> Dict[str, Any]:
    """Compile + profile-check + sample-evaluate an expression.

    Returns the core's verdict: ``{ok, errors, sample_value,
    context_version}``. Raises :class:`CoreUnavailable` on transport
    failure.
    """
    try:
        resp = httpx.post(
            f"{_base_url()}/v1/assurance/checks/compile",
            json={"expression": expression, "output": output},
            headers=_headers(tenant_id),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise CoreUnavailable(
            f"check evaluator unreachable ({exc.__class__.__name__}); "
            "the spec cannot be validated, so it cannot be saved"
        ) from exc


def backtest(
    tenant_id: str, check_id: str, version: int, window_days: int
) -> Dict[str, Any]:
    """Evaluate a published spec version against recorded runs.

    Returns ``{ok, evaluated, breaches, errors, window_days, samples}``.
    Raises :class:`CoreUnavailable` on transport failure; a 422 from the
    core (unknown version, non-deterministic tier) comes back as
    ``{ok: False, error}``.
    """
    try:
        resp = httpx.post(
            f"{_base_url()}/v1/assurance/checks/backtest",
            json={
                "check_id": check_id,
                "version": version,
                "window_days": window_days,
            },
            headers=_headers(tenant_id),
            timeout=120,
        )
        if resp.status_code == 422:
            return resp.json()
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise CoreUnavailable(
            f"check evaluator unreachable ({exc.__class__.__name__})"
        ) from exc
