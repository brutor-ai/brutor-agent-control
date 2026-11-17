# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live gateway sweep — every catalog model of every keyed provider, one
minimal call each, THROUGH THE CORE PROXY.

`tests/test_seed_models_health_check.py` probes the providers directly; it
proves the catalog names exist upstream. This proves Brutor can actually
serve them: model resolution, the provider adapter's request translation,
response normalisation, and the governance pipeline around the call.

The list is derived from the LLM catalog (`/v1/admin/llm-catalog`) at
collection time, not hand-maintained: every entry of a keyed provider whose
mode is in `conftest.SWEEP_MODES`, minus what the catalog itself marks
unusable (`deprecated`, or a `retire_date` that has passed — see
`conftest.sweep_entry_excluded`). The session setup imports each one through
the catalog import endpoint, keys it, enables it and binds it to E2ETests;
calls use the suite's E2ETests API key.

Outcome per model (`test_gateway_sweep[provider/model]`):
  * PASS   — 2xx with the mode's success shape.
  * XFAIL  — the PROVIDER refused (model retired or not available to this
             key/tier/region, rate limited, overloaded, or the provider's
             own content-policy / safety checker refused the prompt).
             The provider's error is the xfail reason. (Out of credit is
             never an xfail — it FAILS, see `check_out_of_credit`.)
  * FAIL   — anything else: a gateway-side error (Brutor refusal, 5xx from
             Brutor, a provider 400 about a parameter the gateway sent or
             translated, a malformed success body, a timeout).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import pytest

from .conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    CONTROL_URL,
    DEFAULT_TENANT_ID,
    _KEY_TO_PROVIDER,
    _load_provider_keys,
    check_out_of_credit,
    short,
    sweep_entry_excluded,
)
from .test_e2e_smoke import _build_silent_wav


_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_PROMPT = "Reply with the single word OK."
# Unmistakably benign: an abstract prompt ("a small green dot on white")
# tripped fal's content checker on fal-ai/nano-banana-2 (422
# content_policy_violation). A plain still life gives every image model
# nothing to flag.
_IMAGE_PROMPT = "A red apple on a white table, studio photo"


# ─────────────────────────────────────────────────────────────────────
# Collection-time parametrization from the live catalog
# ─────────────────────────────────────────────────────────────────────


def _catalog_sweep_params() -> List[Any]:
    """``pytest.param(entry, id="provider/model")`` per swept catalog entry.
    Empty (→ one clean skip) when the stack or keys are unavailable."""
    keyed = sorted(
        {_KEY_TO_PROVIDER[k] for k in _load_provider_keys() if k in _KEY_TO_PROVIDER}
    )
    if not keyed:
        return []
    try:
        with httpx.Client(base_url=CONTROL_URL, timeout=15.0) as c:
            r = c.post(
                "/v1/admin/users/tenant/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
                      "tenant_id": DEFAULT_TENANT_ID},
            )
            if r.status_code != 200:
                return []
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}
            params = []
            for slug in keyed:
                r = c.get(f"/v1/admin/llm-catalog?provider_key={slug}&limit=500", headers=h)
                if r.status_code != 200:
                    continue
                for e in sorted(r.json().get("catalog") or [], key=lambda x: x.get("model_name") or ""):
                    if sweep_entry_excluded(e) is None:
                        params.append(pytest.param(e, id=f"{slug}/{e['model_name']}"))
            return params
    except httpx.HTTPError:
        return []


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "catalog_entry" in metafunc.fixturenames:
        metafunc.parametrize("catalog_entry", _catalog_sweep_params())


# ─────────────────────────────────────────────────────────────────────
# Outcome classification
# ─────────────────────────────────────────────────────────────────────

# Error text that means the PROVIDER refused this model for this account —
# not something Brutor sent wrong. Matched case-insensitively against the
# error body of a non-2xx response.
_PROVIDER_REFUSAL_MARKERS = (
    # (Billing / credit exhaustion is checked first by check_out_of_credit
    # and FAILS loudly — it is deliberately not in this list.)
    "model_not_found", "does not exist", "no longer available",
    "has been deprecated", "deprecated", "decommissioned", "retired",
    "not available in your", "region", "unsupported_country",
    "do not have access", "does not have access", "not have access to",
    "usage tier", "your tier", "access tier", "requires_tier",
    "organization must be verified", "must be verified",
    "rate limit", "rate_limit", "too many requests", "overloaded",
    "resource_exhausted", "capacity",
)

# Brutor's own refusals — these are never provider-side, whatever else the
# body says (e.g. "Access denied: your resource-group memberships …").
_GATEWAY_MARKERS = (
    "resource-group", "resource group", "brutor", "guardrail",
    "limits for this request", "usage limit", "not bound",
    "only supports the responses endpoint",
)


# The PROVIDER's own content-policy / safety checker refused the prompt.
# The gateway relays these wrapped in its own 4xx/5xx (e.g. the image route
# answers 502 with fal's 422 body or OpenAI's cleaned safety sentence), so
# they are matched at any error status — but only AFTER _GATEWAY_MARKERS, so
# a Brutor guardrail block is still a gateway-side FAIL. Specific provider
# wording only; no bare "blocked" / "safety".
_PROVIDER_CONTENT_POLICY_MARKERS = (
    "content_policy_violation",        # fal, OpenAI (code)
    "flagged by a content checker",    # fal
    "moderation_blocked",              # OpenAI images (code)
    "rejected as a result of our safety system",  # OpenAI (message)
    "responsibleaipolicyviolation",    # Azure OpenAI content filter
    "image_safety", "prohibited_content", "blockreason",  # Gemini
    '"finishreason": "safety"', '"finishreason":"safety"',  # Gemini
)


def _classify_failure(status: int, body: str) -> Optional[str]:
    """Return a provider-side reason (→ xfail), or None (→ gateway-side FAIL)."""
    text = (body or "").lower()
    if any(m in text for m in _GATEWAY_MARKERS):
        return None
    if status >= 400 and any(m in text for m in _PROVIDER_CONTENT_POLICY_MARKERS):
        return f"provider content-policy refusal ({status}): {short(body, 240)}"
    if status in (429, 529):
        return f"provider rate-limited/overloaded ({status}): {short(body, 200)}"
    if status in (401, 402, 403, 404) or 400 <= status < 500:
        if any(m in text for m in _PROVIDER_REFUSAL_MARKERS):
            return f"provider refused ({status}): {short(body, 220)}"
        return None
    # The gateway's 504 for a provider that outlasted the model's timeout
    # ("the image provider did not answer within Ns"). A slow or
    # cold-starting provider app, not a gateway fault; the message names the
    # setting to raise. Only this exact gateway wording counts.
    if status == 504 and "did not answer within" in text:
        return f"provider too slow for the model's timeout ({status}): {short(body, 200)}"
    if status in (502, 503) and any(m in text for m in ("overloaded", "unavailable", "capacity")):
        return f"provider unavailable ({status}): {short(body, 200)}"
    return None


# ─────────────────────────────────────────────────────────────────────
# One minimal call per mode
# ─────────────────────────────────────────────────────────────────────


def _first(values: Any, default: Any) -> Any:
    return values[0] if isinstance(values, list) and values else default


def _call(
    proxy: httpx.Client, headers: Dict[str, str], row: Dict[str, Any]
) -> tuple[httpx.Response, str]:
    """Send the mode's minimal request. Returns (response, what was checked)."""
    name = row["model_name"]
    mode = (row.get("mode") or "chat").lower()
    caps = row.get("capabilities") or {}
    if mode == "responses" or caps.get("requires_responses_api"):
        return proxy.post(
            "/v1/proxy/llm/responses", headers=headers, timeout=_TIMEOUT,
            json={"model": name, "input": _PROMPT, "max_output_tokens": 16},
        ), "responses"
    if mode == "chat":
        return proxy.post(
            "/v1/proxy/llm/chat/completions", headers=headers, timeout=_TIMEOUT,
            # 256, not 16: reasoning models (gpt-5.x, o-series) spend output
            # tokens thinking and OpenAI answers 400 "max_tokens … reached"
            # when nothing is left for the answer.
            json={"model": name, "max_tokens": 256,
                  "messages": [{"role": "user", "content": _PROMPT}]},
        ), "chat"
    if mode == "embedding":
        return proxy.post(
            "/v1/proxy/llm/embeddings", headers=headers, timeout=_TIMEOUT,
            json={"model": name, "input": "ok"},
        ), "embedding"
    if mode == "image_generation":
        body: Dict[str, Any] = {
            "model": name, "prompt": _IMAGE_PROMPT, "n": 1,
            "size": _first(row.get("supported_image_sizes"), "1024x1024"),
        }
        qualities = row.get("supported_image_qualities") or []
        if "low" in qualities:
            body["quality"] = "low"
        return proxy.post(
            "/v1/proxy/llm/images/generations", headers=headers, timeout=_TIMEOUT, json=body,
        ), "image"
    if mode == "audio_speech":
        return proxy.post(
            "/v1/proxy/llm/audio/speech", headers=headers, timeout=_TIMEOUT,
            json={"model": name, "input": "OK.",
                  "voice": _first(row.get("supported_voices"), "alloy"),
                  "response_format": "mp3"},
        ), "tts"
    if mode == "audio_transcription":
        upload = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        return proxy.post(
            "/v1/proxy/llm/audio/transcriptions", headers=upload, timeout=_TIMEOUT,
            files={"file": ("silence.wav", _build_silent_wav(), "audio/wav")},
            data={"model": name, "response_format": "json"},
        ), "stt"
    if mode == "video_generation":
        # Submit only — the smoke tier polls the same model to completion.
        return proxy.post(
            "/v1/proxy/llm/video/generations", headers=headers, timeout=_TIMEOUT,
            json={"model": name, "prompt": "a green dot drifting", "duration_seconds": 5,
                  "resolution": _first(row.get("supported_video_resolutions"), "540p")},
        ), "video"
    pytest.skip(f"mode {mode} has no sweep call")


def _success_problem(kind: str, r: httpx.Response) -> Optional[str]:
    """None when the 2xx body has the mode's success shape."""
    if kind in ("tts",):
        return None if len(r.content) >= 128 else f"audio body too small ({len(r.content)} bytes)"
    try:
        body = r.json()
    except ValueError:
        return f"non-JSON 2xx body: {short(r.text, 160)}"
    if kind == "chat":
        return None if body.get("choices") else f"no choices: {short(r.text, 200)}"
    if kind == "responses":
        return None if ("output" in body or body.get("status")) else f"no output: {short(r.text, 200)}"
    if kind == "embedding":
        data = body.get("data") or []
        ok = data and isinstance(data[0].get("embedding"), list) and data[0]["embedding"]
        return None if ok else f"no embedding vector: {short(r.text, 200)}"
    if kind == "image":
        data = body.get("data") or []
        first = data[0] if data else {}
        ok = first.get("b64_json") or first.get("url") or first.get("brutor_file_id") or body.get("brutor_file_id")
        return None if ok else f"no image payload: {short(r.text, 200)}"
    if kind == "stt":
        return None if isinstance(body.get("text"), str) else f"no text: {short(r.text, 200)}"
    if kind == "video":
        return None if body.get("job_id") else f"no job_id: {short(r.text, 200)}"
    return None


# ─────────────────────────────────────────────────────────────────────
# The sweep
# ─────────────────────────────────────────────────────────────────────


_MEDIA_KINDS = {"image_generation", "audio_speech", "audio_transcription", "video_generation"}


@pytest.fixture(scope="session")
def sweep_headers(e2e_api_key: str) -> Dict[str, str]:
    """Text modes go through the programmatic path: the E2ETests API key."""
    return {"Authorization": f"Bearer {e2e_api_key}", "Content-Type": "application/json"}


def test_gateway_sweep(
    catalog_entry: Dict[str, Any],
    provisioned_models: Dict[str, Dict[str, Dict[str, Any]]],
    proxy_client: httpx.Client,
    sweep_headers: Dict[str, str],
    trial_headers: Dict[str, str],
) -> None:
    slug = catalog_entry["provider_key"]
    name = catalog_entry["model_name"]
    row = (provisioned_models.get(slug) or {}).get(name)
    assert row, f"{slug}/{name} was not provisioned by the session setup"
    assert row.get("enabled"), f"{slug}/{name} is configured but disabled"

    try:
        # The multimodal routes are portal routes: they require the portal
        # user's JWT (core routes/portal_common.rs: "Gateway JWT
        # authentication required" for an API key), so media modes run as
        # the trial user pinned to E2ETests.
        mode = (row.get("mode") or "chat").lower()
        headers = {**trial_headers, "Content-Type": "application/json"} if mode in _MEDIA_KINDS else sweep_headers
        r, kind = _call(proxy_client, headers, row)
    except httpx.TimeoutException as e:
        pytest.fail(f"gateway call timed out ({type(e).__name__}) — no response within 180s")

    if 200 <= r.status_code < 300:
        problem = _success_problem(kind, r)
        assert problem is None, f"{kind} {r.status_code} but malformed: {problem}"
        return

    billing = check_out_of_credit(slug, r.status_code, r.text)
    if billing:
        pytest.fail(billing)
    provider_reason = _classify_failure(r.status_code, r.text)
    if provider_reason:
        pytest.xfail(provider_reason)
    pytest.fail(
        f"gateway-side failure on {kind} ({r.status_code}): {short(r.text, 400)} "
        f"[x-correlation-id={r.headers.get('x-correlation-id')}]"
    )
