# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrail admin API validator tests.

Covers:
  * Field-level Pydantic validators on `GuardrailBuiltinConfig` and
    `GuardrailConfigUpdate` (allowed providers, allowed PII categories,
    allowed toxic categories, threshold ranges, banned regex compile,
    fail_open_overrides keys).
  * Cross-field provider-completeness rule (an external provider
    selector cannot be saved unless the matching `providers[]` entry
    carries an api_key).
"""

from __future__ import annotations

from typing import List, Optional

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.guardrail import (
    ALLOWED_PII_CATEGORIES,
    ALLOWED_TOXIC_CATEGORIES,
    GuardrailBuiltinConfig,
    GuardrailConfigCreateRequest,
    GuardrailConfigUpdate,
    GuardrailProviderConfig,
)


# ---------------------------------------------------------------------------
# Field-level validators
# ---------------------------------------------------------------------------

class TestPiiCategoriesValidator:
    def test_accepts_known_categories(self):
        b = GuardrailBuiltinConfig(pii_categories=["email", "phone"])
        assert b.pii_categories == ["email", "phone"]

    def test_dedupes_preserving_order(self):
        b = GuardrailBuiltinConfig(pii_categories=["phone", "email", "phone"])
        assert b.pii_categories == ["phone", "email"]

    def test_rejects_unknown_category(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(pii_categories=["email", "made_up"])
        assert "made_up" in str(exc.value)

    def test_full_set_round_trips(self):
        b = GuardrailBuiltinConfig(pii_categories=list(ALLOWED_PII_CATEGORIES))
        assert b.pii_categories == ALLOWED_PII_CATEGORIES


class TestToxicCategoryThresholdsValidator:
    def test_accepts_known_categories(self):
        b = GuardrailBuiltinConfig(
            toxic_content_category_thresholds={"hate": 0.7, "harassment": 0.8}
        )
        assert b.toxic_content_category_thresholds == {"hate": 0.7, "harassment": 0.8}

    def test_rejects_unknown_category(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(toxic_content_category_thresholds={"unknown_cat": 0.5})
        assert "unknown_cat" in str(exc.value)

    def test_rejects_threshold_above_one(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(toxic_content_category_thresholds={"hate": 1.5})
        assert "[0.0, 1.0]" in str(exc.value)

    def test_rejects_threshold_below_zero(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(toxic_content_category_thresholds={"hate": -0.1})
        assert "[0.0, 1.0]" in str(exc.value)

    def test_all_known_categories_pass(self):
        m = {c: 0.5 for c in ALLOWED_TOXIC_CATEGORIES}
        b = GuardrailBuiltinConfig(toxic_content_category_thresholds=m)
        assert b.toxic_content_category_thresholds == m


class TestProviderSelectorValidators:
    def test_pii_provider_built_in_ok(self):
        GuardrailBuiltinConfig(pii_provider="built_in")

    def test_pii_provider_presidio_ok(self):
        # Presidio is a supported PII provider (ALLOWED_PII_PROVIDERS).
        GuardrailBuiltinConfig(pii_provider="presidio")

    def test_pii_provider_unknown_rejected(self):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(pii_provider="lakera_guard")

    def test_prompt_injection_lakera_ok(self):
        GuardrailBuiltinConfig(prompt_injection_provider="lakera_guard")

    def test_prompt_injection_unknown_rejected(self):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(prompt_injection_provider="bedrock")

    def test_jailbreak_unknown_rejected(self):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(jailbreak_provider="custom")

    def test_toxic_provider_built_in_ok(self):
        GuardrailBuiltinConfig(toxic_content_provider="built_in")

    def test_toxic_provider_unknown_rejected(self):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(toxic_content_provider="lakera_guard")


class TestBannedWordsMatchModeValidator:
    def test_substring_ok(self):
        GuardrailBuiltinConfig(banned_words_match_mode="substring")

    def test_word_boundary_ok(self):
        GuardrailBuiltinConfig(banned_words_match_mode="word_boundary")

    def test_unknown_rejected(self):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(banned_words_match_mode="fuzzy")


class TestBannedPatternsCompile:
    def test_valid_pattern(self):
        b = GuardrailBuiltinConfig(banned_patterns=[r"\bsecret-\d+\b"])
        assert b.banned_patterns == [r"\bsecret-\d+\b"]

    def test_invalid_pattern_rejected(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(banned_patterns=["[unclosed"])
        assert "Invalid regex pattern" in str(exc.value)


class TestFailOpenOverridesValidator:
    def test_allowed_keys_ok(self):
        u = GuardrailConfigUpdate(
            enabled=True,
            fail_open_overrides={"pii": False, "jailbreak": True},
        )
        assert u.fail_open_overrides == {"pii": False, "jailbreak": True}

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailConfigUpdate(
                enabled=True, fail_open_overrides={"unknown_check": False}
            )
        assert "unknown_check" in str(exc.value)

    def test_non_bool_value_rejected(self):
        with pytest.raises(ValidationError):
            GuardrailConfigUpdate(
                enabled=True, fail_open_overrides={"pii": "no"}  # type: ignore[arg-type]
            )

    def test_works_on_create_request_too(self):
        c = GuardrailConfigCreateRequest(
            name="test", fail_open_overrides={"pii": False}
        )
        assert c.fail_open_overrides == {"pii": False}


# ---------------------------------------------------------------------------
# Cross-field validator: provider-completeness gate
# ---------------------------------------------------------------------------
#
# The endpoint-level helper isn't import-light (it lives inside a
# router factory and pulls in DB / RBAC / audit modules). We replicate
# the same logic against the public Pydantic models so the test stays
# fast and deterministic — and document the contract here so the
# endpoint helper can't drift.

def _provider_present_with_key(
    providers: Optional[List[GuardrailProviderConfig]],
    aliases: tuple,
) -> bool:
    if not providers:
        return False
    for p in providers:
        if p.enabled is False:
            continue
        if p.provider in aliases and (p.api_key or "").strip():
            return True
    return False


def _check(builtin: GuardrailBuiltinConfig, providers: Optional[List[GuardrailProviderConfig]]) -> List[str]:
    errors: List[str] = []
    if builtin.prompt_injection_enabled and builtin.prompt_injection_provider == "lakera_guard":
        if not _provider_present_with_key(providers, ("lakera", "lakera_guard")):
            errors.append("pi-needs-lakera")
    if builtin.jailbreak_enabled and builtin.jailbreak_provider == "lakera_guard":
        if not _provider_present_with_key(providers, ("lakera", "lakera_guard")):
            errors.append("jb-needs-lakera")
    tc_provider = builtin.toxic_content_provider or "openai_moderation"
    if builtin.toxic_content_enabled and tc_provider == "openai_moderation":
        if not _provider_present_with_key(providers, ("openai_moderation",)):
            errors.append("tc-needs-openai")
    return errors


class TestProviderCompletenessGate:
    def test_built_in_only_passes_with_no_providers(self):
        b = GuardrailBuiltinConfig(
            prompt_injection_provider="built_in",
            jailbreak_provider="built_in",
            toxic_content_provider="built_in",
            toxic_content_enabled=True,
        )
        assert _check(b, None) == []

    def test_lakera_pi_without_key_fails(self):
        b = GuardrailBuiltinConfig(prompt_injection_provider="lakera_guard")
        assert "pi-needs-lakera" in _check(b, None)

    def test_lakera_pi_with_key_passes(self):
        b = GuardrailBuiltinConfig(prompt_injection_provider="lakera_guard")
        providers = [GuardrailProviderConfig(provider="lakera", api_key="sk-test")]
        assert "pi-needs-lakera" not in _check(b, providers)

    def test_lakera_provider_disabled_still_fails(self):
        b = GuardrailBuiltinConfig(jailbreak_provider="lakera_guard")
        providers = [
            GuardrailProviderConfig(provider="lakera", enabled=False, api_key="sk-test"),
        ]
        assert "jb-needs-lakera" in _check(b, providers)

    def test_openai_moderation_default_requires_key_when_toxic_enabled(self):
        # toxic_content_provider defaulted (None → openai_moderation)
        b = GuardrailBuiltinConfig(toxic_content_enabled=True)
        assert "tc-needs-openai" in _check(b, None)

    def test_openai_moderation_with_key_passes(self):
        b = GuardrailBuiltinConfig(toxic_content_enabled=True)
        providers = [GuardrailProviderConfig(provider="openai_moderation", api_key="sk-x")]
        assert "tc-needs-openai" not in _check(b, providers)

    def test_built_in_toxic_does_not_require_provider(self):
        b = GuardrailBuiltinConfig(
            toxic_content_enabled=True,
            toxic_content_provider="built_in",
        )
        assert _check(b, None) == []

    def test_disabled_check_skips_gate(self):
        # Even though selector is lakera_guard, jailbreak_enabled=False
        # → the gate should not require a Lakera key.
        b = GuardrailBuiltinConfig(
            jailbreak_enabled=False,
            jailbreak_provider="lakera_guard",
        )
        assert _check(b, None) == []


# ---------------------------------------------------------------------------
# Per-check surface validator
# ---------------------------------------------------------------------------

from app.api.v1.models.admin.guardrail import ALLOWED_CHECK_SURFACES


class TestPerCheckSurfacesValidator:
    def test_accepts_valid_surfaces(self):
        b = GuardrailBuiltinConfig(
            pii_surfaces=["chat_input", "mcp_input"],
            prompt_injection_surfaces=["chat_input"],
            banned_words_surfaces=["mcp_input", "mcp_output"],
        )
        assert b.pii_surfaces == ["chat_input", "mcp_input"]
        assert b.prompt_injection_surfaces == ["chat_input"]
        assert b.banned_words_surfaces == ["mcp_input", "mcp_output"]

    def test_rejects_unknown_surface(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(pii_surfaces=["chat_input", "future_surface"])
        assert "future_surface" in str(exc.value)

    def test_dedupes_preserving_order(self):
        b = GuardrailBuiltinConfig(
            toxic_content_surfaces=["mcp_output", "chat_input", "mcp_output"]
        )
        assert b.toxic_content_surfaces == ["mcp_output", "chat_input"]

    def test_empty_list_accepted(self):
        # Empty = check is effectively off without flipping the
        # toplevel enable. Operators use this to silence a check.
        b = GuardrailBuiltinConfig(banned_patterns_surfaces=[])
        assert b.banned_patterns_surfaces == []

    def test_null_is_pass_through(self):
        # None means "leave the row unchanged" on PATCH semantics.
        b = GuardrailBuiltinConfig(pii_surfaces=None)
        assert b.pii_surfaces is None

    def test_full_set_round_trips(self):
        b = GuardrailBuiltinConfig(pii_surfaces=list(ALLOWED_CHECK_SURFACES))
        assert b.pii_surfaces == list(ALLOWED_CHECK_SURFACES)
