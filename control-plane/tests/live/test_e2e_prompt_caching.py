# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live end-to-end prompt-cache effectiveness probe — OpenAI, Anthropic, Gemini.

Provider-specific cache logic lives in adapter modules whose boundary
contract is pinned by unit tests. This
suite is the operational counterpart: it runs an actual two-turn
conversation against a live provider and asserts that the cache
counters returned in the response usage block confirm caching
*actually happened* on turn 2.

Per provider:
  - Pick one chat-capable model (smallest available — we're measuring
    cache effectiveness, not model quality).
  - Build a ~5KB stable doc as the leading system message. Anthropic
    and OpenAI cache prefixes ≥ 1024 tokens; ~5KB sits well above
    that. Gemini's 32K-token minimum requires a ~140KB doc — the
    Gemini arm builds one synthetically.
  - Turn 1: send the doc + a short user question. Capture response
    usage.
  - Turn 2: send the doc + a different short user question. Capture
    response usage and assert provider-specific cache counters are
    > 0.

Assertions per provider (response shape after proxy normalisation):
  | Provider  | Counter                                    | Expected   |
  |-----------|--------------------------------------------|------------|
  | OpenAI    | usage.prompt_tokens_details.cached_tokens  | > 0 on T2  |
  | Anthropic | usage.cache_creation_input_tokens          | > 0 on T1  |
  | Anthropic | usage.cache_read_input_tokens              | > 0 on T2  |
  | Gemini    | usageMetadata.cachedContentTokenCount      | > 0 on T2  |

The suite is opt-in: ``pytest -m live_e2e tests/live/test_e2e_prompt_caching.py -v``.

Per-provider arms skip cleanly when the provider's key isn't
configured — a partial setup still produces a green run on the
providers it CAN cover.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from .conftest import e2e_chat_model_names
from .test_e2e_smoke import _list_trial_chat_models


# ─────────────────────────────────────────────────────────────────────
# Doc builders — sized to each provider's minimum cacheable prefix
# ─────────────────────────────────────────────────────────────────────


def _build_stable_doc_small() -> str:
    """~15KB doc — well above OpenAI's 1024-token (~4KB) auto-prefix
    threshold and Anthropic's per-block cache minimum (2048 tokens for
    Sonnet/Opus, 2048 for Haiku 4.5+). Generously sized so models in
    the 4.5+ generation, which use the higher minimum, also cache."""
    paragraphs = [
        "The Brutor AI Gateway is a multi-tenant LLM proxy that "
        "centralises authentication, rate limiting, prompt-cache "
        "lifecycle, and observability for any number of upstream "
        "models. Tenants register their own provider API keys and "
        "configure which models their users can reach."
    ] * 80
    return "\n\n".join(paragraphs)


def _build_stable_doc_huge() -> str:
    """~140KB doc to clear Gemini's 32K-token (~128KB) cachedContents
    minimum. Built from the small doc repeated until it exceeds the
    threshold by a comfortable margin."""
    base = _build_stable_doc_small()
    chunks = []
    total = 0
    while total < 140_000:
        chunks.append(base)
        total += len(base) + 2
    return "\n\n".join(chunks)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _pick_first_model_by_provider(
    candidates: List[Dict[str, Any]],
    provider: str,
) -> Optional[Dict[str, Any]]:
    """First visible candidate that is one of ``provider``'s E2E_MODELS
    chat models (conftest), in E2E_MODELS order. Replaces the old
    name-substring heuristics ("gpt-4", "flash", ...) that matched
    whatever happened to be configured."""
    by_name = {m.get("name"): m for m in candidates}
    for name in e2e_chat_model_names(provider):
        if name in by_name:
            return by_name[name]
    return None


def _send_chat(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    model_name: str,
    messages: List[Dict[str, Any]],
) -> Tuple[int, Dict[str, Any]]:
    """Send a chat completion (non-streaming) and return
    ``(status_code, parsed_body)``."""
    r = proxy_client.post(
        "/v1/proxy/llm/chat/completions",
        headers=chat_headers,
        json={
            "model": model_name,
            "messages": messages,
            "temperature": 0.1,
            # 256, not 64: thinking models (Gemini 2.5+/3, gpt-5.x) can spend a
            # small budget entirely on reasoning and answer with empty content
            # and finish_reason "length" (live E2E, 0.11.25).
            "max_tokens": 256,
            "stream": False,
        },
    )
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:400]}
    return r.status_code, body


def _read_cache_counters(usage: Dict[str, Any]) -> Dict[str, int]:
    """Pull every cache counter our proxy normalises into the response.
    Missing fields are reported as 0 so downstream assertions can
    treat them uniformly."""
    out = {
        # OpenAI auto-prefix
        "openai_cached_tokens": 0,
        # Anthropic explicit breakpoints
        "anthropic_cache_creation": 0,
        "anthropic_cache_read": 0,
        # Gemini cachedContents
        "gemini_cached_content_tokens": 0,
    }
    details = usage.get("prompt_tokens_details") or {}
    out["openai_cached_tokens"] = int(details.get("cached_tokens") or 0)
    out["anthropic_cache_creation"] = int(usage.get("cache_creation_input_tokens") or 0)
    out["anthropic_cache_read"] = int(usage.get("cache_read_input_tokens") or 0)
    # Gemini lives under `usageMetadata` on the native shape; if the
    # proxy normalises to OpenAI shape and carries this through it
    # may also appear under the top-level `usage` block.
    out["gemini_cached_content_tokens"] = int(
        usage.get("cachedContentTokenCount") or 0
    )
    return out


def _two_turn_cache_probe(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    model_name: str,
    stable_doc: str,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Run a two-turn conversation with `stable_doc` as the leading
    system message. Returns the cache counters from turn 1 and turn 2."""
    system = {
        "role": "system",
        "content": f"You have access to the following reference document:\n\n{stable_doc}",
    }
    # Turn 1 — populate the cache.
    s1, b1 = _send_chat(
        proxy_client,
        chat_headers,
        model_name,
        [
            system,
            {"role": "user", "content": "In one sentence, what is this document about?"},
        ],
    )
    assert s1 == 200, f"turn 1 chat failed ({s1}): {b1}"
    usage1 = b1.get("usage") or b1.get("usageMetadata") or {}
    counters1 = _read_cache_counters(usage1)

    # Turn 2 — different question, same stable doc. The cache prefix
    # is everything up to (and including) the system message, which
    # is identical to turn 1.
    s2, b2 = _send_chat(
        proxy_client,
        chat_headers,
        model_name,
        [
            system,
            {"role": "user", "content": "In one sentence, who runs this gateway?"},
        ],
    )
    assert s2 == 200, f"turn 2 chat failed ({s2}): {b2}"
    usage2 = b2.get("usage") or b2.get("usageMetadata") or {}
    counters2 = _read_cache_counters(usage2)

    # If the gateway's own response/semantic cache is bound to this group,
    # turn 2 is answered from the gateway cache and never reaches the
    # provider — provider-side cache counters are unmeasurable here. The
    # tell is both turns carrying the same upstream completion id. That is
    # a stack-configuration conflict, not a proxy regression: skip with an
    # actionable message instead of failing with a misleading one.
    id1, id2 = b1.get("id"), b2.get("id")
    if id1 and id1 == id2:
        pytest.skip(
            "gateway response/semantic cache served turn 2 (same completion "
            f"id {id1!r} on both turns) — provider prompt caching cannot be "
            "measured on a group with a gateway cache binding. Unbind the "
            "cache from the group (or use a cache-free group) to run this tier."
        )

    return counters1, counters2


# ─────────────────────────────────────────────────────────────────────
# Tests — one arm per provider, each independently skip-able
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.live_e2e
class TestPromptCachingEffectiveness:
    """Prove each provider's caching mechanism actually engages
    end-to-end through the proxy."""

    def test_openai_auto_prefix_cache_hit_on_turn_2(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        if "openai" not in keyed_providers:
            pytest.skip("OPENAI_API_KEY not configured")

        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers
        )
        model = _pick_first_model_by_provider(
            candidates, "openai"
        )
        if model is None:
            pytest.skip("no OpenAI chat model visible to trial user")

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        c1, c2 = _two_turn_cache_probe(
            proxy_client,
            chat_headers,
            model["name"],
            _build_stable_doc_small(),
        )

        # OpenAI's auto-prefix cache: read tokens > 0 on turn 2. We
        # don't assert turn-1 counts because OpenAI's cache fill is
        # eventual — sometimes the first call has already-warmed bytes
        # from prior runs (e.g. CI re-runs), sometimes not. The
        # turn-2 hit is the load-bearing check.
        assert c2["openai_cached_tokens"] > 0, (
            f"OpenAI cached_tokens should be > 0 on turn 2 with a stable "
            f"~5KB system prefix. Got turn-1={c1}, turn-2={c2} on "
            f"model={model['name']!r}. If this fails consistently, the "
            f"proxy is either (a) not preserving the auto-prefix-eligible "
            f"prefix shape upstream, or (b) the response normaliser is "
            f"stripping `usage.prompt_tokens_details.cached_tokens`."
        )

    def test_anthropic_cache_control_breakpoints_engage(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        if "anthropic" not in keyed_providers:
            pytest.skip("ANTHROPIC_API_KEY not configured")

        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers
        )
        model = _pick_first_model_by_provider(
            candidates, "anthropic"
        )
        if model is None:
            pytest.skip("no Anthropic chat model visible to trial user")

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        c1, c2 = _two_turn_cache_probe(
            proxy_client,
            chat_headers,
            model["name"],
            _build_stable_doc_small(),
        )

        # Anthropic explicit breakpoints: cache_creation > 0 on turn 1
        # (the warm), cache_read > 0 on turn 2 (the hit). Both are
        # surfaced by the Anthropic adapter via
        # `extract_anthropic_usage`.
        assert c1["anthropic_cache_creation"] > 0, (
            f"Anthropic cache_creation_input_tokens should be > 0 on turn 1 "
            f"when a ToolsAndSystem policy is bound and the system block "
            f"exceeds the per-block cache minimum (~1024 tokens for Sonnet/"
            f"Opus, ~2048 for Haiku). Got {c1}. The Anthropic adapter "
            f"(`llm_adapter::anthropic_cache::apply_plan`) may not be "
            f"emitting `cache_control` markers — verify the planner "
            f"resolves `CacheControlBreakpoints` and the policy is enabled."
        )
        assert c2["anthropic_cache_read"] > 0, (
            f"Anthropic cache_read_input_tokens should be > 0 on turn 2 "
            f"after turn 1 warmed the cache. Got turn-1={c1}, turn-2={c2}. "
            f"If creation > 0 on T1 but read == 0 on T2, the cache is "
            f"being warmed but the prefix is changing turn-to-turn (a "
            f"portal-side bug, OR something between the "
            f"proxy and Anthropic is mutating the prefix)."
        )

    def test_gemini_chat_path_works_end_to_end(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        """Smoke check that Gemini chat completion flows end-to-end
        through the proxy and the response normaliser. Cache
        engagement is verified by the next test (currently xfail,
        see its docstring for why)."""
        if "gemini" not in keyed_providers:
            pytest.skip("GOOGLE_API_KEY not configured")

        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers
        )
        model = _pick_first_model_by_provider(
            candidates, "gemini"
        )
        if model is None:
            pytest.skip("no Gemini chat model visible to trial user")

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        status, body = _send_chat(
            proxy_client,
            chat_headers,
            model["name"],
            [{"role": "user", "content": "Reply with the single word 'OK'."}],
        )
        assert status == 200, f"Gemini chat call failed ({status}): {body}"
        # Response should be normalised to OpenAI shape — choices[0].message.content.
        content = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        assert content, (
            f"Gemini response missing content. Body shape: "
            f"{list(body.keys())}, choices: {body.get('choices')}"
        )

    def test_gemini_cached_contents_lifecycle(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        # The conftest maps GOOGLE_API_KEY → provider slug "gemini",
        # which matches the seed catalog's Google AI Studio entries.
        if "gemini" not in keyed_providers:
            pytest.skip("GOOGLE_API_KEY not configured")

        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers
        )
        # Strictly anchor on "gemini" — "flash" alone would also match
        # DeepSeek's deepseek-flash and similar non-Gemini naming
        # collisions, so we require the model name to start with the
        # actual provider's family prefix.
        model = _pick_first_model_by_provider(
            candidates, "gemini"
        )
        if model is None:
            pytest.skip("no Gemini chat model visible to trial user")

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        # Gemini's `cachedContents` minimum is ~32K tokens (~128KB). The
        # lazy-warm strategy means turn 1 NEVER hits and turn 2 only
        # hits if the background warm task completed in time. So we
        # take three turns: T1 trigger warm, brief wait, T2 expect hit,
        # T3 confirm the hit is stable.
        import time

        huge_doc = _build_stable_doc_huge()
        c1, c2 = _two_turn_cache_probe(
            proxy_client, chat_headers, model["name"], huge_doc
        )

        # Turn 2 may miss if the background warm hasn't completed yet.
        # Allow up to 30 seconds for the cachedContents resource to
        # land in Redis, then retry one more turn.
        if c2["gemini_cached_content_tokens"] == 0:
            time.sleep(30)
            system = {
                "role": "system",
                "content": f"You have access to the following reference document:\n\n{huge_doc}",
            }
            s3, b3 = _send_chat(
                proxy_client,
                chat_headers,
                model["name"],
                [
                    system,
                    {"role": "user", "content": "What is the document about?"},
                ],
            )
            assert s3 == 200, f"turn 3 chat failed ({s3}): {b3}"
            usage3 = b3.get("usage") or b3.get("usageMetadata") or {}
            c2 = _read_cache_counters(usage3)

        assert c2["gemini_cached_content_tokens"] > 0, (
            f"Gemini cachedContentTokenCount should be > 0 after the "
            f"background warm completes (waited 30s). Got turn-1={c1}, "
            f"final={c2}. If this fails: check that "
            f"`llm_adapter::gemini_cache::spawn_warm` is reachable from "
            f"the proxy (it requires `model.provider == 'gemini' or "
            f"'google_ai_studio'`), that the model has an `api_key` "
            f"populated, and that Redis is reachable from the proxy."
        )


# ─────────────────────────────────────────────────────────────────────
# Control test: verify a non-cache-mechanism provider does NOT report
# false-positive cache counters.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.live_e2e
class TestPromptCachingNegativeControl:
    """A provider with `mechanism = None` (e.g. Ollama, Groq, smaller
    open-source providers) MUST NOT report cache counters even when
    we send a large stable system prompt. Catches regressions where
    the response normaliser fabricates counters that don't exist on
    the wire."""

    def test_no_mechanism_reports_zero_cache_counters(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        keyed_providers: Dict[str, str],
    ) -> None:
        # Find a provider known to have `mechanism = None` per
        # `mechanism_for_provider` in `services/prompt_cache.rs`. Pick
        # any with a key; skip if none are available.
        no_mech_providers = ["groq", "deepseek", "moonshot"]
        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers
        )

        model: Optional[Dict[str, Any]] = None
        for prov in no_mech_providers:
            if prov in keyed_providers:
                m = _pick_first_model_by_provider(candidates, prov)
                if m is not None:
                    model = m
                    break
        if model is None:
            pytest.skip(
                "no non-cache-mechanism provider keyed (need one of: "
                "groq, deepseek, moonshot) with a visible model"
            )

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        # Use a SMALL doc for the negative control — we don't need to
        # clear any cache threshold here, we only need to verify the
        # response normaliser doesn't fabricate counters for a
        # mechanism-=-None provider. A small doc also keeps the test
        # well under free-tier per-minute token limits (Groq's
        # free-tier models carry tight TPM caps, easily blown by the
        # 15KB doc the affirmative arms use).
        small_doc = (
            "The Brutor AI Gateway proxies LLM requests for one or more "
            "tenants. Each tenant configures its own provider API keys."
        ) * 3

        c1, c2 = _two_turn_cache_probe(
            proxy_client,
            chat_headers,
            model["name"],
            small_doc,
        )

        # Every counter MUST be zero — these providers don't expose
        # any cache mechanism, and we must not fabricate counters.
        for label, counters in (("turn-1", c1), ("turn-2", c2)):
            assert counters["openai_cached_tokens"] == 0, (
                f"{label}: openai_cached_tokens fabricated for "
                f"non-cache-mechanism model {model['name']!r}: {counters}"
            )
            assert counters["anthropic_cache_creation"] == 0, (
                f"{label}: anthropic_cache_creation fabricated: {counters}"
            )
            assert counters["anthropic_cache_read"] == 0, (
                f"{label}: anthropic_cache_read fabricated: {counters}"
            )
            assert counters["gemini_cached_content_tokens"] == 0, (
                f"{label}: gemini_cached_content_tokens fabricated: {counters}"
            )
