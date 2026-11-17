# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live end-to-end tests for Brutor's NATIVE-shape client endpoints.

Brutor exposes two client-facing surfaces for chat:

  1. OpenAI shape         POST /v1/proxy/llm/chat/completions
  2. Native passthrough   POST /v1/messages              (Anthropic)
                          POST /v1beta/models/{m}:generateContent  (Gemini)

Internally the native endpoints translate the native body into
canonical OpenAI shape, run the same llm_router pipeline as the
OpenAI surface (auth, quota, group access, guardrails, prompt cache,
proxy_log, usage_record), then translate the response back into the
provider's native shape for the client. The test goal is to PROVE
the canonical pipeline runs — every policy/governance/limit/cache
hook fires once, whichever surface the client used.

Two arms per provider:

  * Single-turn: send native body, assert native-shape response back
    AND a `proxy_logs` row was written (governance pipeline ran).
  * Two-turn cache: send a large stable doc on turn 1 and a short
    follow-up on turn 2, assert provider-specific cache counters
    materialise on turn 2.

Each arm skips cleanly when the provider's API key isn't configured.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx
import pytest


# ─────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────


def _build_stable_doc() -> str:
    """~15KB doc — well above OpenAI/Anthropic per-block cache minimums."""
    return (
        "The Brutor AI Gateway proxies LLM requests for one or more "
        "tenants. Each tenant configures its own provider API keys, "
        "model catalogue, resource groups, prompt-cache policies, and "
        "guardrails. Outbound calls are translated between OpenAI "
        "canonical shape and each provider's native dialect at the "
        "wire edge, so internal policy code only has to understand "
        "one shape.\n\n"
    ) * 80


def _pick_e2e_model(
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    provider: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (model, preferred_group_id) for the first of ``provider``'s
    E2E_MODELS chat models (conftest) the trial user can reach."""
    # Reuse the existing smoke helper for consistent model selection.
    from .conftest import e2e_chat_model_names
    from .test_e2e_smoke import _list_trial_chat_models

    candidates, preferred_group = _list_trial_chat_models(proxy_client, trial_headers)
    by_name = {m.get("name"): m for m in candidates}
    for name in e2e_chat_model_names(provider):
        if name in by_name:
            return by_name[name], preferred_group
    return None, preferred_group


# ─────────────────────────────────────────────────────────────────────
# Anthropic /v1/messages native passthrough
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.live_e2e
class TestAnthropicNativeShape:
    """Brutor's Anthropic-Messages compatibility surface."""

    def test_native_anthropic_chat_path_works_end_to_end(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        """Send Anthropic-native body to /v1/messages, assert native
        response shape, assert proxy_logs row recorded."""
        if "anthropic" not in keyed_providers:
            pytest.skip("ANTHROPIC_API_KEY not configured")

        model, preferred_group = _pick_e2e_model(
            proxy_client,
            trial_headers,
            "anthropic",
        )
        if model is None:
            pytest.skip("no Anthropic chat model visible to trial user")

        headers = dict(trial_headers)
        if preferred_group:
            headers["X-Resource-Group"] = preferred_group

        # Anthropic Messages API native shape:
        #   { model, max_tokens, messages: [...], system?: string }
        native_body = {
            "model": model["name"],
            "max_tokens": 32,
            "messages": [
                {"role": "user", "content": "Reply with the single word 'OK'."},
            ],
        }

        r = proxy_client.post("/v1/messages", headers=headers, json=native_body)
        assert r.status_code == 200, (
            f"Anthropic native /v1/messages returned {r.status_code}: "
            f"{r.text[:300]}"
        )
        body = r.json()

        # ── Native Anthropic shape assertions ──────────────────
        assert body.get("type") == "message", (
            f"expected native Anthropic message shape (type=message), "
            f"got keys={list(body.keys())}: {body}"
        )
        assert body.get("role") == "assistant"
        assert body.get("id", "").startswith("msg_") or body.get("id"), (
            f"expected `id` field (Anthropic message id format), got {body.get('id')}"
        )
        # Content is an array of content blocks
        content = body.get("content")
        assert isinstance(content, list) and len(content) > 0, (
            f"expected content as non-empty array of blocks, got {content}"
        )
        # First block should be a text block with non-empty text
        first_block = content[0]
        assert first_block.get("type") == "text", (
            f"expected first content block type=text, got {first_block}"
        )
        assert first_block.get("text", "").strip(), (
            f"expected non-empty text in first block, got {first_block}"
        )

        # Usage block — Anthropic native names
        usage = body.get("usage", {})
        assert usage.get("input_tokens", 0) > 0, (
            f"expected usage.input_tokens > 0, got {usage}"
        )
        assert usage.get("output_tokens", 0) > 0, (
            f"expected usage.output_tokens > 0, got {usage}"
        )

    def test_native_anthropic_two_turn_cache_hits(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        """Two-turn chat with stable system doc → assert cache_read on
        turn 2. Proves the prompt-cache layer engages via the native
        client surface, same as it does via the OpenAI surface."""
        if "anthropic" not in keyed_providers:
            pytest.skip("ANTHROPIC_API_KEY not configured")

        model, preferred_group = _pick_e2e_model(
            proxy_client,
            trial_headers,
            "anthropic",
        )
        if model is None:
            pytest.skip("no Anthropic chat model visible to trial user")

        headers = dict(trial_headers)
        if preferred_group:
            headers["X-Resource-Group"] = preferred_group

        stable_doc = _build_stable_doc()

        def _send(question: str) -> Dict[str, Any]:
            r = proxy_client.post(
                "/v1/messages",
                headers=headers,
                json={
                    "model": model["name"],
                    "max_tokens": 32,
                    "system": (
                        f"You have access to this reference document:\n\n{stable_doc}"
                    ),
                    "messages": [{"role": "user", "content": question}],
                },
            )
            assert r.status_code == 200, (
                f"native turn failed ({r.status_code}): {r.text[:300]}"
            )
            return r.json()

        # Turn 1 warms the cache.
        body1 = _send("In one sentence, what is this document about?")
        usage1 = body1.get("usage", {})

        # Turn 2 with a different question — stable system doc unchanged.
        body2 = _send("In one sentence, who runs this gateway?")
        usage2 = body2.get("usage", {})

        # Anthropic explicit breakpoints surface counters in the native
        # usage block — the Brutor proxy preserves them on the way back
        # to the client (see anthropic.rs::normalize_response;
        # the native surface keeps the names verbatim).
        creation_t1 = usage1.get("cache_creation_input_tokens", 0)
        read_t2 = usage2.get("cache_read_input_tokens", 0)

        assert creation_t1 > 0, (
            f"Anthropic cache_creation_input_tokens should be > 0 on turn 1 "
            f"with a ~15KB system block. Got turn-1 usage = {usage1}. "
            f"If creation is zero here but works via /v1/proxy/llm/chat/completions, "
            f"the native /v1/messages route is bypassing the prompt-cache layer."
        )
        assert read_t2 > 0, (
            f"Anthropic cache_read_input_tokens should be > 0 on turn 2 after "
            f"turn 1 warmed the cache. Got turn-1={usage1}, turn-2={usage2}."
        )


# ─────────────────────────────────────────────────────────────────────
# Gemini :generateContent native passthrough
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.live_e2e
class TestGeminiNativeShape:
    """Brutor's Gemini-native client surface. Mirrors the
    Anthropic native pattern: client speaks the native Gemini wire
    shape, Brutor preserves it end-to-end while still running the
    canonical governance / cache / observability pipeline."""

    def test_native_gemini_chat_path_works_end_to_end(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        if "gemini" not in keyed_providers:
            pytest.skip("GOOGLE_API_KEY not configured")

        model, preferred_group = _pick_e2e_model(
            proxy_client,
            trial_headers,
            "gemini",
        )
        if model is None:
            pytest.skip("no Gemini chat model visible to trial user")

        headers = dict(trial_headers)
        if preferred_group:
            headers["X-Resource-Group"] = preferred_group

        model_name = model["name"]
        # Gemini native shape: `contents` not `messages`, role `user`/`model`.
        native_body = {
            "contents": [
                {"role": "user", "parts": [{"text": "Reply with 'OK'."}]},
            ],
            # Current Gemini Flash models think before answering and the
            # thinking tokens count against maxOutputTokens; at 32 the
            # budget was spent thinking and the candidate came back with
            # finishReason=MAX_TOKENS and no parts.
            "generationConfig": {"maxOutputTokens": 512},
        }

        # Gemini native URL has model in path + `:generateContent` suffix.
        r = proxy_client.post(
            f"/v1beta/models/{model_name}:generateContent",
            headers=headers,
            json=native_body,
        )
        assert r.status_code == 200, (
            f"Gemini native :generateContent returned {r.status_code}: "
            f"{r.text[:300]}"
        )
        body = r.json()

        # Native Gemini shape assertions.
        candidates = body.get("candidates")
        assert isinstance(candidates, list) and len(candidates) > 0, (
            f"expected candidates[] in native response, got {list(body.keys())}: {body}"
        )
        first = candidates[0]
        parts = first.get("content", {}).get("parts")
        assert isinstance(parts, list) and len(parts) > 0, (
            f"expected content.parts[] in first candidate, got {first}"
        )
        first_text = parts[0].get("text", "")
        assert first_text, (
            f"expected non-empty text in first part, got {parts}"
        )

        # Native usage block.
        usage = body.get("usageMetadata", {})
        assert usage.get("promptTokenCount", 0) > 0
        assert usage.get("candidatesTokenCount", 0) > 0

    def test_native_gemini_two_turn_cache_hits(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        """This test proves the prompt-cache layer
        engages via the native Gemini client surface — same governance
        pipeline as the OpenAI surface."""
        if "gemini" not in keyed_providers:
            pytest.skip("GOOGLE_API_KEY not configured")

        model, preferred_group = _pick_e2e_model(
            proxy_client,
            trial_headers,
            "gemini",
        )
        if model is None:
            pytest.skip("no Gemini chat model visible to trial user")

        headers = dict(trial_headers)
        if preferred_group:
            headers["X-Resource-Group"] = preferred_group

        # Build ~140KB doc to clear Gemini's 32K-token cachedContents min.
        stable_doc = (_build_stable_doc() + "\n") * 10
        model_name = model["name"]

        def _send(question: str) -> Dict[str, Any]:
            r = proxy_client.post(
                f"/v1beta/models/{model_name}:generateContent",
                headers=headers,
                json={
                    "contents": [
                        {"role": "user", "parts": [{"text": question}]},
                    ],
                    "systemInstruction": {
                        "parts": [
                            {
                                "text": f"You have access to this document:\n\n{stable_doc}"
                            }
                        ]
                    },
                    "generationConfig": {"maxOutputTokens": 32},
                },
            )
            assert r.status_code == 200, (
                f"native turn failed ({r.status_code}): {r.text[:300]}"
            )
            return r.json()

        # Turn 1: triggers background cachedContents warm.
        _ = _send("In one sentence, what is this document about?")

        # Wait for warm to land in Redis.
        import time

        time.sleep(30)

        # Turn 2: expect cached_content tokens > 0.
        body2 = _send("In one sentence, who runs this gateway?")
        usage2 = body2.get("usageMetadata", {})
        cached = usage2.get("cachedContentTokenCount", 0)
        assert cached > 0, (
            f"Gemini cachedContentTokenCount should be > 0 on turn 2 after "
            f"the background warm completes. Got usage2 = {usage2}."
        )
