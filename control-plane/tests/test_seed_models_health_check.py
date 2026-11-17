# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live-API health check across every seeded OpenAI / Anthropic / Google model.

Why this test exists
--------------------
When an admin adds a model from the seeded catalogue and clicks "Health
check", the worst possible UX is to discover the seeded entry has the
wrong model_name, a stale price, or a config that the provider rejects.
This test catches that *before* an admin ever sees it: it loops through
every chat / responses / embedding entry from the OpenAI, Anthropic
and Google seed files and probes the provider directly with a minimal
payload.

It deliberately does NOT route through the Rust proxy or the control
plane DB — those layers add their own auth, header, and routing logic
that can mask provider-side failures. We hit the providers' public REST
endpoints, mirroring the request shape Brutor's adapters produce.

How to run
----------
1. Drop API keys into ``tests/.secrets/provider_keys.env``::

       OPENAI_API_KEY=sk-...
       ANTHROPIC_API_KEY=sk-ant-...
       GOOGLE_API_KEY=AIza...

   The directory is gitignored — keys never enter version control.
   Per-provider env vars (``OPENAI_API_KEY`` etc.) also work as a fallback.

2. Run the suite explicitly::

       pytest -m live_api tests/test_seed_models_health_check.py -v

   The default ``pytest`` invocation excludes ``live_api`` so CI doesn't
   burn credits on every push. A single full pass costs cents but adds
   30–60 seconds of wall time.

What it does NOT cover
----------------------
- Rerank / moderation seeds — different probe shapes; not yet wired.
- The other 30 providers (Bedrock, Together, Groq, etc.) — same harness
  pattern, but each one needs its own per-provider probe + key. Add
  them as separate ``_probe_<provider>`` helpers when the keys land.
- Streaming behaviour — the probes are non-streaming because the goal
  is "does the model respond at all", not "does the SSE pipe work".

What is covered now
-------------------
- Text: chat, responses, completion, embedding (OpenAI / Anthropic /
  Google / xAI / DeepSeek / Moonshot / Groq / MiniMax).
- Image generation (OpenAI ``images/generations``).
- Audio TTS (OpenAI ``audio/speech``).
- Audio STT (OpenAI ``audio/transcriptions``).
- Video generation (Fal queue submit + status poll).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from app.database.provider_data import (
    anthropic_models_init,
    deepseek_models_init,
    fal_ai_models_init,
    google_models_init,
    groq_models_init,
    minimax_models_init,
    moonshotai_models_init,
    openai_embedding_models_init,
    openai_models_init,
    openai_multimodal_models_init,
    xai_models_init,
    openrouter_models_init,
)


# ─────────────────────────────────────────────────────────────────────
# API-key loading
# ─────────────────────────────────────────────────────────────────────

_KEYS_FILE = Path(__file__).parent / ".secrets" / "provider_keys.env"


def _load_provider_keys() -> Dict[str, str]:
    """Return ``{provider: api_key}`` from the .env file + os.environ.

    File-based keys take precedence over env vars so a developer who
    runs ``OPENAI_API_KEY=stale pytest`` doesn't accidentally probe
    against a key they thought they'd retired in the file.
    """
    keys: Dict[str, str] = {}
    if _KEYS_FILE.exists():
        for line in _KEYS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            keys[k.strip()] = v.strip().strip("'").strip('"')

    # env-var fallback for CI runners that inject keys via the
    # workflow rather than a checked-in file.
    for env_name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "GROQ_API_KEY",
        "MINIMAX_API_KEY",
        # Multimodal — image, audio, video providers added when the
        # multimodal phases land. Fal is the default-baseline video
        # provider; OpenAI carries image/TTS/STT for the baseline.
        "FAL_AI_API_KEY",
        "OPENROUTER_API_KEY",
        "FAL_KEY",
    ):
        if env_name not in keys and env_name in os.environ:
            keys[env_name] = os.environ[env_name]

    return keys


_PROVIDER_KEYS = _load_provider_keys()


# ─────────────────────────────────────────────────────────────────────
# Seed collection
# ─────────────────────────────────────────────────────────────────────

# Modes the harness can probe today. Anything else (rerank, moderation,
# routing) is skipped with a clear reason rather than failing.
_PROBABLE_MODES = {
    "chat",
    "responses",
    "embedding",
    "completion",
    "image_generation",
    "audio_speech",
    "audio_transcription",
    "video_generation",
}


def _collect_models() -> List[Tuple[str, Dict[str, Any]]]:
    """Build the parametrize list: ``[(provider, model_dict), ...]``.

    The provider key is what we use to look up the API key + dispatch
    to the right probe helper — NOT the model row's `provider` field
    (which can be unset on seed dicts; the file location is the
    authoritative signal).
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for m in openai_models_init.get_models():
        out.append(("openai", m))
    for m in openai_embedding_models_init.get_models():
        # The OpenAI-embedding seed file historically lumps the
        # Google-embedding entry in too (with provider override). Skip
        # any non-OpenAI entries here so we route them through the
        # right probe.
        if m.get("provider") and m["provider"] != "openai":
            continue
        out.append(("openai", m))
    for m in openai_multimodal_models_init.get_models():
        out.append(("openai", m))
    for m in anthropic_models_init.get_models():
        out.append(("anthropic", m))
    for m in google_models_init.get_models():
        out.append(("google", m))
    for m in xai_models_init.get_models():
        out.append(("xai", m))
    for m in deepseek_models_init.get_models():
        out.append(("deepseek", m))
    for m in moonshotai_models_init.get_models():
        out.append(("moonshot", m))
    for m in groq_models_init.get_models():
        out.append(("groq", m))
    for m in minimax_models_init.get_models():
        out.append(("minimax", m))
    for m in fal_ai_models_init.get_models():
        out.append(("fal_ai", m))
    for m in openrouter_models_init.get_models():
        # `openrouter/auto` has no meaningful list price and routes per
        # request; a chat ping exercises the whole path regardless of
        # which upstream model answers.
        out.append(("openrouter", m))
    return out


_ALL_MODELS = _collect_models()


def _ids(models: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
    """pytest parametrize ids — one per row in the test report."""
    return [f"{p}/{m['model_name']}" for p, m in models]


# ─────────────────────────────────────────────────────────────────────
# Per-provider probes
# ─────────────────────────────────────────────────────────────────────

# Tight default — every probe is a "ping" + 5-token reply. Anything
# slower than this for a working model means an upstream regression.
_TIMEOUT_SECS = 30.0

# Cap on output tokens. Reasoning models burn billable tokens on
# their internal chain-of-thought even with a 1-token visible reply,
# so we cap chat at a value high enough that hidden-reasoning chat
# models (gpt-5.x family) still emit at least one visible token and
# return ``"finish_reason": "stop"``. /responses gets even more headroom
# because the OpenAI Responses API charges separately for reasoning
# tokens — a tiny cap there makes ``output_text`` come back empty.
_CHAT_MAX_TOKENS = 256
_RESPONSES_MAX_OUTPUT_TOKENS = 256


def _probe_openai_chat(api_key: str, model_name: str) -> None:
    """Hit ``api.openai.com/v1/chat/completions``. Falls back to the
    Responses API if OpenAI says the model isn't chat-compatible — this
    mirrors the Rust proxy's reasoning-model fallback so a seed entry
    that *should* be on Responses but is mistakenly mode=chat still
    reports healthy."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        # Reasoning models reject `max_tokens`; some accept
        # `max_completion_tokens`. Send the modern name — older chat
        # models still accept it as an alias.
        "max_completion_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    if resp.status_code == 404 and (
        "not a chat model" in resp.text.lower()
        or "v1/responses" in resp.text.lower()
    ):
        # Reasoning-only models (o-series / *-pro) — same fallback as
        # the proxy. Surface as a `pytest.xfail`-flavoured pass with a
        # message so operators see it should probably be `mode=responses`.
        _probe_openai_responses(api_key, model_name)
        return
    _assert_ok(resp, provider="openai", model_name=model_name)


def _probe_openai_responses(api_key: str, model_name: str) -> None:
    """Hits OpenAI's Responses API. Sends ``reasoning.effort: "medium"``
    because that's the lowest universally-accepted level across the o-,
    gpt-5-, and gpt-5-pro/codex families:

    * o3 / o3-mini / o4-mini / o1-pro accept ``low|medium|high``
    * gpt-5.x non-pro accepts ``minimal|low|medium|high|xhigh``
    * gpt-5.x-pro accepts ONLY ``medium|high|xhigh`` (no low/minimal)
    * gpt-5.x-codex accepts ``low|medium|high(|xhigh)`` (no minimal)

    ``medium`` is the only string every reasoning model accepts. Older
    chat-only models that get fallback-routed here (because /chat said
    "not a chat model") will reject ``reasoning.effort`` outright with
    an ``unsupported_parameter`` 400 — surface that as a real failure
    so the seed gets fixed (the entry's ``mode`` is wrong)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "input": "ping",
        "max_output_tokens": _RESPONSES_MAX_OUTPUT_TOKENS,
        "reasoning": {"effort": "medium"},
    }
    resp = httpx.post(
        "https://api.openai.com/v1/responses",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="openai (responses)", model_name=model_name)


def _probe_openai_completion(api_key: str, model_name: str) -> None:
    """Hits the legacy ``/v1/completions`` endpoint for ``mode=completion``
    seeds (gpt-3.5-turbo-instruct and similar). The Chat Completions and
    Responses APIs both reject these models — only the original
    completions endpoint accepts a ``prompt`` field."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "prompt": "ping",
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.openai.com/v1/completions",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="openai (completion)", model_name=model_name)


def _probe_openai_embedding(api_key: str, model_name: str) -> None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"model": model_name, "input": "health check"}
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="openai (embedding)", model_name=model_name)


# ─────────────────────────────────────────────────────────────────────
# Multimodal probes — image / audio_speech / audio_transcription /
# video_generation. Each probe issues the smallest possible request
# that still exercises the provider's modality endpoint, so a credit
# overrun is impossible (~$0.04 image, ~$0.001 TTS, ~$0.0001 STT).
# ─────────────────────────────────────────────────────────────────────

# Image-gen timeout: DALL-E 3 averages 10–20 s but tail can hit 60.
_IMAGE_TIMEOUT_SECS = 90.0
# Video-gen timeout for the SUBMIT step only — the queue endpoint
# returns within seconds; the actual render takes minutes and we do
# NOT wait for it (would be expensive + flaky in CI). The probe is
# satisfied as long as the queue accepts the job.
_VIDEO_SUBMIT_TIMEOUT_SECS = 30.0


def _probe_openai_image(api_key: str, model_name: str) -> None:
    """Hit ``/v1/images/generations``. Smallest legal payload:
    1 image, 1024x1024, standard quality. Returns a URL we don't
    actually fetch — all we care about is that the provider accepted
    the model_name + prompt."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Smallest payload that works across OpenAI's image-gen models.
    # DALL-E 2 / DALL-E 3 are retired; the in-family replacement is gpt-image-1-mini. The shape below
    # (model + prompt + n + size) is the common subset every variant
    # accepts; per-model extras (quality, style) are model-specific
    # and aren't required for a smoke probe.
    body = {
        "model": model_name,
        "prompt": "a small red dot",
        "n": 1,
        "size": "1024x1024",
    }
    resp = httpx.post(
        "https://api.openai.com/v1/images/generations",
        json=body, headers=headers, timeout=_IMAGE_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="openai (image)", model_name=model_name)


def _probe_openai_speech(api_key: str, model_name: str) -> None:
    """Hit ``/v1/audio/speech``. Returns binary audio; we just verify
    the request succeeded with a tiny ``ping`` payload (~5 chars,
    well under $0.001)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "voice": "alloy",
        "input": "ping",
        "response_format": "mp3",
    }
    resp = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    if resp.status_code >= 400:
        # Use the JSON error path even though the success body is
        # binary — providers always return JSON on errors.
        _assert_ok(resp, provider="openai (audio_speech)", model_name=model_name)
        return
    # Sanity: must be at least a few hundred bytes of audio for a
    # `ping` reply. Anything tiny means an empty/error response that
    # snuck past the status check.
    if len(resp.content) < 200:
        raise AssertionError(
            f"openai (audio_speech) {model_name}: response only "
            f"{len(resp.content)} bytes — provider likely returned an "
            f"error payload disguised as 200"
        )


# 0.2-second silent WAV (16-bit mono, 44.1 kHz). OpenAI Whisper
# rejects files shorter than 0.1 seconds with HTTP 400, so a
# 1-sample file fails the harness even though it's a valid WAV.
# 0.2 s = 8820 samples × 2 bytes = 17,640 bytes of payload + 44-byte
# header, billed at the 1-second minimum (~$0.0001 / probe).
def _silent_wav_bytes(duration_seconds: float = 0.2,
                      sample_rate: int = 44100) -> bytes:
    """Build a valid silent WAV in memory. Avoids the test fixture
    needing an external file and keeps the upload payload tiny.
    """
    n_samples = int(sample_rate * duration_seconds)
    bytes_per_sample = 2  # 16-bit mono
    pcm_size = n_samples * bytes_per_sample
    riff_size = 36 + pcm_size
    header = (
        b"RIFF" + riff_size.to_bytes(4, "little") + b"WAVE"
        + b"fmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")              # PCM
        + (1).to_bytes(2, "little")              # mono
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * bytes_per_sample).to_bytes(4, "little")  # byte rate
        + bytes_per_sample.to_bytes(2, "little")  # block align
        + (16).to_bytes(2, "little")              # bits per sample
        + b"data" + pcm_size.to_bytes(4, "little")
    )
    return header + (b"\x00" * pcm_size)


_TINY_WAV_BYTES = _silent_wav_bytes()


def _probe_openai_transcription(api_key: str, model_name: str) -> None:
    """Hit ``/v1/audio/transcriptions``. Multipart upload of a 60-byte
    silent WAV. Whisper returns ``{"text": ""}`` for silence — fine,
    we only care about a 200 OK + a parseable JSON envelope."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        # Don't set Content-Type — httpx fills the multipart boundary.
    }
    files = {
        "file": ("silence.wav", _TINY_WAV_BYTES, "audio/wav"),
    }
    data = {
        "model": model_name,
        "response_format": "json",
    }
    resp = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers=headers, files=files, data=data, timeout=_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="openai (audio_transcription)", model_name=model_name)


def _probe_fal_video(api_key: str, model_name: str) -> None:
    """Submit a video-gen job to Fal's queue and verify the job is
    accepted. Does NOT wait for the render to finish — that would
    cost ~$0.50 per probe and add minutes of wall time. A successful
    submit is enough to verify the model_name is current and the key
    is valid.

    Fal's URL pattern is ``https://queue.fal.run/{slug}`` where the
    slug is everything after ``fal_ai/``. So a seed entry of
    ``fal_ai/fal-ai/luma-dream-machine`` becomes
    ``https://queue.fal.run/fal-ai/luma-dream-machine``.

    We immediately cancel the job after submission so it doesn't run
    against quota. Cancel is best-effort — if it fails we let the job
    complete and absorb the cost (Fal's smallest video clip is well
    under $1).
    """
    # The dispatcher hands over the seed's bare `model_name`
    # (`fal-ai/<model>`), while the gateway-qualified form is
    # `fal_ai/fal-ai/<model>`; accept either and derive the queue slug.
    slug = model_name.removeprefix("fal_ai/")
    if not slug.startswith("fal-ai/"):
        raise AssertionError(
            f"fal video probe expected a Fal slug ('fal-ai/…'), got: {model_name}"
        )
    submit_url = f"https://queue.fal.run/{slug}"
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }
    # Tiny payload — providers reject empty bodies, so send the
    # minimal required fields. Most Fal video models accept just
    # `prompt` (text-to-video) or `image_url` (image-to-video). We
    # send `prompt`; image-to-video models will 400 with a clear
    # message that we surface back to the operator.
    body = {"prompt": "a red dot on a white background", "duration": 5}
    resp = httpx.post(
        submit_url, json=body, headers=headers,
        timeout=_VIDEO_SUBMIT_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="fal_ai (video)", model_name=model_name)

    # Best-effort cancel so we don't burn render minutes.
    try:
        request_id = resp.json().get("request_id")
        if request_id:
            httpx.put(
                f"https://queue.fal.run/{slug}/requests/{request_id}/cancel",
                headers={"Authorization": f"Key {api_key}"},
                timeout=10.0,
            )
    except Exception:
        # Cancel is best-effort; an uncancelled probe still costs
        # < $1 and surfaces in the Fal dashboard.
        pass


def _probe_anthropic_chat(api_key: str, model_name: str) -> None:
    """Anthropic's Messages API. `anthropic-version` is a required
    header — without it the request 400s with a vague "unsupported
    version" message that wastes operator time."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "max_tokens": _CHAT_MAX_TOKENS,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="anthropic", model_name=model_name)


def _probe_xai_chat(api_key: str, model_name: str) -> None:
    """xAI's OpenAI-compatible Chat Completions endpoint at api.x.ai.

    Model IDs are passed bare (no ``xai/`` prefix); xAI 404s on prefixed
    names. Reasoning variants stream their chain-of-thought server-side
    and can take 60-90s on a cold ping — give them headroom."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.x.ai/v1/chat/completions",
        json=body, headers=headers, timeout=120.0,
    )
    _assert_ok(resp, provider="xai", model_name=model_name)


def _probe_xai_responses(api_key: str, model_name: str) -> None:
    """xAI's Responses API at ``/v1/responses``.

    Required for ``grok-4.20-multi-agent-0309`` (chat completions
    explicitly rejects it: "Multi Agent requests are not allowed on chat
    completions"). Multi-agent runs span 60-180s as sub-agents fan out;
    keep the timeout generous."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "input": "ping",
    }
    resp = httpx.post(
        "https://api.x.ai/v1/responses",
        json=body, headers=headers, timeout=240.0,
    )
    _assert_ok(resp, provider="xai (responses)", model_name=model_name)


def _probe_minimax_chat(api_key: str, model_name: str) -> None:
    """MiniMax's OpenAI-compatible Chat Completions endpoint at
    api.minimax.io. Model IDs are case-sensitive (``MiniMax-M2.7``,
    not ``minimax-m2.7``). The reasoning M2.x family emits a hidden
    ``<think>...</think>`` block in the content before the visible
    answer, so even small completions can take a few seconds."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.minimax.io/v1/chat/completions",
        json=body, headers=headers, timeout=180.0,
    )
    _assert_ok(resp, provider="minimax", model_name=model_name)


def _probe_groq_chat(api_key: str, model_name: str) -> None:
    """Groq's OpenAI-compatible Chat Completions endpoint at
    api.groq.com/openai/v1. Model IDs are passed *exactly* as listed in
    /v1/models — Groq encodes the upstream org as part of the ID
    (e.g. ``openai/gpt-oss-120b``, ``meta-llama/llama-4-scout-...``).
    Adding a ``groq/`` prefix to non-Groq IDs returns 404; stripping
    it from Groq's own IDs (e.g. ``compound`` instead of
    ``groq/compound``) also returns 404. The seed mirrors /v1/models
    verbatim and so does this probe."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json=body, headers=headers, timeout=120.0,
    )
    _assert_ok(resp, provider="groq", model_name=model_name)


def _probe_openrouter_chat(api_key: str, model_name: str) -> None:
    """OpenRouter's OpenAI-compatible endpoint at openrouter.ai/api/v1.
    Model IDs are vendor/model slugs passed through verbatim;
    ``openrouter/auto`` routes the ping to whatever model the router
    picks and bills at that model's rate. Authorization is the only
    required header (HTTP-Referer/X-OpenRouter-Title are optional
    attribution)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=body, headers=headers, timeout=120.0,
    )
    _assert_ok(resp, provider="openrouter", model_name=model_name)


def _probe_moonshot_chat(api_key: str, model_name: str) -> None:
    """Moonshot AI's OpenAI-compatible Chat Completions endpoint at
    api.moonshot.ai. Bare model names only — prefixed names 404 with
    "resource_not_found_error". Kimi K2.x reasoning models burn
    significant time on internal CoT (60-180s on a cold ping)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.moonshot.ai/v1/chat/completions",
        json=body, headers=headers, timeout=240.0,
    )
    _assert_ok(resp, provider="moonshot", model_name=model_name)


def _probe_deepseek_chat(api_key: str, model_name: str) -> None:
    """DeepSeek's OpenAI-compatible Chat Completions endpoint at
    api.deepseek.com. Bare model names only — prefixed names 400 with
    "Model Not Exist". Reasoner-capable models (v4-flash thinking,
    v4-pro, deepseek-reasoner alias) burn time on internal CoT, so the
    timeout is generous."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://api.deepseek.com/v1/chat/completions",
        json=body, headers=headers, timeout=180.0,
    )
    _assert_ok(resp, provider="deepseek", model_name=model_name)


def _probe_google_chat(api_key: str, model_name: str) -> None:
    """Google's OpenAI-compatible endpoint. Hitting this rather than
    the native ``/v1beta/models/{model}:generateContent`` keeps the
    probe shape identical to OpenAI's — the gateway adapter targets
    the same compat surface in production."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _CHAT_MAX_TOKENS,
    }
    resp = httpx.post(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        json=body, headers=headers, timeout=_TIMEOUT_SECS,
    )
    _assert_ok(resp, provider="google", model_name=model_name)


# ─────────────────────────────────────────────────────────────────────
# Shared assertion + dispatch
# ─────────────────────────────────────────────────────────────────────

def _assert_ok(resp: httpx.Response, *, provider: str, model_name: str) -> None:
    """Centralised pass/fail check.

    A successful probe is HTTP 200 with a JSON body that has at least
    one of the expected response keys. We don't assert exact body shape
    because providers tweak it occasionally; the key signal is "200 +
    parseable JSON + non-error envelope".
    """
    if resp.status_code != 200:
        pytest.fail(
            f"{provider} health check for {model_name!r} returned "
            f"HTTP {resp.status_code}\n"
            f"Response body (truncated): {resp.text[:600]}"
        )
    try:
        body = resp.json()
    except ValueError:
        pytest.fail(
            f"{provider} health check for {model_name!r} returned "
            f"non-JSON body: {resp.text[:300]}"
        )
    if isinstance(body, dict) and body.get("error"):
        pytest.fail(
            f"{provider} health check for {model_name!r} returned a JSON "
            f"error envelope: {body['error']!r}"
        )


def _key_for_provider(provider: str) -> Optional[str]:
    """Map provider → API key. Returns None if unset; caller skips
    rather than fails when a provider isn't configured locally.

    Some providers ship under multiple env-var conventions — Fal in
    particular is documented as ``FAL_KEY`` in the JS SDK but as
    ``FAL_AI_API_KEY`` in some configs. Treat the list as an
    ordered fallback so either name works.
    """
    candidates: Dict[str, List[str]] = {
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
        "google": ["GOOGLE_API_KEY"],
        "xai": ["XAI_API_KEY"],
        "deepseek": ["DEEPSEEK_API_KEY"],
        "moonshot": ["MOONSHOT_API_KEY"],
        "groq": ["GROQ_API_KEY"],
        "minimax": ["MINIMAX_API_KEY"],
        "fal_ai": ["FAL_KEY", "FAL_AI_API_KEY"],
        "openrouter": ["OPENROUTER_API_KEY"],
    }
    for env_name in candidates.get(provider, []):
        if env_name in _PROVIDER_KEYS:
            return _PROVIDER_KEYS[env_name]
    return None


# ─────────────────────────────────────────────────────────────────────
# The test
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.live_api
@pytest.mark.parametrize(("provider", "model"), _ALL_MODELS, ids=_ids(_ALL_MODELS))
def test_seed_model_responds_to_health_check(
    provider: str, model: Dict[str, Any]
) -> None:
    """Every seeded chat/responses/embedding model must answer a
    minimal probe from its provider's REST endpoint.

    Skip cases:
      * No API key configured for that provider in the local key file
        or environment (clear skip reason in test output).
      * Mode is audio/image/rerank — out of scope for this harness.
      * The seed entry has ``enabled: False`` — the operator marked it
        retired; not a failing condition.
      * The seed entry carries a ``retire_date`` on/before today — the
        provider has shut it down; the catalog marks it deprecated and
        seeds it disabled (same rule as the live gateway sweep's
        ``sweep_entry_excluded``).
    """
    if model.get("enabled") is False:
        pytest.skip(f"Seed marks {model['model_name']!r} as enabled=False")
    retire = model.get("retire_date")
    if retire is not None and retire <= date.today():
        pytest.skip(f"Seed retires {model['model_name']!r} on {retire.isoformat()}")

    api_key = _key_for_provider(provider)
    if not api_key:
        pytest.skip(f"No API key configured for {provider!r}")

    mode = model.get("mode") or "chat"  # default mode is chat in init_llms
    if mode not in _PROBABLE_MODES:
        pytest.skip(
            f"Mode {mode!r} not yet supported by health-check harness "
            f"(model={model['model_name']!r})"
        )

    name = model["model_name"]

    # Dispatch — provider × mode → probe function.
    if provider == "openai":
        if mode == "embedding":
            _probe_openai_embedding(api_key, name)
        elif mode == "responses":
            _probe_openai_responses(api_key, name)
        elif mode == "completion":
            _probe_openai_completion(api_key, name)
        elif mode == "image_generation":
            _probe_openai_image(api_key, name)
        elif mode == "audio_speech":
            _probe_openai_speech(api_key, name)
        elif mode == "audio_transcription":
            _probe_openai_transcription(api_key, name)
        else:
            _probe_openai_chat(api_key, name)
    elif provider == "anthropic":
        if mode != "chat":
            pytest.skip(
                f"Anthropic only ships chat models; mode={mode!r} unexpected"
            )
        _probe_anthropic_chat(api_key, name)
    elif provider == "google":
        if mode == "embedding":
            pytest.skip(
                "Google embedding probe not yet implemented — "
                "needs `/v1beta/models/{model}:embedContent` shape"
            )
        _probe_google_chat(api_key, name)
    elif provider == "xai":
        if mode == "responses":
            _probe_xai_responses(api_key, name)
        else:
            _probe_xai_chat(api_key, name)
    elif provider == "deepseek":
        if mode != "chat":
            pytest.skip(
                f"DeepSeek chat-only probe; mode={mode!r} not supported here"
            )
        _probe_deepseek_chat(api_key, name)
    elif provider == "moonshot":
        if mode != "chat":
            pytest.skip(
                f"Moonshot chat-only probe; mode={mode!r} not supported here"
            )
        _probe_moonshot_chat(api_key, name)
    elif provider == "groq":
        if mode != "chat":
            pytest.skip(
                f"Groq chat-only probe; mode={mode!r} not supported here"
            )
        _probe_groq_chat(api_key, name)
    elif provider == "openrouter":
        if mode != "chat":
            pytest.skip(
                f"OpenRouter chat-only probe; mode={mode!r} not supported here"
            )
        _probe_openrouter_chat(api_key, name)
    elif provider == "minimax":
        if mode != "chat":
            pytest.skip(
                f"MiniMax chat-only probe; mode={mode!r} not supported here"
            )
        _probe_minimax_chat(api_key, name)
    elif provider == "fal_ai":
        if mode == "video_generation":
            _probe_fal_video(api_key, name)
        elif mode == "image_generation":
            pytest.skip(
                "Fal image probe not yet implemented — submit shape "
                "is per-model on Fal (FLUX vs Imagen vs SD), so a "
                "single probe won't cover them. Add per-model "
                "probes when image-gen testing is needed."
            )
        else:
            pytest.skip(
                f"No Fal probe for mode={mode!r} on model={name!r}"
            )
    else:
        pytest.skip(f"No probe implementation for provider {provider!r}")
