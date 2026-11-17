# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the per-surface override layer (migration
``a075_per_surface_overrides``).

Covers:
  * The Python resolver (`app/services/guardrails/per_surface.py`) —
    every fallback path, dict + ORM-shape configs, banned-words list
    replacement, malformed JSON defence.
  * The Pydantic validator (`GuardrailConfigUpdate.*_per_surface`) —
    rejects unknown surface tokens, unknown keys per check, bad
    action strings; accepts well-formed overrides.

These pin the contract Rust + admin UI rely on. Cross-check: the
Rust unit tests in `services/guardrails/per_surface.rs::tests` cover
the same matrix from the other side; a behaviour drift between the
two languages will fail at least one of the two suites.
"""

from __future__ import annotations

import importlib.util

import pytest

# Load the per_surface module WITHOUT triggering the package __init__
# (which pulls in httpx + DB config; not needed for pure-function
# resolver tests).
_spec = importlib.util.spec_from_file_location(
    "per_surface",
    "app/services/guardrails/per_surface.py",
)
_per_surface = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_per_surface)  # type: ignore[union-attr]

resolve = _per_surface.resolve
resolve_words = _per_surface.resolve_words
resolve_patterns = _per_surface.resolve_patterns
has_any_per_surface_override = _per_surface.has_any_per_surface_override


# ---------------------------------------------------------------------------
# Resolver — happy paths
# ---------------------------------------------------------------------------


class TestResolveScalarFallback:
    def test_scalar_action_returned_when_no_override(self):
        cfg = {"pii_action": "redact", "pii_per_surface": None}
        assert resolve(cfg, "pii", "chat_input", "action") == "redact"

    def test_scalar_provider_returned_when_no_override(self):
        cfg = {"pii_provider": "built_in", "pii_per_surface": None}
        assert resolve(cfg, "pii", "chat_input", "provider") == "built_in"

    def test_missing_scalar_returns_field_default(self):
        # No scalar AND no override → built-in default per field.
        cfg = {"pii_per_surface": None}
        assert resolve(cfg, "pii", "chat_input", "action") == "warn"
        assert resolve(cfg, "pii", "chat_input", "provider") == "built_in"


class TestResolveOverrideWins:
    def test_action_override_beats_scalar(self):
        cfg = {
            "pii_action": "redact",
            "pii_per_surface": {"chat_output": {"action": "block"}},
        }
        # llm_input has no override → scalar.
        assert resolve(cfg, "pii", "chat_input", "action") == "redact"
        # llm_output has override → override.
        assert resolve(cfg, "pii", "chat_output", "action") == "block"

    def test_provider_override_isolated_from_action(self):
        cfg = {
            "pii_action": "redact",
            "pii_provider": "built_in",
            "pii_per_surface": {"chat_output": {"provider": "presidio"}},
        }
        # llm_output: action falls through to scalar; provider overridden.
        assert resolve(cfg, "pii", "chat_output", "action") == "redact"
        assert resolve(cfg, "pii", "chat_output", "provider") == "presidio"
        # mcp_input: no override → both come from scalars.
        assert resolve(cfg, "pii", "mcp_input", "action") == "redact"
        assert resolve(cfg, "pii", "mcp_input", "provider") == "built_in"

    def test_threshold_override_for_toxic(self):
        cfg = {
            "toxic_content_threshold": 0.5,
            "toxic_content_per_surface": {"chat_input": {"threshold": 0.7}},
        }
        assert resolve(cfg, "toxic_content", "chat_input", "threshold") == 0.7
        assert resolve(cfg, "toxic_content", "chat_output", "threshold") == 0.5

    def test_enabled_override_can_force_disable_on_one_surface(self):
        cfg = {
            "pii_detection_enabled": True,
            "pii_per_surface": {"mcp_output": {"enabled": False}},
        }
        assert resolve(cfg, "pii", "chat_input", "enabled") is True
        assert resolve(cfg, "pii", "mcp_output", "enabled") is False


class TestResolveBannedListReplacement:
    """`words`/`patterns` per-surface entries REPLACE the global list
    (no merge). Real demo case: 'competitor names' list only on
    llm_output, 'DELETE FROM' only on mcp_input."""

    def test_banned_words_per_surface_replaces_global(self):
        cfg = {
            "banned_words": ["secret", "password"],
            "banned_words_per_surface": {
                "mcp_input": {"words": ["DELETE FROM", "DROP TABLE"]},
            },
        }
        # Per-surface entry replaces.
        assert resolve_words(cfg, "mcp_input") == ["DELETE FROM", "DROP TABLE"]
        # Other surface gets the global list.
        assert resolve_words(cfg, "chat_input") == ["secret", "password"]

    def test_banned_patterns_per_surface_replaces_global(self):
        cfg = {
            "banned_patterns": ["foo.*"],
            "banned_patterns_per_surface": {
                "chat_output": {"patterns": ["competitor_a", "competitor_b"]},
            },
        }
        assert resolve_patterns(cfg, "chat_output") == ["competitor_a", "competitor_b"]
        assert resolve_patterns(cfg, "chat_input") == ["foo.*"]

    def test_no_overrides_returns_global_lists(self):
        cfg = {
            "banned_words": ["x", "y"],
            "banned_patterns": ["a.*"],
            "banned_words_per_surface": None,
            "banned_patterns_per_surface": None,
        }
        assert resolve_words(cfg, "chat_input") == ["x", "y"]
        assert resolve_patterns(cfg, "chat_input") == ["a.*"]

    def test_neither_global_nor_override_yields_empty(self):
        cfg = {"banned_words": None, "banned_words_per_surface": None}
        assert resolve_words(cfg, "chat_input") == []


# ---------------------------------------------------------------------------
# Resolver — defensive / unhappy paths
# ---------------------------------------------------------------------------


class TestResolveDefensive:
    def test_unknown_check_falls_through_to_default(self):
        cfg = {"pii_action": "block"}
        assert resolve(cfg, "no_such_check", "chat_input", "action") == "warn"

    def test_unknown_surface_falls_through_to_default(self):
        cfg = {"pii_action": "block"}
        # Unknown surface — scalar isn't consulted; system default wins.
        assert resolve(cfg, "pii", "some_made_up_surface", "action") == "warn"

    def test_malformed_per_surface_not_a_dict_falls_through(self):
        cfg = {"pii_action": "redact", "pii_per_surface": "garbage"}
        assert resolve(cfg, "pii", "chat_input", "action") == "redact"

    def test_per_surface_entry_not_a_dict_falls_through(self):
        cfg = {
            "pii_action": "redact",
            "pii_per_surface": {"chat_input": "not_an_object"},
        }
        assert resolve(cfg, "pii", "chat_input", "action") == "redact"

    def test_null_override_value_falls_through_to_scalar(self):
        # Operator stores `null` explicitly under a field — treated as
        # "no override on this field" (matches Pydantic's None=absent).
        cfg = {
            "pii_action": "redact",
            "pii_per_surface": {"chat_input": {"action": None}},
        }
        assert resolve(cfg, "pii", "chat_input", "action") == "redact"

    def test_has_any_per_surface_override(self):
        empty = {"pii_per_surface": None}
        assert has_any_per_surface_override(empty) is False
        active = {"pii_per_surface": {"chat_input": {"action": "block"}}}
        assert has_any_per_surface_override(active) is True


# ---------------------------------------------------------------------------
# Pydantic validator — admin API contract
# ---------------------------------------------------------------------------


class TestPydanticValidator:
    """The `*_per_surface` fields on GuardrailConfigCreate/Update
    validate the JSONB shape at save time so a typo can't make it to
    the runtime path. These tests pin the rejection rules."""

    def _make(self, **kwargs):
        # GuardrailBuiltinConfig owns the per-check scalars + the
        # new `*_per_surface` fields. Build a minimum-viable instance
        # and override only the per-surface field under test.
        from app.api.v1.models.admin.guardrail import GuardrailBuiltinConfig
        return GuardrailBuiltinConfig(**kwargs)

    def test_accepts_well_formed_override(self):
        cfg = self._make(pii_per_surface={"chat_input": {"action": "block"}})
        assert cfg.pii_per_surface == {"chat_input": {"action": "block"}}

    def test_rejects_unknown_surface_token(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc:
            self._make(pii_per_surface={"bogus_surface": {"action": "block"}})
        assert "unknown surface token" in str(exc.value).lower()

    def test_rejects_unknown_field_in_override(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc:
            self._make(pii_per_surface={"chat_input": {"foo": "bar"}})
        assert "unknown keys" in str(exc.value).lower()

    def test_rejects_provider_on_secrets(self):
        # Secrets has no provider key — Pydantic should reject.
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc:
            self._make(secrets_per_surface={"chat_input": {"provider": "presidio"}})
        assert "unknown keys" in str(exc.value).lower()

    def test_rejects_words_on_pii(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc:
            self._make(pii_per_surface={"chat_input": {"words": ["foo"]}})
        assert "unknown keys" in str(exc.value).lower()

    def test_rejects_invalid_action_string(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc:
            self._make(pii_per_surface={"chat_input": {"action": "delete_everything"}})
        # Validator surfaces the action allow-list in the error.
        assert "action must be one of" in str(exc.value).lower()

    def test_action_normalised_to_lowercase(self):
        cfg = self._make(pii_per_surface={"chat_input": {"action": "BLOCK"}})
        assert cfg.pii_per_surface["chat_input"]["action"] == "block"

    def test_accepts_banned_words_per_surface(self):
        cfg = self._make(banned_words_per_surface={
            "mcp_input": {"words": ["DELETE FROM", "DROP TABLE"]},
        })
        assert cfg.banned_words_per_surface == {
            "mcp_input": {"words": ["DELETE FROM", "DROP TABLE"]},
        }

    def test_accepts_threshold_on_toxic(self):
        cfg = self._make(toxic_content_per_surface={
            "chat_input": {"threshold": 0.7, "action": "block"},
        })
        assert cfg.toxic_content_per_surface["chat_input"]["threshold"] == 0.7

    def test_accepts_threshold_on_pii(self):
        # PII carries a threshold for the Presidio path.
        # The built-in regex detector ignores it, but the per-surface
        # entry must validate so an operator can override the scalar
        # value when the effective provider is Presidio.
        cfg = self._make(pii_per_surface={"chat_input": {"threshold": 0.7}})
        assert cfg.pii_per_surface["chat_input"]["threshold"] == 0.7

    def test_none_accepted_as_no_overrides(self):
        cfg = self._make(pii_per_surface=None)
        assert cfg.pii_per_surface is None

    def test_empty_dict_accepted_as_no_overrides(self):
        cfg = self._make(pii_per_surface={})
        assert cfg.pii_per_surface == {}
