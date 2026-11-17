# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Reachability + auth probes for external guardrail providers.

Why this exists: guardrail configs run with ``fail_open: true`` by
default, so a misconfigured provider endpoint (wrong host, bad key,
unreachable Presidio analyzer) does NOT surface as an error at runtime —
the check silently passes and the operator believes the control is
active. The behaviour test (``POST /test``) can't catch this either: a
dead provider + fail_open returns "no violation", which looks like a
pass. This module does the one thing that distinguishes "reachable +
authenticated" from "silently failing open": a synchronous probe the
operator triggers from the provider config UI.

Each probe is intentionally cheap — a health/validation call, not a full
content scan — and mirrors the real detector's endpoint/auth so a probe
pass means the live path will connect.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import httpx

from app.services.outbound_http import outbound_async_client

from app.core.logging_system import BrutorLogger

logger = BrutorLogger(__name__)

# Probe ceiling. Guardrail providers are meant to answer in 2–4 s on the
# hot path; a generous-but-bounded probe window keeps a hung endpoint
# from wedging the request while still tolerating a cold Presidio start.
DEFAULT_PROBE_TIMEOUT_S = 8.0


class ProbeResult:
    """Internal probe outcome, mapped to the API response by the caller."""

    def __init__(self, status: str, detail: Optional[str] = None, error: Optional[str] = None):
        self.status = status  # "healthy" | "unhealthy" | "timeout"
        self.detail = detail
        self.error = error


def _timeout_s(config: Optional[Dict[str, Any]]) -> float:
    """Probe window, honouring an operator-set per-provider ``timeout_ms``
    (capped to the probe ceiling so a huge config value can't hang the UI)."""
    if config:
        ms = config.get("timeout_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            return min(float(ms) / 1000.0, DEFAULT_PROBE_TIMEOUT_S)
    return DEFAULT_PROBE_TIMEOUT_S


async def probe_provider(
    provider: str,
    endpoint: Optional[str],
    api_key: Optional[str],
    config: Optional[Dict[str, Any]],
) -> ProbeResult:
    """Dispatch to the per-provider probe. Never raises — all failures are
    returned as an unhealthy/timeout ``ProbeResult`` so the endpoint can
    always answer 200 with a structured verdict."""
    provider = (provider or "").lower()
    cfg = config or {}
    try:
        if provider == "presidio":
            return await _probe_presidio(endpoint, cfg)
        if provider == "custom":
            return await _probe_custom(endpoint, api_key, cfg)
        if provider == "openai_moderation":
            return await _probe_openai_moderation(endpoint, api_key, cfg)
        if provider == "lakera":
            return await _probe_lakera(endpoint, api_key, cfg)
        if provider == "bedrock":
            return await _probe_bedrock(cfg)
        return ProbeResult("unhealthy", error=f"No connection probe implemented for provider '{provider}'")
    except Exception as e:  # defensive: probe must never bubble
        logger.warning("Guardrail provider probe raised", context={"provider": provider, "error": str(e)})
        return ProbeResult("unhealthy", error=f"Probe error: {e}")


# ---------------------------------------------------------------------------
# Presidio — self-hosted analyzer, no api_key. Reachability is the whole
# point (this is the case that bit us: a `presedio-analyzer` hostname typo
# fails open). Hit GET /health first (canonical, cheap, no analyzer warmup);
# fall back to a trivial POST /analyze if /health isn't exposed.
# ---------------------------------------------------------------------------
async def _probe_presidio(endpoint: Optional[str], config: Dict[str, Any]) -> ProbeResult:
    if not endpoint:
        return ProbeResult("unhealthy", error="Presidio endpoint (Analyzer URL) is required")
    base = endpoint.rstrip("/")
    timeout = _timeout_s(config)
    async with outbound_async_client(timeout=timeout) as client:
        try:
            resp = await client.get(f"{base}/health")
            if resp.status_code == 200:
                return ProbeResult("healthy", detail=f"Analyzer reachable at {base}/health")
            # /health missing — verify the real /analyze path instead.
            if resp.status_code == 404:
                lang = config.get("language", "en")
                ar = await client.post(
                    f"{base}/analyze",
                    json={"text": "probe", "language": lang},
                )
                if ar.status_code == 200:
                    return ProbeResult("healthy", detail=f"Analyzer reachable at {base}/analyze")
                return ProbeResult("unhealthy", error=f"/analyze returned HTTP {ar.status_code}: {ar.text[:200]}")
            return ProbeResult("unhealthy", error=f"/health returned HTTP {resp.status_code}")
        except httpx.TimeoutException:
            return ProbeResult("timeout", error=f"No response from {base} within {timeout:.0f}s")
        except httpx.HTTPError as e:
            return ProbeResult("unhealthy", error=f"Cannot reach {base}: {e}")


# ---------------------------------------------------------------------------
# Custom webhook — mirrors the detector's request shape + auth so a pass
# means the live call will connect. Any 2xx is healthy; 401/403 means the
# endpoint is up but the key is wrong (still useful to surface distinctly).
# ---------------------------------------------------------------------------
async def _probe_custom(endpoint: Optional[str], api_key: Optional[str], config: Dict[str, Any]) -> ProbeResult:
    if not endpoint:
        return ProbeResult("unhealthy", error="Custom webhook endpoint is required")
    timeout = _timeout_s(config)
    headers = {"Content-Type": "application/json"}
    if api_key:
        auth_header = config.get("auth_header")
        if auth_header:
            headers[auth_header] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    async with outbound_async_client(timeout=timeout) as client:
        try:
            resp = await client.post(endpoint, json={"content": "probe"}, headers=headers)
            if 200 <= resp.status_code < 300:
                return ProbeResult("healthy", detail=f"Webhook reachable ({resp.status_code})")
            if resp.status_code in (401, 403):
                return ProbeResult("unhealthy", error=f"Authentication rejected (HTTP {resp.status_code}) — check the API key/header")
            return ProbeResult("unhealthy", error=f"Webhook returned HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.TimeoutException:
            return ProbeResult("timeout", error=f"No response from {endpoint} within {timeout:.0f}s")
        except httpx.HTTPError as e:
            return ProbeResult("unhealthy", error=f"Cannot reach {endpoint}: {e}")


# ---------------------------------------------------------------------------
# OpenAI Moderation — validates the key with a 1-token moderation call.
# ---------------------------------------------------------------------------
async def _probe_openai_moderation(endpoint: Optional[str], api_key: Optional[str], config: Dict[str, Any]) -> ProbeResult:
    if not api_key:
        return ProbeResult("unhealthy", error="OpenAI Moderation requires an API key")
    url = (endpoint or "https://api.openai.com/v1/moderations").rstrip("/")
    timeout = _timeout_s(config)
    async with outbound_async_client(timeout=timeout) as client:
        try:
            resp = await client.post(
                url,
                json={"input": "probe"},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return ProbeResult("healthy", detail="Moderation API reachable and key accepted")
            if resp.status_code in (401, 403):
                return ProbeResult("unhealthy", error="API key rejected (HTTP 401/403)")
            return ProbeResult("unhealthy", error=f"Moderation API returned HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.TimeoutException:
            return ProbeResult("timeout", error=f"No response from {url} within {timeout:.0f}s")
        except httpx.HTTPError as e:
            return ProbeResult("unhealthy", error=f"Cannot reach {url}: {e}")


# ---------------------------------------------------------------------------
# Lakera Guard — validates the key against the guard endpoint.
# ---------------------------------------------------------------------------
async def _probe_lakera(endpoint: Optional[str], api_key: Optional[str], config: Dict[str, Any]) -> ProbeResult:
    if not api_key:
        return ProbeResult("unhealthy", error="Lakera requires an API key")
    url = (endpoint or "https://api.lakera.ai/v2/guard").rstrip("/")
    timeout = _timeout_s(config)
    async with outbound_async_client(timeout=timeout) as client:
        try:
            resp = await client.post(
                url,
                json={"messages": [{"role": "user", "content": "probe"}]},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            if 200 <= resp.status_code < 300:
                return ProbeResult("healthy", detail="Lakera Guard reachable and key accepted")
            if resp.status_code in (401, 403):
                return ProbeResult("unhealthy", error="API key rejected (HTTP 401/403)")
            return ProbeResult("unhealthy", error=f"Lakera returned HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.TimeoutException:
            return ProbeResult("timeout", error=f"No response from {url} within {timeout:.0f}s")
        except httpx.HTTPError as e:
            return ProbeResult("unhealthy", error=f"Cannot reach {url}: {e}")


# ---------------------------------------------------------------------------
# Bedrock Guardrails — validate creds + region + guardrail existence via
# the free `bedrock:GetGuardrail` control-plane call (no ApplyGuardrail
# cost). boto3 is sync, so run it off the event loop.
# ---------------------------------------------------------------------------
async def _probe_bedrock(config: Dict[str, Any]) -> ProbeResult:
    guardrail_id = config.get("guardrail_id")
    region = config.get("region")
    if not guardrail_id:
        return ProbeResult("unhealthy", error="Bedrock guardrail_id is required")
    if not region:
        return ProbeResult("unhealthy", error="Bedrock region is required")
    # The entry's own credential, or no probe (as in the gateway): with none
    # given, the AWS SDK would resolve the identity of this process (its
    # environment, a credentials file, an instance role), which is the
    # operator's and not the tenant's, and the gateway reads no such thing.
    for field in ("aws_access_key_id", "aws_secret_access_key"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            return ProbeResult(
                "unhealthy",
                error=f"Bedrock {field} is required: a guardrail is called with its own AWS credential, "
                      "and the gateway reads no other.",
            )

    def _call() -> ProbeResult:
        try:
            import boto3  # local import — boto3 is an optional/heavy dep
            from botocore.config import Config as BotoConfig
            from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
        except ImportError:
            return ProbeResult("unhealthy", error="boto3 is not installed in the control plane")

        kwargs: Dict[str, Any] = {
            "region_name": region,
            "aws_access_key_id": config["aws_access_key_id"],
            "aws_secret_access_key": config["aws_secret_access_key"],
        }
        if config.get("aws_session_token"):
            kwargs["aws_session_token"] = config["aws_session_token"]
        try:
            client = boto3.client(
                "bedrock",
                config=BotoConfig(connect_timeout=int(DEFAULT_PROBE_TIMEOUT_S), read_timeout=int(DEFAULT_PROBE_TIMEOUT_S), retries={"max_attempts": 0}),
                **kwargs,
            )
            params: Dict[str, Any] = {"guardrailIdentifier": guardrail_id}
            version = config.get("guardrail_version")
            if version and str(version).upper() != "DRAFT":
                params["guardrailVersion"] = str(version)
            resp = client.get_guardrail(**params)
            name = resp.get("name", guardrail_id)
            return ProbeResult("healthy", detail=f"Guardrail '{name}' reachable in {region}")
        except NoCredentialsError:
            return ProbeResult("unhealthy", error="The AWS credential of the entry was not accepted")
        except EndpointConnectionError as e:
            return ProbeResult("timeout", error=f"Cannot reach Bedrock in {region}: {e}")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            msg = e.response.get("Error", {}).get("Message", str(e))
            return ProbeResult("unhealthy", error=f"{code}: {msg}")
        except Exception as e:  # noqa: BLE001 — surface anything else as unhealthy
            return ProbeResult("unhealthy", error=f"Bedrock probe failed: {e}")

    try:
        return await asyncio.wait_for(asyncio.to_thread(_call), timeout=DEFAULT_PROBE_TIMEOUT_S + 2)
    except asyncio.TimeoutError:
        return ProbeResult("timeout", error=f"Bedrock probe exceeded {DEFAULT_PROBE_TIMEOUT_S:.0f}s")
