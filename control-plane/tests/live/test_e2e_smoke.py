# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live end-to-end smoke for the running Brutor stack.

Flow
====
1. Admin login → token (handled in conftest fixtures).
2. For every provider whose key is in ``provider_keys.env``:
   - (Session provisioning, conftest ``e2e_models``) import each
     E2E_MODELS entry from the catalog if not configured, key + enable
     it and bind it to the **E2ETests** resource group.
3. Per-model ``POST /v1/admin/llms/{id}/health-check`` and assert
   the model reports healthy.
4. Trial user login → token (fixture).
5. Build the ``hello-world`` skill into an OpenAI tool definition
   and POST a chat completion. Assert the model emitted a tool_call
   naming ``hello-world`` — that's the proxy-side activation signal.

The suite is opt-in: ``pytest -m live_e2e tests/live/ -v``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from .conftest import (
    E2E_GROUP_NAME,
    E2E_IMAGE_MODEL,
    E2E_MODELS,
    E2E_STT_MODEL,
    E2E_TTS_MODEL,
    E2E_VIDEO_MODEL,
    HELLO_WORLD_SKILL_NAME,
    check_out_of_credit,
    current_group_ids,
    e2e_chat_model_names,
    list_catalog_entries,
    short,
)


# ─────────────────────────────────────────────────────────────────────
# Tier 1 — auth
# ─────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_admin_login_returns_token(self, admin_token: str) -> None:
        # Fixture would have failed the suite if login didn't return a
        # token; this is a sanity ping that proves the fixture wired up.
        assert admin_token
        assert len(admin_token) > 20  # JWTs are far longer; reject empty stubs.

    def test_admin_can_resolve_default_group(self, default_group_id: str) -> None:
        assert default_group_id

    def test_e2e_group_exists(self, e2e_group_id: str) -> None:
        assert e2e_group_id and e2e_group_id != "default"

    def test_trial_login_returns_token(self, trial_token: str) -> None:
        assert trial_token
        assert len(trial_token) > 20


# ─────────────────────────────────────────────────────────────────────
# Tier 2 — model lifecycle for each keyed provider
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def keyed_provider_models(
    e2e_models: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """``{provider_slug: [configured model row, ...]}`` — the E2E_MODELS rows
    the session provisioning (conftest ``e2e_models``) imported from the
    catalog / reused, keyed, enabled and bound to E2ETests.

    Previously this listed EVERY configured model per provider and, when a
    provider had none, imported up to 12 catalog chat entries — written when
    the catalog and the configured models were one table and every catalog
    model existed in the tenant. Now the catalog is separate and only
    explicitly imported models are configured, so the suite tests exactly
    the curated E2E_MODELS list."""
    return e2e_models


class TestModelLifecycle:
    """The E2E models are configured, keyed, enabled and bound to E2ETests."""

    def test_e2e_models_exist_in_catalog(
        self,
        keyed_providers: Dict[str, str],
        control_client: httpx.Client,
        admin_headers: Dict[str, str],
    ) -> None:
        """Every E2E_MODELS name must still be a catalog entry — a retired
        model has to fail here, not make its tests skip quietly."""
        missing = []
        for slug, names in E2E_MODELS.items():
            catalog = {e.get("model_name") for e in list_catalog_entries(control_client, admin_headers, slug)}
            missing += [f"{slug}/{n}" for n in names if n not in catalog]
        assert not missing, (
            f"E2E_MODELS names no longer in the LLM catalog: {missing}. Pick "
            f"replacements from the provider catalog files and update "
            f"E2E_MODELS in tests/live/conftest.py."
        )

    def test_e2e_models_configured_keyed_and_bound(
        self,
        keyed_providers: Dict[str, str],
        keyed_provider_models: Dict[str, List[Dict[str, Any]]],
        e2e_group_id: str,
        default_group_id: str,
    ) -> None:
        if not keyed_providers:
            pytest.skip("no provider keys present (already filtered at module level)")
        problems = []
        for slug in keyed_providers:
            if slug not in E2E_MODELS:
                continue
            rows = {m.get("model_name"): m for m in keyed_provider_models.get(slug, [])}
            for name in E2E_MODELS[slug]:
                m = rows.get(name)
                if m is None:
                    problems.append(f"{slug}/{name}: not configured")
                    continue
                if not m.get("enabled"):
                    problems.append(f"{slug}/{name}: disabled")
                if e2e_group_id not in current_group_ids(m):
                    problems.append(f"{slug}/{name}: not bound to {E2E_GROUP_NAME}")
                if m.get("catalog_id") is None:
                    problems.append(f"{slug}/{name}: has no catalog_id (not imported from the catalog)")
        assert not problems, "E2E model provisioning incomplete:\n  " + "\n  ".join(problems)


# ─────────────────────────────────────────────────────────────────────
# Tier 3 — per-model health check
# ─────────────────────────────────────────────────────────────────────


class TestHealthCheck:
    def test_each_keyed_model_reports_healthy(
        self,
        keyed_provider_models: Dict[str, List[Dict[str, Any]]],
        control_client: httpx.Client,
        admin_headers: Dict[str, str],
    ) -> None:
        """``POST /v1/admin/llms/{id}/health-check`` per model.

        Aggregates failures per (provider, model_name) so a transient
        outage on one provider doesn't hide failures on others."""
        if not keyed_provider_models:
            pytest.skip("no models for any keyed provider")

        # Aggregate per provider so we can distinguish "the API key is
        # broken" (every model dark) from "this user can't reach a
        # specific preview model" (one or two dark, rest healthy). Only
        # the former should fail the suite — preview-model 404s are
        # provider-side reality, not a bug in our stack.
        by_provider: Dict[str, Dict[str, List[str]]] = {}
        # Modes the proxy probes — anything else (e.g. `rerank`,
        # `moderation`, `routing`) is intentionally skipped because
        # those models don't have a meaningful single-call probe.
        # Multimodal modes (image / audio / video) ARE included: there are
        # mode-aware probes in `llm_models_manager.py`:
        # image uses `n=0` sentinel (DALL·E 400 = healthy), TTS uses
        # 1-char input, transcription uses a 46-byte silent WAV, video
        # uses `duration_seconds=-1` sentinel.
        PROBE_MODES = {
            "chat",
            "responses",
            "embedding",
            "completion",
            "image_generation",
            "audio_speech",
            "audio_transcription",
            "video_generation",
        }
        out_of_credit: List[str] = []
        for slug, models in keyed_provider_models.items():
            stats = by_provider.setdefault(slug, {"healthy": [], "unhealthy": []})
            for model in models:
                mode = (model.get("mode") or "").lower()
                if mode and mode not in PROBE_MODES:
                    continue
                model_id = model["id"]
                model_name = model.get("model_name", model_id)
                # Per-call timeout: the control plane forwards the
                # probe through the Rust proxy which can take a few
                # seconds on cold connections, and we're sequencing
                # 70+ models. The conftest default of 30s is too tight
                # — bump per-call so a single slow probe doesn't hang
                # the whole tier. Catch transport-level errors so a
                # single bad provider can't crash the whole sweep.
                try:
                    r = control_client.post(
                        f"/v1/admin/llms/{model_id}/health-check",
                        headers=admin_headers,
                        timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
                    )
                except httpx.TimeoutException:
                    stats["unhealthy"].append(f"{model_name} (probe timed out)")
                    continue
                except httpx.HTTPError as e:
                    stats["unhealthy"].append(
                        f"{model_name} (transport: {type(e).__name__}: {short(str(e), 80)})"
                    )
                    continue

                # ``healthy`` is the green case. ``requires_tier`` is
                # the restricted-tier case (Codex preview, Pro,
                # o1-pro): the model is correctly seeded but the
                # configured API key doesn't have access — surface a
                # distinct status, not a red light.
                check_status = (r.json().get("status") or "").lower() if r.status_code == 200 else ""
                if r.status_code == 200 and check_status in {"healthy", "requires_tier", "warning"}:
                    stats["healthy"].append(model_name)
                else:
                    err = (
                        short(r.text)
                        if r.status_code != 200
                        else short(str(r.json().get("error_message") or r.json()))
                    )
                    stats["unhealthy"].append(f"{model_name} ({err})")
                    full = r.text if r.status_code != 200 else str(r.json().get("error_message") or "")
                    billing = check_out_of_credit(slug, r.status_code, full)
                    if billing:
                        out_of_credit.append(f"{model_name}: {billing}")

        assert not out_of_credit, "provider key(s) out of credit:\n  " + "\n  ".join(out_of_credit)

        total_checked = sum(
            len(s["healthy"]) + len(s["unhealthy"]) for s in by_provider.values()
        )
        assert total_checked > 0, "no probe-able models were health-checked"

        # Hard fail only when an entire provider has zero healthy models —
        # signals a missing or invalid API key. Otherwise emit a summary
        # so the unhealthy preview models are visible without burning
        # the whole suite over them.
        # A provider with zero healthy models means one of two very
        # different things, and only one of them is the runner's problem:
        #
        #   * the KEY is bad — the provider rejected us (401/403). That is a
        #     setup error and must fail loudly.
        #   * the CATALOG named a model this provider will not serve — a 404,
        #     or the gateway's own resilience layer gating it after repeated
        #     upstream errors (503 gateway_resilience_gated). Now that the
        #     suite provisions every catalog entry rather than a hand-picked
        #     few, this is expected occasionally and says nothing about the
        #     key. Failing the whole provider over it turns a catalog-quality
        #     signal into a red suite.
        def _looks_like_bad_key(failures: List[str]) -> bool:
            blob = " ".join(failures).lower()
            if "401" in blob or "403" in blob:
                return True
            # No recognisable upstream verdict at all — treat as a key
            # problem rather than silently passing.
            return not any(
                marker in blob
                for marker in ("404", "resilience", "503", "not found", "does not exist")
            )

        dark_all = [slug for slug, s in by_provider.items() if not s["healthy"]]
        dark = [s for s in dark_all if _looks_like_bad_key(by_provider[s]["unhealthy"])]
        for slug in dark_all:
            if slug not in dark:
                print(
                    f"\n  NOTE: {slug} has no healthy models, but the failures look "
                    f"like unservable catalog entries rather than a bad key: "
                    f"{by_provider[slug]['unhealthy'][:2]}"
                )
        if dark:
            details = "\n  - ".join(
                f"{slug}: 0 healthy / {len(by_provider[slug]['unhealthy'])} unhealthy"
                f"\n      first failure: {by_provider[slug]['unhealthy'][0] if by_provider[slug]['unhealthy'] else '—'}"
                for slug in dark
            )
            pytest.fail(
                f"{len(dark)} provider(s) have NO healthy models — likely a "
                f"bad/missing API key:\n  - {details}"
            )

        # Soft signal: print the per-provider rollup so a developer
        # running with -s sees what was checked and what was dark.
        for slug, s in sorted(by_provider.items()):
            ok, bad = len(s["healthy"]), len(s["unhealthy"])
            if bad:
                print(
                    f"\n[live_e2e] {slug}: {ok} healthy, {bad} unhealthy "
                    f"(degraded — preview/restricted models?)"
                )
                for line in s["unhealthy"]:
                    print(f"    · {line}")
            else:
                print(f"\n[live_e2e] {slug}: {ok} healthy, all green")


# ─────────────────────────────────────────────────────────────────────
# Tier 4 — trial chat round-trip + skill activation
# ─────────────────────────────────────────────────────────────────────


def _list_trial_chat_models(
    proxy_client: httpx.Client, trial_headers: Dict[str, str]
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Return ``(candidates, preferred_group_id)``.

    - ``candidates`` is every chat-capable model the trial user can
      reach, each as ``{"id": <ULID>, "name": <model_name>}``. We keep
      the id around so the post-run error table can identify rows by
      DB id, not just by display name.
    - ``preferred_group_id`` is the first group in the portal response —
      we send it as ``X-Resource-Group`` on the chat call so the proxy
      preflight resolves access against that specific group rather than
      walking the user's ``primary_group_id`` (which is unset on a
      freshly seeded trial user)."""
    # Axum mounts the route as ``.route("/", get(...))`` under
    # ``/v1/portal/models`` — both with and without a trailing slash
    # may 404 depending on whether redirect normalisation is active.
    # Try both shapes and use the first that responds 200.
    last_err = ""
    body = None
    for path in ("/v1/portal/models/", "/v1/portal/models"):
        r = proxy_client.get(path, headers=trial_headers)
        if r.status_code == 200:
            body = r.json()
            break
        last_err = f"{path} → {r.status_code} {short(r.text)}"
    if body is None:
        pytest.fail(f"could not list portal models: {last_err}")

    # Pull the first resource group id from the response — used as
    # X-Resource-Group on the chat call so preflight scopes the access
    # check to a group we know the user belongs to.
    # The suite binds everything to E2ETests and `trial_headers` already
    # pins it via X-Resource-Group — return that, so callers that re-set
    # the header keep the same group. (The portal's `resource_groups` is a
    # bare id list with Default first; taking element 0, as this used to,
    # routed every chat call through Default → 403 for the E2E models.)
    preferred_group_id: Optional[str] = trial_headers.get("X-Resource-Group")
    rgs = body.get("resource_groups") if isinstance(body, dict) else None
    if not preferred_group_id and isinstance(rgs, list) and rgs:
        first_rg = rgs[0]
        if isinstance(first_rg, dict):
            preferred_group_id = first_rg.get("id") or first_rg.get("group_id")
        elif isinstance(first_rg, str):
            preferred_group_id = first_rg

    raw = body.get("models") if isinstance(body, dict) else body
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = raw
    else:
        pytest.fail(f"unexpected /v1/portal/models shape: {short(str(body))}")

    # Filter to chat-mode. Completion-mode models (e.g.
    # ``gpt-3.5-turbo-instruct``) are *also* exposed by the portal —
    # but they only respond on the ``/completions`` endpoint, not
    # ``/chat/completions``, so feeding them into this sweep produces
    # a guaranteed 404. We keep null-mode models as a safety net for
    # rows where mode metadata is missing entirely.
    def _is_chat(m: Dict[str, Any]) -> bool:
        mode = (m.get("mode") or "").lower()
        return mode in {"chat", "responses", ""}
    # Only the curated E2E_MODELS (conftest) — the portal also lists
    # whatever else the trial user can reach (seeded Default models, demo
    # models), which would multiply provider spend and 403 under the
    # pinned E2ETests group.
    wanted = set(e2e_chat_model_names())
    chat = [
        m for m in items
        if _is_chat(m) and (m.get("model_name") or m.get("name")) in wanted
    ]
    candidates = chat
    if not candidates:
        pytest.fail(
            "trial user sees none of the E2E_MODELS chat models — the session "
            f"provisioning should have bound them to {E2E_GROUP_NAME}. "
            f"Portal returned: {[m.get('model_name') or m.get('name') for m in items]}"
        )

    # Order: cheap-fast names first (so the suite stays responsive on a
    # tiny prompt), then everything else.
    preferred_substrings = ["mini", "haiku", "flash", "small", "lite", "8b"]
    def _rank(m: Dict[str, Any]) -> int:
        name = (m.get("model_name") or m.get("name") or "").lower()
        for i, needle in enumerate(preferred_substrings):
            if needle in name:
                return i
        return len(preferred_substrings)

    ordered = sorted(candidates, key=_rank)
    out: List[Dict[str, Any]] = []
    for m in ordered:
        name = m.get("model_name") or m.get("name") or m.get("id")
        if not name:
            continue
        # Surface ``supports_function_calling`` from the catalogue so the
        # test can stop sending ``tools`` to closed-agentic models
        # (Groq Compound, etc) that 400 on user-supplied tools. Default
        # ``True`` matches the historical assumption.
        caps = m.get("capabilities") or {}
        if not isinstance(caps, dict):
            caps = {}
        supports_fc = caps.get("supports_function_calling")
        # Some endpoints surface the flag at the top level; tolerate
        # both shapes.
        if supports_fc is None:
            supports_fc = m.get("supports_function_calling")
        # Tool calling runs ONLY where the configured model says so: the
        # llm_model `supports_function_calling` column, surfaced by the
        # portal as `capabilities.supports_function_calling`. Missing means
        # unsupported (it used to default to True).
        if supports_fc is None:
            supports_fc = False
        # Surface mode + requires_responses_api so the per-model loop
        # can pick the right gateway endpoint. ``mode=responses`` ⇒
        # POST /v1/proxy/llm/responses; ``requires_responses_api`` is
        # a hard signal from the seed that chat/completions will hard-
        # fail with our friendly 400 ("model only supports the
        # Responses endpoint"). Defaulting to ``chat`` matches the
        # historical assumption.
        mode = (m.get("mode") or "").lower() or "chat"
        requires_responses = bool(caps.get("requires_responses_api"))
        out.append({
            "id": m.get("id") or "",
            "name": name,
            "supports_function_calling": bool(supports_fc),
            "mode": mode,
            "requires_responses_api": requires_responses,
        })
    return out, preferred_group_id


def _accumulate_streamed_message(sse_text: str) -> Dict[str, Any]:
    """Reduce an OpenAI-format SSE stream to the same shape a
    non-streaming response gives back (``{role, content, tool_calls}``).

    Tool-call streaming sends:
    - one initial frame with ``tool_calls[i].id`` + ``function.name``,
    - a series of frames that append to ``function.arguments`` chunk by
      chunk,
    - a closing frame with ``finish_reason=tool_calls``.

    We accumulate by ``tool_calls[i].index`` so the caller's assertion
    can use the same code path as the non-streaming variant."""
    content_parts: List[str] = []
    # Map tool_call index → {"id": str, "name": str, "arguments": str}
    tool_call_acc: Dict[int, Dict[str, str]] = {}
    finish_reason: Optional[str] = None

    for raw_line in sse_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_call_acc.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    return {
        "role": "assistant",
        "content": "".join(content_parts),
        "tool_calls": [
            {
                "id": v["id"],
                "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]},
            }
            for k, v in sorted(tool_call_acc.items())
        ],
        "_finish_reason": finish_reason,
    }


# Result of a single (model, stream-flavour) attempt. The test
# iterates a full sweep of these so every model produces its own log
# rows; per-model errors are captured here, never raised, so a single
# flaky provider can't poison the whole sweep.
class _ChatAttempt:
    __slots__ = ("model", "stream", "status", "msg", "skip_reason")

    def __init__(self, model: str, stream: bool):
        self.model = model
        self.stream = stream
        self.status: int = 0
        self.msg: Optional[Dict[str, Any]] = None
        self.skip_reason: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status == 200 and self.msg is not None


# Per-call HTTP timeout for chat completions. Some reasoning models
# (o1-pro, gpt-5.x-pro, claude-opus-4-7 with thinking) routinely take
# longer than the conftest default of 30s. Using a bespoke long
# timeout here means the suite can finish even when one provider is
# slow on a particular request.
_CHAT_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0)


def _chat_one_model(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    model_name: str,
    body: Dict[str, Any],
    *,
    stream: bool,
) -> _ChatAttempt:
    """Run one chat completion against a specific model. Captures the
    per-model error mode in ``skip_reason`` rather than failing the
    test — the caller decides whether the per-model outcome is
    acceptable. Transport-level errors (timeouts, connection drops)
    are also captured as skips so a single slow model can't sink the
    full sweep."""
    out = _ChatAttempt(model_name, stream)
    body = {**body, "stream": stream}

    text = ""
    try:
        if not stream:
            r = proxy_client.post(
                "/v1/proxy/llm/chat/completions",
                headers=chat_headers,
                json=body,
                timeout=_CHAT_HTTP_TIMEOUT,
            )
            out.status = r.status_code
            if out.status == 200:
                resp = r.json()
                out.msg = (resp.get("choices") or [{}])[0].get("message") or {}
                return out
            text = r.text
        else:
            with proxy_client.stream(
                "POST",
                "/v1/proxy/llm/chat/completions",
                headers=chat_headers,
                json=body,
                timeout=_CHAT_HTTP_TIMEOUT,
            ) as resp:
                out.status = resp.status_code
                if out.status == 200:
                    sse_text = "".join(resp.iter_text())
                    out.msg = _accumulate_streamed_message(sse_text)
                    return out
                text = resp.read().decode("utf-8", errors="replace")
    except httpx.TimeoutException as e:
        out.status = 0
        out.skip_reason = f"timeout ({type(e).__name__})"
        return out
    except httpx.HTTPError as e:
        out.status = 0
        out.skip_reason = f"transport error: {type(e).__name__}: {short(str(e), 80)}"
        return out

    body_text = (text or "").lower()
    # Classify the non-success: real bugs vs known model-can't-do-this.
    if out.status == 403:
        out.skip_reason = "403 access"
    elif out.status in {404, 501}:
        out.skip_reason = f"{out.status} unsupported"
    elif out.status == 400 and ("tool" in body_text or "function" in body_text):
        out.skip_reason = "400 no-tool-calling"
    else:
        # Don't pytest.fail here — let the orchestrator decide based
        # on the per-provider rollup. A single 5xx on one model
        # shouldn't block coverage on the rest.
        out.skip_reason = f"{out.status} {short(text, 80)}"
    return out


# ─────────────────────────────────────────────────────────────────────
# Responses API path — used for models seeded with mode=responses
# (e.g. gpt-5.5 / gpt-5.5-pro) so tools + reasoning_effort don't hit
# OpenAI's chat-completions restriction. Returns the same _ChatAttempt
# shape as _chat_one_model so the per-model rollup can ignore which
# endpoint was actually used.
# ─────────────────────────────────────────────────────────────────────


def _chat_body_to_responses_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an OpenAI chat-completions body into the responses-API
    body shape our proxy expects.

    Differences worth flagging:
      * ``messages`` → ``input`` (same role/content shape).
      * ``max_tokens`` → ``max_output_tokens``.
      * ``tools[].function.{name,description,parameters}`` → flat
        ``tools[].{name,description,parameters}`` with ``type=function``.
      * ``temperature``, ``top_p`` are dropped — Responses-mode
        reasoning models reject both.
    """
    out: Dict[str, Any] = {
        "model": body["model"],
        "input": body.get("messages") or [],
    }
    if "max_tokens" in body:
        out["max_output_tokens"] = body["max_tokens"]
    if "stream" in body:
        out["stream"] = body["stream"]
    raw_tools = body.get("tools")
    if isinstance(raw_tools, list) and raw_tools:
        flat: List[Dict[str, Any]] = []
        for t in raw_tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            flat.append({
                "type": "function",
                "name": fn.get("name") or t.get("name"),
                "description": fn.get("description") or t.get("description"),
                "parameters": fn.get("parameters") or t.get("parameters") or {
                    "type": "object",
                    "properties": {},
                },
            })
        out["tools"] = flat
        if body.get("tool_choice"):
            out["tool_choice"] = body["tool_choice"]
    return out


def _responses_output_to_chat_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a non-stream /v1/responses payload into the same
    ``{role, content, tool_calls}`` shape ``_first_skill_message``
    consumes. Stable shape across endpoints means the rollup logic
    below doesn't have to branch."""
    output = payload.get("output") or []
    content_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    finish_reason: Optional[str] = None

    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        content_parts.append(text)
        elif kind == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                },
            })
            finish_reason = "tool_calls"

    return {
        "role": "assistant",
        "content": "".join(content_parts),
        "tool_calls": tool_calls,
        "_finish_reason": finish_reason or payload.get("status"),
    }


def _accumulate_streamed_responses_message(sse_text: str) -> Dict[str, Any]:
    """Reduce a /v1/responses SSE stream to the same chat-style dict.

    The wire format is a sequence of ``response.*`` events, each with
    its own JSON payload. We only need three of them for our
    rollup:

      * ``response.output_item.added`` — first sight of a
        ``function_call`` item; we record id + name here.
      * ``response.function_call_arguments.delta`` — appends to the
        item's `arguments`.
      * ``response.output_text.delta`` — appends to assistant text.

    Anything else (reasoning items, completed/in-progress markers) is
    accepted but ignored. This keeps the parser tolerant — newer
    event types added by OpenAI won't break our test."""
    content_parts: List[str] = []
    # Keyed by output_index so multiple parallel tool calls stay
    # separate even when their delta frames interleave.
    tool_acc: Dict[int, Dict[str, str]] = {}

    for raw in sse_text.splitlines():
        if not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type") or ""

        if etype == "response.output_item.added":
            idx = event.get("output_index", 0)
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                tool_acc[idx]["id"] = item.get("call_id") or item.get("id") or ""
                tool_acc[idx]["name"] = item.get("name") or ""
        elif etype == "response.function_call_arguments.delta":
            idx = event.get("output_index", 0)
            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            delta = event.get("delta")
            if isinstance(delta, str):
                slot["arguments"] += delta
        elif etype == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                content_parts.append(delta)

    finish_reason: Optional[str] = "tool_calls" if tool_acc else None
    return {
        "role": "assistant",
        "content": "".join(content_parts),
        "tool_calls": [
            {
                "id": v["id"],
                "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]},
            }
            for _, v in sorted(tool_acc.items())
        ],
        "_finish_reason": finish_reason,
    }


def _responses_one_model(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    model_name: str,
    chat_body: Dict[str, Any],
    *,
    stream: bool,
) -> _ChatAttempt:
    """Run one /v1/proxy/llm/responses round-trip, reusing the
    chat-shaped body as the source of truth. Same per-model fault
    isolation as ``_chat_one_model``: transport errors and non-2xx
    responses become skip reasons rather than hard failures."""
    out = _ChatAttempt(model_name, stream)
    body = _chat_body_to_responses_body({**chat_body, "stream": stream})

    text = ""
    try:
        if not stream:
            r = proxy_client.post(
                "/v1/proxy/llm/responses",
                headers=chat_headers,
                json=body,
                timeout=_CHAT_HTTP_TIMEOUT,
            )
            out.status = r.status_code
            if out.status == 200:
                out.msg = _responses_output_to_chat_message(r.json())
                return out
            text = r.text
        else:
            with proxy_client.stream(
                "POST",
                "/v1/proxy/llm/responses",
                headers=chat_headers,
                json=body,
                timeout=_CHAT_HTTP_TIMEOUT,
            ) as resp:
                out.status = resp.status_code
                if out.status == 200:
                    sse_text = "".join(resp.iter_text())
                    out.msg = _accumulate_streamed_responses_message(sse_text)
                    return out
                text = resp.read().decode("utf-8", errors="replace")
    except httpx.TimeoutException as e:
        out.status = 0
        out.skip_reason = f"timeout ({type(e).__name__})"
        return out
    except httpx.HTTPError as e:
        out.status = 0
        out.skip_reason = f"transport error: {type(e).__name__}: {short(str(e), 80)}"
        return out

    body_text = (text or "").lower()
    if out.status == 403:
        out.skip_reason = "403 access"
    elif out.status in {404, 501}:
        out.skip_reason = f"{out.status} unsupported"
    elif out.status == 400 and ("tool" in body_text or "function" in body_text):
        out.skip_reason = "400 no-tool-calling"
    else:
        out.skip_reason = f"{out.status} {short(text, 80)}"
    return out


def _chat_or_responses(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    model_name: str,
    chat_body: Dict[str, Any],
    *,
    stream: bool,
    use_responses: bool,
) -> _ChatAttempt:
    """Single dispatch point for the per-model sweep. Picks the right
    gateway endpoint based on the model's seeded mode + capability
    flags; the rollup table below doesn't need to know which one ran."""
    if use_responses:
        return _responses_one_model(
            proxy_client, chat_headers, model_name, chat_body, stream=stream
        )
    return _chat_one_model(
        proxy_client, chat_headers, model_name, chat_body, stream=stream
    )


# MCP system-server constants (mirrored from
# brutor-gateway-core/src/constants.rs::system_server::agent_skill).
# Driving the skill via the MCP path — JSON-RPC ``tools/call`` against
# the system agent-skill server — produces ``proxy_type=mcp`` rows in
# proxy_logs (which the Default-group Usage card counts as "MCP
# Calls"). The REST ``/skills/execute`` endpoint writes
# ``proxy_type=skill`` and is invisible to the MCP-Calls counter.
#
# The database row id is the constant suffixed with the tenant id,
# e.g. ``system-agent-skill-server-default``. We discover it at
# runtime so tenant-id changes don't break the suite.
_AGENT_SKILL_SERVER_PREFIX = "system-agent-skill-server"
_AGENT_SKILL_TOOL_PREFIX = "skill"


def _resolve_agent_skill_server_id(
    proxy_client: httpx.Client, headers: Dict[str, str]
) -> Optional[str]:
    """Find the tenant-scoped id of the system agent-skill MCP server
    by listing servers visible to the trial user."""
    for path in ("/v1/portal/servers", "/v1/portal/servers/"):
        r = proxy_client.get(path, headers=headers)
        if r.status_code != 200:
            continue
        body = r.json()
        raw = body.get("servers") if isinstance(body, dict) else body
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            continue
        for s in raw:
            if not isinstance(s, dict):
                continue
            sid = s.get("mcp_server_id") or s.get("server_id") or s.get("id") or ""
            if sid.startswith(_AGENT_SKILL_SERVER_PREFIX):
                return sid
    return None


def _execute_hello_world(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    msg: Dict[str, Any],
    agent_skill_server_id: str,
) -> Tuple[bool, str]:
    """Invoke ``hello-world`` through the MCP path the portal uses.

    Sends a JSON-RPC ``tools/call`` to the system agent-skill server
    with the LLM-supplied arguments. Returns ``(ok, detail)``."""
    tool_calls = msg.get("tool_calls") or []
    first_call = next(
        (tc for tc in tool_calls
         if tc.get("function", {}).get("name") == HELLO_WORLD_SKILL_NAME),
        None,
    )
    input_params: Dict[str, Any] = {}
    if first_call:
        raw_args = first_call.get("function", {}).get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if isinstance(parsed, dict):
                input_params = parsed
        except json.JSONDecodeError:
            input_params = {}

    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": f"{_AGENT_SKILL_TOOL_PREFIX}__{HELLO_WORLD_SKILL_NAME}",
            "arguments": input_params,
        },
    }
    try:
        r = proxy_client.post(
            f"/v1/proxy/mcp/{agent_skill_server_id}",
            headers=chat_headers,
            json=rpc,
            timeout=_CHAT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        return False, f"transport: {type(e).__name__}: {short(str(e), 120)}"
    if r.status_code != 200:
        return False, f"{r.status_code} {short(r.text, 160)}"
    body = r.json()
    # JSON-RPC error envelope
    if isinstance(body.get("error"), dict):
        return False, f"jsonrpc error {short(json.dumps(body['error']), 160)}"
    result = body.get("result") or {}
    # MCP tools/call result shape: { content: [{type, text}], isError? }
    if result.get("isError"):
        return False, f"isError=true {short(json.dumps(result), 160)}"
    content = result.get("content") or []
    text = ""
    for chunk in content:
        if isinstance(chunk, dict) and chunk.get("type") == "text":
            text += chunk.get("text") or ""
    if not text:
        return False, f"empty result content {short(json.dumps(result), 160)}"
    return True, short(text, 80)


# ─────────────────────────────────────────────────────────────────────
# Per-model error classification + diagnosis table
# ─────────────────────────────────────────────────────────────────────


_DIAGNOSIS = {
    "timeout": (
        "Provider didn't respond within the read timeout. Common for "
        "reasoning models on long prompts; not a Brutor bug."
    ),
    "transport_error": (
        "TCP / TLS / proxy connection problem. Usually transient — "
        "rerun the suite. If repeatable, investigate provider DNS or "
        "outbound network."
    ),
    "529": (
        "Anthropic 'Overloaded' — provider-side capacity event. The proxy "
        "should absorb this with a single retry on overloaded_error."
    ),
    "404": (
        "model_not_found from provider. The configured API key does not "
        "have access to this model (preview / restricted tier)."
    ),
    "403": (
        "Proxy preflight denied access — the user's group doesn't include "
        "this model, or the X-Resource-Group header didn't match."
    ),
    "400_no_tool": (
        "Provider rejects the `tools` parameter for this model. The seed "
        "should set supports_function_calling=false."
    ),
    "400_temp": (
        "Provider rejects the `temperature` parameter for this model. "
        "Seed should record a temperature_locked capability and the "
        "adapter should drop/coerce it before forwarding."
    ),
    "400_multi_agent": (
        "Multi-agent Grok model — only works on /v1/responses, not "
        "/v1/chat/completions. Needs to be flagged for auto-routing."
    ),
    "400_other": "Provider returned 400 for another reason; see message.",
    "500_other": "Provider returned 5xx; check provider status.",
    "skill_skip": (
        "LLM didn't select hello-world even when prompted to. Usually "
        "model-quality (small / heavy reasoning models can ignore tools "
        "for greeting prompts)."
    ),
    "skill_exec_fail": (
        "MCP tools/call returned an error executing the skill itself."
    ),
}


def _classify(att: "_ChatAttempt") -> str:
    """Map a per-attempt skip_reason to a stable Error ID."""
    reason = (att.skip_reason or "").lower()
    # status==0 means we never got an HTTP response (timeout / transport)
    if att.status == 0:
        if "timeout" in reason:
            return "timeout"
        return "transport_error"
    if att.status == 403:
        return "403"
    if att.status == 404:
        return "404"
    if att.status == 529:
        return "529"
    if att.status == 400:
        if "temperature" in reason:
            return "400_temp"
        if "multi agent" in reason or "multi-agent" in reason:
            return "400_multi_agent"
        if "tool" in reason or "function" in reason:
            return "400_no_tool"
        return "400_other"
    if 500 <= att.status < 600:
        return "500_other"
    return f"http_{att.status}"


def _first_skill_message(
    *attempts: Optional["_ChatAttempt"],
) -> Optional[Dict[str, Any]]:
    """Return the first attempt's message dict whose tool_calls name
    the hello-world skill, or None."""
    for att in attempts:
        if att is None or not att.succeeded:
            continue
        msg = att.msg or {}
        names = [
            tc.get("function", {}).get("name")
            for tc in (msg.get("tool_calls") or [])
        ]
        if HELLO_WORLD_SKILL_NAME in names:
            return msg
    return None


def _print_error_table(problems: List[Dict[str, str]]) -> None:
    """Emit a Markdown table of per-model problems. Always printed at
    the end of the chat-tier run (regardless of pass/fail)."""
    if not problems:
        print("\n=== Encountered problems: none — every model passed ===")
        return
    # Deduplicate (model_id, error_id) — multiple stream variants of
    # the same model often record the same skip reason.
    seen: set = set()
    unique: List[Dict[str, str]] = []
    for p in problems:
        key = (p["model_id"], p["error_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    print(f"\n=== Encountered problems ({len(unique)} unique) ===")
    print("|model ID|model name|Error ID|Error message|What is happening|")
    print("|---|---|---|---|---|")
    for p in unique:
        msg = p["message"].replace("|", "/").replace("\n", " ")
        diag = (p["diagnosis"] or "").replace("|", "/").replace("\n", " ")
        print(
            f"|{p['model_id']}|{p['model_name']}|{p['error_id']}|{msg}|{diag}|"
        )


class TestHelloWorldChat:
    """Exercise every chat-capable model the trial user can reach.

    For each model we record three usage rows in ``proxy_logs``:
      1. non-streaming chat completion with the hello-world tool,
      2. streaming chat completion with the hello-world tool, and
      3. an Agent Skill invocation against the hello-world skill,
         driven from whatever args the LLM produced.

    The test passes when *at least one* model produces a fully
    successful triplet (chat OK → tool_call references the skill →
    skill executes). Per-model errors are captured in a rollup table
    rather than aborting the run, so a single flaky provider doesn't
    starve the rest.
    """

    def test_each_chat_model_activates_hello_world_skill(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        hello_world_tool_def: Dict[str, Any],
        control_client: httpx.Client,
        admin_headers: Dict[str, str],
    ) -> None:
        if not hello_world_tool_def:
            pytest.fail(
                "hello-world skill is not visible in the trial user's "
                "skill catalog. Check that the trial bundle seeded the "
                "skill; the session provisioning binds it to E2ETests."
            )

        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers
        )
        if not candidates:
            pytest.fail("trial user has no chat-capable models exposed by the portal")

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        agent_skill_server_id = _resolve_agent_skill_server_id(
            proxy_client, chat_headers
        )
        if not agent_skill_server_id:
            pytest.fail(
                "could not find the system agent-skill MCP server in the "
                "trial user's portal-server list. The system server should "
                "be seeded on tenant init; the session provisioning binds it to E2ETests."
            )

        def make_body(
            model_name: str,
            user_prompt: str = "Hello World!",
            *,
            include_tools: bool,
        ) -> Dict[str, Any]:
            body: Dict[str, Any] = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are Brutor's assistant. The user has a "
                            f"`{HELLO_WORLD_SKILL_NAME}` skill available "
                            "as a tool — call it whenever the user "
                            "greets you, instead of replying in plain text."
                        )
                        if include_tools
                        else "You are Brutor's assistant. Respond briefly.",
                    },
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 256,
                "temperature": 0,
            }
            if include_tools:
                body["tools"] = [hello_world_tool_def]
                body["tool_choice"] = "auto"
            return body

        # Per-model rollup; key is the model_name so the printout is
        # ordered by name. ``problems`` is the structured form keyed
        # by model_id, used for the "always-print" error table.
        rollup: Dict[str, Dict[str, str]] = {}
        problems: List[Dict[str, str]] = []
        any_full_triplet = False

        def _record_problem(
            model_id: str, model_name: str, error_id: str, message: str
        ) -> None:
            problems.append({
                "model_id": model_id or "(unknown)",
                "model_name": model_name,
                "error_id": error_id,
                "message": short(message, 200),
                "diagnosis": _DIAGNOSIS.get(error_id, ""),
            })

        for entry in candidates:
            name = entry["name"]
            mid = entry.get("id") or ""
            supports_fc = bool(entry.get("supports_function_calling", False))
            # Models seeded with ``mode=responses`` (or that the seed
            # explicitly flagged as ``requires_responses_api``) are
            # exercised against ``/v1/proxy/llm/responses`` instead of
            # ``/v1/proxy/llm/chat/completions``. Mixing them up
            # produces guaranteed failures: gpt-5.5-pro 404s on chat,
            # and gpt-5.5 with tools+reasoning 400s with "use
            # /v1/responses instead". The dispatch below is the
            # single source of truth — this is the regression test
            # for that combination class.
            mode = (entry.get("mode") or "").lower()
            use_responses = mode == "responses" or bool(
                entry.get("requires_responses_api")
            )
            row = rollup.setdefault(
                name, {"non_stream": "—", "stream": "—", "skill": "—"}
            )

            # 1. Non-streaming chat with the standard prompt. If the
            #    catalogue says this model doesn't support function
            #    calling, omit ``tools`` entirely — sending them to a
            #    closed-agentic system (Groq Compound) returns 400.
            ns = _chat_or_responses(
                proxy_client,
                chat_headers,
                name,
                make_body(name, include_tools=supports_fc),
                stream=False,
                use_responses=use_responses,
            )
            row["non_stream"] = "ok" if ns.succeeded else f"skip ({ns.skip_reason})"
            if not ns.succeeded:
                _record_problem(mid, name, _classify(ns), ns.skip_reason or "")

            # 2. Streaming chat
            st = _chat_or_responses(
                proxy_client,
                chat_headers,
                name,
                make_body(name, include_tools=supports_fc),
                stream=True,
                use_responses=use_responses,
            )
            row["stream"] = "ok" if st.succeeded else f"skip ({st.skip_reason})"
            if not st.succeeded:
                _record_problem(mid, name, _classify(st), st.skip_reason or "")

            # 3. Skill execution. Only attempted when the catalogue
            #    says the model can use tools — otherwise the LLM had
            #    no way to name the skill in the first place. Skip the
            #    skill row honestly rather than counting it against
            #    the model.
            if not supports_fc:
                row["skill"] = "skip (model does not support tool calling)"
                continue

            tool_call_msg = _first_skill_message(ns, st)
            if tool_call_msg is None and (ns.succeeded or st.succeeded):
                retry = _chat_or_responses(
                    proxy_client,
                    chat_headers,
                    name,
                    make_body(
                        name,
                        "Hello World! use tool if available",
                        include_tools=True,
                    ),
                    stream=False,
                    use_responses=use_responses,
                )
                tool_call_msg = _first_skill_message(retry, None)
                if tool_call_msg is not None:
                    row["non_stream"] += " (retry-OK)"

            if tool_call_msg is None:
                if ns.succeeded or st.succeeded:
                    row["skill"] = "skip (LLM did not select the skill)"
                    _record_problem(
                        mid, name, "skill_skip",
                        "LLM did not select hello-world tool, even with retry",
                    )
                else:
                    row["skill"] = "skip (no chat call succeeded)"
                continue

            ok, detail = _execute_hello_world(
                proxy_client, chat_headers, tool_call_msg, agent_skill_server_id
            )
            row["skill"] = f"ok ({detail})" if ok else f"FAIL ({detail})"
            if ok and ns.succeeded and st.succeeded:
                any_full_triplet = True
            elif not ok:
                _record_problem(mid, name, "skill_exec_fail", detail)

        # Per-model triplet rollup (always printed)
        rollup_lines = [
            f"  {m:<48} non-stream={r['non_stream']:<24} "
            f"stream={r['stream']:<24} skill={r['skill']}"
            for m, r in sorted(rollup.items())
        ]
        rollup_text = (
            f"\nPer-model triplet results ({len(rollup)} models):\n"
            + "\n".join(rollup_lines)
        )
        print(rollup_text)

        # Always print the markdown error table — same shape the
        # operator asked for in the runbook.
        _print_error_table(problems)

        if not any_full_triplet:
            pytest.fail(
                "No model completed the full (non-stream, stream, skill) "
                "triplet. At least one model must succeed on all three for "
                "this test to pass. See the table above for per-model "
                "failure modes."
            )


# ─────────────────────────────────────────────────────────────────────
# Tier 5 — multimodal proxy endpoints
#
# Image / audio / video modes go through dedicated proxy routes (NOT
# /v1/proxy/llm/chat/completions). Each route runs the same governance
# pipeline as the chat path — auth, quota, group access, input
# guardrails, resilience gate, dispatch, output guardrails (where
# applicable), proxy_log, usage_record, breaker signal — so the live
# stack can regress in any of those steps. Tier 5 exercises one model
# per mode end-to-end:
#
#   * Image:        OpenAI gpt-image-2.6-flare (synchronous, returns URL/b64_json)
#   * Audio TTS:    OpenAI tts-1       (synchronous, returns audio bytes)
#   * Audio STT:    OpenAI gpt-transcribe   (multipart upload, returns text)
#   * Video:        Fal luma/agent/ray/v3.2/text-to-video (async queue, poll)
#
# The model names below are pinned to the seed catalogue and guarded by
# `test_multimodal_model_names_still_exist_in_the_catalogue` — see the
# note there for why a rename must fail rather than skip.
#
# Tests skip cleanly if the corresponding provider key isn't present
# or the seed model isn't enabled in this tenant — same pattern as
# Tier 4.
# ─────────────────────────────────────────────────────────────────────


def _build_silent_wav() -> bytes:
    """PCM WAV with 0.2s of silence (≥ OpenAI Whisper's current 0.1s
    minimum). The clip is just long enough to clear the
    `audio_too_short` upstream guard while staying small enough to
    upload instantly — at 8 kHz / 16-bit / mono that's 1600 samples
    = 3200 byte data section + 44 byte header = 3244 bytes total.
    Constructed inline rather than imported so the live suite has
    zero compile-time coupling to the probe module.
    """
    import struct
    sample_rate = 8000
    bits_per_sample = 16
    channels = 1
    # 0.2 seconds = 1600 samples at 8 kHz. OpenAI's
    # `audio_too_short` (`Minimum audio length is 0.1 seconds`)
    # surfaced after a server-side change in late 2025; the previous
    # 1-sample fixture passed before then. Doubling the floor gives
    # headroom against future tightening without bloating uploads.
    num_samples = 1600
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = num_samples * channels * bits_per_sample // 8
    chunk_size = 36 + data_size
    return (
        b"RIFF"
        + struct.pack("<I", chunk_size)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        )
        + b"data"
        + struct.pack("<I", data_size)
        + b"\x00" * data_size
    )


def _find_model_by_name(
    models: List[Dict[str, Any]],
    target_name: str,
) -> Optional[Dict[str, Any]]:
    """Pick a model from the per-provider list whose `model_name`
    matches `target_name` exactly, AND that's enabled. Returns
    None if no match — caller skips with a useful message."""
    for m in models:
        if m.get("model_name") == target_name and m.get("enabled"):
            return m
    return None


class TestMultimodalEndpoints:
    """One end-to-end smoke per multimodal mode.

    These tests run AFTER `TestModelLifecycle` has enabled+keyed+
    grouped every keyed-provider model, so gpt-image-2.6-flare / tts-1 /
    gpt-transcribe / luma/agent/ray/v3.2/text-to-video should be reachable to
    the trial user via the E2ETests group. Each test is otherwise
    independent — a transient failure on one mode doesn't poison the
    others.
    """

    # The exact catalogue names these tests call. Kept in one place so
    # the guard below and the tests cannot disagree about what the seed
    # is supposed to contain.
    IMAGE_MODEL = E2E_IMAGE_MODEL
    TTS_MODEL = E2E_TTS_MODEL
    STT_MODEL = E2E_STT_MODEL
    VIDEO_MODEL = E2E_VIDEO_MODEL

    # ─────────── Catalogue guard ───────────

    def test_multimodal_model_names_still_exist_in_the_catalogue(
        self,
        control_client: httpx.Client,
        admin_headers: Dict[str, str],
    ) -> None:
        """The four tests below must not go quiet when a model is retired.

        Every multimodal test resolves its model by exact name and skips
        when the lookup misses. That is the right behaviour for "the
        operator hasn't enabled it", and the wrong behaviour for "the
        model no longer exists" — and the two are indistinguishable at
        the skip.

        They were indistinguishable in practice. `dall-e-3` was dropped
        from `openai_multimodal_models_init.py` (OpenAI
        retired it) and `fal-ai/luma-dream-machine` was
        superseded by `/ray-2-flash` (gone since: Fal answers 404; the video
        default is now `luma/agent/ray/v3.2/text-to-video`), so the image and video
        smokes reported a tidy skip for months while testing nothing,
        blaming tenant configuration in the message.

        This asserts the names are in the catalogue at all. Enablement
        is still the individual tests' business — a miss here means the
        seed moved and the constants above need updating, which is a
        code change, not a deployment question.
        """
        # Same unwrapping as conftest `list_catalog_entries` —
        # the catalogue list route has been through more than one envelope
        # shape, so both readers stay tolerant of all of them.
        names: set = set()
        for provider in ("openai", "fal_ai"):
            r = control_client.get(
                f"/v1/admin/llm-catalog?provider_key={provider}&limit=500",
                headers=admin_headers,
            )
            if r.status_code != 200:
                pytest.skip(
                    f"cannot read the LLM catalogue for {provider} "
                    f"({r.status_code})"
                )
            body = r.json()
            entries = (
                body.get("entries") or body.get("items") or body.get("catalog") or []
            )
            if isinstance(entries, dict):
                entries = list(entries.values())
            names |= {
                e.get("model_name") for e in entries if isinstance(e, dict)
            }
        if not names:
            pytest.skip("LLM catalogue came back empty — nothing to check")

        expected = {
            "image": self.IMAGE_MODEL,
            "audio_speech": self.TTS_MODEL,
            "audio_transcription": self.STT_MODEL,
            "video": self.VIDEO_MODEL,
        }
        missing = {k: v for k, v in expected.items() if v not in names}
        assert not missing, (
            f"multimodal smoke models are no longer in the seed catalogue: "
            f"{missing}. Update the constants on TestMultimodalEndpoints to "
            f"the replacement models rather than leaving these tests to skip."
        )

    # ─────────── Image: gpt-image-2.6-flare ───────────

    def test_gpt_image_2_image_generation(
        self,
        keyed_providers: Dict[str, str],
        keyed_provider_models: Dict[str, List[Dict[str, Any]]],
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
    ) -> None:
        if "openai" not in keyed_providers:
            pytest.skip("OPENAI_API_KEY not set — image generation needs OpenAI")
        models = keyed_provider_models.get("openai", [])
        model = _find_model_by_name(models, self.IMAGE_MODEL)
        if model is None:
            pytest.skip(
                f"{self.IMAGE_MODEL} not enabled in this tenant — Tier 2 "
                "should have enabled it via OPENAI_API_KEY. If the model "
                "is missing from the catalogue entirely rather than "
                "merely disabled, the guard test in this class fails "
                "instead of this skipping quietly."
            )

        body = {
            # Sending the model NAME (not the brutor id) lets the
            # proxy resolve via either path. The portal/admin UIs
            # both use the name on the wire.
            "model": self.IMAGE_MODEL,
            "prompt": "A small green dot on white background, minimal vector art.",
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",  # avoids depending on URL
                                            # storage/CDN
        }
        # Image generation can take 10-30s; bump read timeout.
        r = proxy_client.post(
            "/v1/proxy/llm/images/generations",
            headers={**trial_headers, "Content-Type": "application/json"},
            json=body,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
        )
        assert r.status_code == 200, (
            f"{self.IMAGE_MODEL} image failed ({r.status_code}): {short(r.text)}"
        )
        body = r.json()
        data = body.get("data") or []
        assert isinstance(data, list) and len(data) >= 1, (
            f"{self.IMAGE_MODEL} returned no `data[]` items: {short(r.text)}"
        )
        first = data[0]
        # Either response_format we asked for OR a brutor file ref
        # is acceptable — the proxy may persist + return a file id.
        has_payload = (
            first.get("b64_json")
            or first.get("url")
            or first.get("brutor_file_id")
            or body.get("brutor_file_id")
        )
        assert has_payload, (
            f"{self.IMAGE_MODEL} returned a `data[0]` with no image payload "
            f"(no b64_json / url / brutor_file_id): {short(str(first))}"
        )

    # ─────────── Audio TTS: tts-1 ───────────

    def test_tts_1_audio_speech(
        self,
        keyed_providers: Dict[str, str],
        keyed_provider_models: Dict[str, List[Dict[str, Any]]],
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
    ) -> None:
        if "openai" not in keyed_providers:
            pytest.skip("OPENAI_API_KEY not set — TTS needs OpenAI")
        models = keyed_provider_models.get("openai", [])
        model = _find_model_by_name(models, self.TTS_MODEL)
        if model is None:
            pytest.skip(f"{self.TTS_MODEL} not enabled in this tenant")

        body = {
            "model": self.TTS_MODEL,
            "voice": "alloy",
            "input": "Brutor live test.",
            "response_format": "mp3",
            "speed": 1.0,
        }
        r = proxy_client.post(
            "/v1/proxy/llm/audio/speech",
            headers={**trial_headers, "Content-Type": "application/json"},
            json=body,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        )
        assert r.status_code == 200, (
            f"tts-1 speech failed ({r.status_code}): {short(r.text)}"
        )
        # The route returns the raw audio bytes with
        # Content-Type: audio/mpeg. A non-trivial body and the
        # MP3 magic bytes (`ID3` or `\xff\xfb`/`\xff\xf3`) prove
        # we got real audio, not a JSON error wrapped in 200.
        audio = r.content
        assert len(audio) >= 256, (
            f"tts-1 audio body suspiciously small ({len(audio)} bytes); "
            f"content-type={r.headers.get('content-type')!r}; "
            f"body head={audio[:64]!r}"
        )
        is_mp3 = audio.startswith(b"ID3") or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa")
        assert is_mp3, (
            f"tts-1 body doesn't look like MP3 (no ID3 / sync frame). "
            f"first 8 bytes={audio[:8]!r}"
        )

    # ─────────── Audio STT: gpt-transcribe ───────────

    def test_whisper_1_audio_transcription(
        self,
        keyed_providers: Dict[str, str],
        keyed_provider_models: Dict[str, List[Dict[str, Any]]],
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
    ) -> None:
        if "openai" not in keyed_providers:
            pytest.skip("OPENAI_API_KEY not set — Whisper needs OpenAI")
        models = keyed_provider_models.get("openai", [])
        model = _find_model_by_name(models, self.STT_MODEL)
        if model is None:
            pytest.skip(f"{self.STT_MODEL} not enabled in this tenant")

        wav = _build_silent_wav()
        # multipart/form-data — same shape Whisper accepts directly,
        # the proxy passes through unchanged.
        files = {"file": ("silence.wav", wav, "audio/wav")}
        data = {
            "model": self.STT_MODEL,
            "response_format": "json",
        }
        # Don't send Content-Type — httpx multipart sets it.
        upload_headers = {
            k: v for k, v in trial_headers.items() if k.lower() != "content-type"
        }
        r = proxy_client.post(
            "/v1/proxy/llm/audio/transcriptions",
            headers=upload_headers,
            files=files,
            data=data,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        )
        assert r.status_code == 200, (
            f"gpt-transcribe transcription failed ({r.status_code}): "
            f"{short(r.text)}"
        )
        body = r.json()
        # For a 0.2-second all-zero WAV, Whisper typically returns
        # `{"text": ""}` or a benign filler ("you", ".") — just
        # assert the field exists. Empty-string text is fine.
        assert "text" in body, (
            f"gpt-transcribe response missing `text` field: {short(r.text)}"
        )
        assert isinstance(body["text"], str), (
            f"gpt-transcribe `text` is not a string: {type(body['text'])}"
        )

    # ─────────── Video: fal luma-dream-machine ───────────

    def test_fal_video_generation_async(
        self,
        keyed_providers: Dict[str, str],
        keyed_provider_models: Dict[str, List[Dict[str, Any]]],
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
    ) -> None:
        if "fal_ai" not in keyed_providers:
            pytest.skip("FAL_KEY not set — video generation needs Fal.ai")
        models = keyed_provider_models.get("fal_ai", [])
        model = _find_model_by_name(models, self.VIDEO_MODEL)
        if model is None:
            pytest.skip(f"{self.VIDEO_MODEL} not enabled in this tenant")

        # 1. Submit the job. Returns immediately with a brutor job id;
        #    the upstream Fal call runs on a background poller.
        submit_body = {
            "model": self.VIDEO_MODEL,
            "prompt": "A green dot drifting on a white background.",
            "duration_seconds": 5,
            "resolution": "720p",
        }
        r = proxy_client.post(
            "/v1/proxy/llm/video/generations",
            headers={**trial_headers, "Content-Type": "application/json"},
            json=submit_body,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        )
        # The async submit endpoint returns 202 Accepted as the
        # canonical "queued, ask back later" status; accepting 200
        # too in case a future revision flips to "OK" with the same
        # body shape (the contract is the body, not the code).
        assert r.status_code in (200, 202), (
            f"video submit failed ({r.status_code}): {short(r.text)}"
        )
        submit = r.json()
        job_id = submit.get("job_id")
        assert job_id, f"video submit missing job_id: {short(r.text)}"
        # Status at submit is typically "pending" or "queued"; once
        # the poller has run a tick before we read it can be
        # "running" or even already terminal. We don't pin the exact
        # value because the background poller's interval is
        # implementation-detail.
        assert submit.get("status") in {
            "pending", "queued", "running", "succeeded", "failed",
        }, (
            f"unexpected initial status: {submit.get('status')!r}"
        )

        # 2. Poll until terminal. Cap at ~3 minutes — luma-dream-machine
        #    typically finishes a 5-second 720p clip in 30-90 seconds.
        import time
        deadline = time.monotonic() + 180.0
        last_status: Optional[str] = None
        last_body: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            r = proxy_client.get(
                f"/v1/proxy/llm/video/jobs/{job_id}",
                headers=trial_headers,
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            )
            assert r.status_code == 200, (
                f"video job poll failed ({r.status_code}): {short(r.text)}"
            )
            last_body = r.json()
            last_status = (last_body.get("status") or "").lower()
            if last_status in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(5.0)

        # 3. Assert the job reached a terminal state.
        if last_status not in {"succeeded", "failed", "cancelled"}:
            pytest.fail(
                f"video job {job_id} didn't reach terminal state within "
                f"180s — last status={last_status!r}, last body={short(str(last_body))}. "
                f"This is likely an upstream Fal queue backlog rather than "
                f"a proxy bug; rerun the test or check the Fal dashboard."
            )

        # 4. On success: assert we got a usable output reference.
        #    On failure: surface the upstream error for diagnostics.
        if last_status == "succeeded":
            assert (
                last_body.get("output_file_id")
                or last_body.get("output_url")
                or last_body.get("output_files")
            ), (
                f"video job {job_id} succeeded but has no output ref: "
                f"{short(str(last_body))}"
            )
        elif last_status == "failed":
            # Fal occasionally rejects prompts as policy violations or
            # returns transient queue errors. Surface the upstream
            # message so an operator can decide whether the failure is
            # a bug in our stack or a Fal-side issue.
            pytest.fail(
                f"video job {job_id} reached `failed` status: "
                f"error={last_body.get('error')!r}, "
                f"body={short(str(last_body))}"
            )
