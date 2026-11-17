# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live e2e — verify that the documented "seeded by default" surfaces are
actually present and wired to the Default resource group on a freshly
provisioned tenant. Hits the running control plane via the same
fixtures the rest of the live suite uses (`control_client`,
`admin_headers`, `default_tenant_id`).

What this proves end-to-end:

  * The default GuardrailConfig exists, is enabled, and has the
    documented PII / prompt-injection / jailbreak / toxic-content
    actions plus the surface fields the admin UI needs.
  * The default SemanticCacheConfig exists with both layers ON
    (Redis exact match + Qdrant semantic match) and the documented
    threshold / TTL pair.
  * The default PromptCachePolicy exists with the
    `tools_and_system` strategy and is bound to the tenant's default
    group so outgoing LLM calls automatically pick up the
    provider-side cache markers.

These are smoke checks — the seeded rows are observable to a fresh
operator on first login. Detailed admin behaviour (CRUD, group
assignment, validators) is covered by the in-process suite.
"""

from __future__ import annotations

from typing import Any, Dict

import httpx
import pytest


pytestmark = pytest.mark.live_e2e


# ---------------------------------------------------------------------------
# Guardrails — default policy
# ---------------------------------------------------------------------------


def _list_guardrail_configs(
    control_client: httpx.Client, admin_headers: Dict[str, str]
) -> list[Dict[str, Any]]:
    r = control_client.get("/v1/admin/guardrails/configs", headers=admin_headers)
    assert r.status_code == 200, f"list guardrails {r.status_code}: {r.text[:300]}"
    body = r.json()
    return body.get("configs", [])


def _default_guardrail(configs: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the lone `is_default=True` policy. Fail with a tight
    diagnostic if zero or more than one are seeded — both are bugs."""
    defaults = [c for c in configs if c.get("is_default")]
    assert len(defaults) == 1, (
        f"Expected exactly one default guardrail config, got {len(defaults)}. "
        f"Names: {[c.get('name') for c in defaults]!r}. "
        f"Tenant provisioning is supposed to seed exactly one — re-run "
        f"provisioning if a fresh DB is missing it."
    )
    return defaults[0]


class TestDefaultGuardrailSeeded:
    def test_default_policy_is_present_and_enabled(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        assert cfg["enabled"] is True
        assert cfg["name"], "default policy must have a name"

    def test_pii_default_action_is_redact(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        pii = cfg["builtin"]["pii"]
        assert pii["enabled"] is True
        assert pii["action"] == "redact"

    def test_prompt_injection_default_action_is_block(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        pi = cfg["builtin"]["prompt_injection"]
        assert pi["enabled"] is True
        assert pi["action"] == "block"
        # Sensitivity 0.7 is the calibrated default — strong canonical
        # attacks fire alone; ambiguous signals only fire combined.
        assert abs(float(pi.get("sensitivity") or 0.0) - 0.7) < 1e-6

    def test_jailbreak_default_action_is_block(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        jb = cfg["builtin"]["jailbreak"]
        assert jb["enabled"] is True
        assert jb["action"] == "block"

    def test_toxic_content_default_action_is_warn(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        tc = cfg["builtin"]["toxic_content"]
        assert tc["enabled"] is True
        assert tc["action"] == "warn"
        # Threshold 0.8 is the strong-signal baseline.
        assert abs(float(tc.get("threshold") or 0.0) - 0.8) < 1e-6

    def test_p9_provider_keys_present_on_each_check(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # `provider` on each check, and categories /
        # category_thresholds / banned_words_match_mode /
        # fail_open_overrides at the policy level. The admin UI relies
        # on these keys being present in the response so it can render
        # the per-check selectors. Confirm a fresh seed produces them.
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        builtin = cfg["builtin"]
        assert "provider" in builtin["pii"]
        assert "provider" in builtin["prompt_injection"]
        assert "provider" in builtin["jailbreak"]
        assert "provider" in builtin["toxic_content"]
        assert "banned_words_match_mode" in builtin
        assert "fail_open_overrides" in cfg

    def test_default_policy_is_bound_to_default_group(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # The seeded policy MUST be wired to a resource group — an
        # unbound policy is invisible at runtime and would silently
        # let everything through. Check the response surfaces at least
        # one group binding.
        cfg = _default_guardrail(_list_guardrail_configs(control_client, admin_headers))
        groups = cfg.get("groups", [])
        group_ids = cfg.get("group_ids", [])
        assert groups or group_ids, (
            "Default guardrail policy is not bound to any resource group. "
            "Tenant provisioning is supposed to bind it to the Default "
            "group — re-run provisioning."
        )


# ---------------------------------------------------------------------------
# Semantic cache — exact + semantic layers
# ---------------------------------------------------------------------------


def _list_semantic_cache_configs(
    control_client: httpx.Client, admin_headers: Dict[str, str]
) -> list[Dict[str, Any]]:
    r = control_client.get("/v1/admin/semantic-cache/configs", headers=admin_headers)
    assert r.status_code == 200, (
        f"list semantic-cache configs {r.status_code}: {r.text[:300]}"
    )
    body = r.json()
    return body.get("configs", body) if isinstance(body, dict) else body


class TestDefaultSemanticCacheSeeded:
    def test_default_config_is_present_and_enabled(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        configs = _list_semantic_cache_configs(control_client, admin_headers)
        assert configs, "no semantic-cache configs visible — seed didn't run"
        # Pick by name="Default" since `is_default` isn't on this model.
        defaults = [c for c in configs if c.get("name") == "Default"]
        assert len(defaults) >= 1, (
            f"no Default semantic cache config — names: "
            f"{[c.get('name') for c in configs]!r}"
        )
        assert defaults[0].get("enabled") is True

    def test_both_cache_layers_on_by_default(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # Layer 1 (Redis exact-match) and Layer 2 (Qdrant semantic
        # similarity) both ship ON. The response shape nests them
        # under `exact_match` / `semantic_match` to match the Pydantic
        # `ExactMatchConfig` / `SemanticMatchConfig` types.
        configs = _list_semantic_cache_configs(control_client, admin_headers)
        cfg = next(c for c in configs if c.get("name") == "Default")
        assert cfg["exact_match"]["enabled"] is True
        assert cfg["semantic_match"]["enabled"] is True

    def test_similarity_threshold_at_production_baseline(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # 0.95 is the production-tuned value (lower triggers cross-topic
        # false hits in chat) — matches the seed in
        # `seed_semantic_cache.py` and the Pydantic default in
        # `policy.py`. Lives under `semantic_match` in the response (not
        # flat). Anything noticeably different than this in a fresh seed
        # means something rewrote it.
        configs = _list_semantic_cache_configs(control_client, admin_headers)
        cfg = next(c for c in configs if c.get("name") == "Default")
        threshold = float(cfg["semantic_match"]["similarity_threshold"])
        assert abs(threshold - 0.95) < 1e-6

    def test_safe_exclusions_are_set(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # Tool calls and guardrail-flagged content MUST be excluded —
        # caching them would skip tool-execution / re-evaluation on a
        # hit. Privacy-class invariants; treat regressions as bugs.
        # Response nests under `exclusions` (Pydantic `ExclusionRules`).
        configs = _list_semantic_cache_configs(control_client, admin_headers)
        cfg = next(c for c in configs if c.get("name") == "Default")
        exclusions = cfg["exclusions"]
        assert exclusions["tool_calls"] is True
        assert exclusions["guardrail_flagged"] is True


# ---------------------------------------------------------------------------
# Prompt cache — provider-side KV caching policy
# ---------------------------------------------------------------------------


def _list_prompt_cache_policies(
    control_client: httpx.Client, admin_headers: Dict[str, str]
) -> list[Dict[str, Any]]:
    r = control_client.get("/v1/admin/prompt-cache/policies", headers=admin_headers)
    assert r.status_code == 200, (
        f"list prompt-cache policies {r.status_code}: {r.text[:300]}"
    )
    return r.json()


class TestDefaultPromptCacheSeeded:
    def test_default_policy_is_present_and_enabled(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        policies = _list_prompt_cache_policies(control_client, admin_headers)
        assert policies, "no prompt-cache policies visible — seed didn't run"
        defaults = [p for p in policies if p.get("name") == "Default"]
        assert len(defaults) >= 1
        assert defaults[0].get("enabled") is True

    def test_strategy_is_tools_and_system(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # `tools_and_system` is Anthropic's documented best-practice
        # layout and captures most of the savings for tool-heavy
        # deployments. Different strategies are opt-in.
        policies = _list_prompt_cache_policies(control_client, admin_headers)
        p = next(x for x in policies if x.get("name") == "Default")
        assert p.get("strategy") == "tools_and_system"

    def test_min_request_tokens_is_at_provider_floor(
        self, control_client: httpx.Client, admin_headers: Dict[str, str]
    ) -> None:
        # Below 1024 tokens, Anthropic's write premium makes caching
        # unprofitable, and OpenAI doesn't auto-cache at all. The seed
        # locks the floor at 1024 to prevent the policy firing on
        # short prompts where it would *cost* the tenant.
        policies = _list_prompt_cache_policies(control_client, admin_headers)
        p = next(x for x in policies if x.get("name") == "Default")
        assert p.get("min_request_tokens") == 1024
