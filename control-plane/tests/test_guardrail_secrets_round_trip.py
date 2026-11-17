# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Guardrail secrets-detection round-trip

Pins the two contracts the admin endpoint relies on for secrets
detection:

  1. Request schema → SQLAlchemy attribute   (write path)
  2. SQLAlchemy attribute → Response schema  (read path)

Auth + tenant-isolation are orthogonal and already covered by
the existing infrastructure.

Defaults are particularly load-bearing here: secrets is opt-in
per tenant so the response builder MUST surface the documented
default category set on a row that was created pre-a068 (NULL
secrets_categories) AND on a row created via the modern endpoint
that doesn't set categories explicitly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.admin.guardrails import _config_to_response
from app.api.v1.models.admin.guardrail import (
    ALLOWED_SECRETS_CATEGORIES,
    GuardrailBuiltinConfig,
    GuardrailConfigCreateRequest,
)
from app.database.models.guardrail_config import (
    ALL_SURFACES,
    DEFAULT_SECRETS_CATEGORIES,
    GuardrailConfig,
)


def _bare_config(**overrides) -> GuardrailConfig:
    defaults = dict(
        id="gc-secrets",
        tenant_id="t1",
        name="probe",
        is_default=True,
        enabled=True,
        priority=0,
        chat_input_enabled=True,
        chat_output_enabled=True,
        embeddings_input_enabled=True,
        image_gen_input_enabled=True,
        image_gen_output_enabled=False,
        audio_tts_input_enabled=True,
        audio_tts_output_enabled=False,
        audio_stt_input_enabled=True,
        audio_stt_output_enabled=True,
        video_gen_input_enabled=True,
        video_gen_output_enabled=False,
        batch_input_enabled=True,
        batch_output_enabled=False,
        moderation_input_enabled=True,
        mcp_input_enabled=True,
        mcp_output_enabled=True,
        mcp_registration_enabled=True,
        skill_input_enabled=True,
        skill_output_enabled=True,
        a2a_inbound_enabled=False,
        a2a_outbound_enabled=False,
        pii_detection_enabled=True,
        pii_action="redact",
        prompt_injection_enabled=True,
        jailbreak_detection_enabled=True,
        toxic_content_enabled=False,
        fail_open=True,
        log_all_checks=False,
        max_check_timeout_ms=2000.0,
        # secrets defaults: disabled, redact, NULL surfaces & categories
        secrets_detection_enabled=False,
        secrets_action="redact",
        secrets_categories=None,
        secrets_surfaces=None,
        secrets_threshold=0.8,
    )
    defaults.update(overrides)
    return GuardrailConfig(**defaults)


# ---------------------------------------------------------------------------
# Request schema validators
# ---------------------------------------------------------------------------


class TestRequestValidatorsRejectBadInput:
    @pytest.mark.parametrize(
        "bad",
        ["nope", "made_up_category", "AWS_ACCESS_KEY", "aws-access-key"],
    )
    def test_rejects_unknown_secrets_category(self, bad: str):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(secrets_categories=["aws_access_key", bad])
        assert bad in str(exc.value)

    def test_accepts_full_allowed_set(self):
        # Round-trip the entire allow-list — important because the
        # admin UI's category picker will send the full list when
        # operators tick every box.
        b = GuardrailBuiltinConfig(secrets_categories=list(ALLOWED_SECRETS_CATEGORIES))
        assert b.secrets_categories == list(ALLOWED_SECRETS_CATEGORIES)

    def test_dedupes_preserving_order(self):
        # Operators paste lists with duplicates from copy-paste —
        # the validator should silently de-dupe.
        b = GuardrailBuiltinConfig(
            secrets_categories=["jwt", "aws_access_key", "jwt", "github_token"]
        )
        assert b.secrets_categories == ["jwt", "aws_access_key", "github_token"]

    def test_null_categories_is_pass_through(self):
        # Null = "leave the row's value alone on PATCH" — the
        # endpoint mapping depends on this semantic.
        b = GuardrailBuiltinConfig(secrets_categories=None)
        assert b.secrets_categories is None

    def test_empty_categories_accepted_as_silence_signal(self):
        # Empty list = operator deliberately disabled all
        # categories without flipping the toplevel toggle. The
        # detector accepts the empty set and never fires; this
        # must survive the request schema unchanged.
        b = GuardrailBuiltinConfig(secrets_categories=[])
        assert b.secrets_categories == []

    @pytest.mark.parametrize("action", ["block", "redact", "warn", "allow"])
    def test_accepts_each_canonical_action(self, action: str):
        b = GuardrailBuiltinConfig(secrets_action=action)
        assert b.secrets_action == action

    def test_rejects_invalid_action(self):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(secrets_action="ignore")  # not in allowed set

    @pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0])
    def test_accepts_threshold_in_range(self, threshold: float):
        b = GuardrailBuiltinConfig(secrets_threshold=threshold)
        assert b.secrets_threshold == threshold

    @pytest.mark.parametrize("threshold", [-0.1, 1.1, 2.0])
    def test_rejects_threshold_out_of_range(self, threshold: float):
        with pytest.raises(ValidationError):
            GuardrailBuiltinConfig(secrets_threshold=threshold)


# ---------------------------------------------------------------------------
# Response builder — read path
# ---------------------------------------------------------------------------


class TestResponseDefaultsToBackfilledValues:
    def test_null_categories_falls_back_to_default_set(self):
        resp = _config_to_response(_bare_config(secrets_categories=None))
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.categories == DEFAULT_SECRETS_CATEGORIES

    def test_null_surfaces_falls_back_to_all_surfaces(self):
        resp = _config_to_response(_bare_config(secrets_surfaces=None))
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.surfaces == ALL_SURFACES

    def test_null_threshold_falls_back_to_eight_tenths(self):
        resp = _config_to_response(_bare_config(secrets_threshold=None))
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.threshold == 0.8

    def test_default_enabled_is_false(self):
        # Secrets ships disabled per tenant. Operators opt in via
        # the admin UI / API; a NULL/default row must NOT surface
        # as enabled.
        resp = _config_to_response(_bare_config())
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.enabled is False


class TestResponseExplicitValuesPreserved:
    def test_explicit_category_subset(self):
        resp = _config_to_response(
            _bare_config(secrets_categories=["aws_access_key", "github_token"])
        )
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.categories == ["aws_access_key", "github_token"]

    def test_explicit_empty_categories_preserved(self):
        # Empty list = "no categories fire" — must NOT be replaced
        # by defaults on the response or the operator's silence
        # signal is lost.
        resp = _config_to_response(_bare_config(secrets_categories=[]))
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.categories == []

    def test_explicit_surface_subset_preserved(self):
        resp = _config_to_response(
            _bare_config(secrets_surfaces=["chat_input", "mcp_input"])
        )
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.surfaces == ["chat_input", "mcp_input"]

    @pytest.mark.parametrize("action", ["block", "redact", "warn", "allow"])
    def test_action_round_trips(self, action: str):
        resp = _config_to_response(_bare_config(secrets_action=action))
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.action == action

    def test_enabled_round_trips(self):
        resp = _config_to_response(_bare_config(secrets_detection_enabled=True))
        assert resp.builtin.secrets is not None
        assert resp.builtin.secrets.enabled is True


# ---------------------------------------------------------------------------
# Create request → endpoint payload shape
# ---------------------------------------------------------------------------


class TestCreateRequestCarriesSecretsFields:
    def test_explicit_secrets_fields_round_trip(self):
        req = GuardrailConfigCreateRequest(
            name="probe",
            builtin=GuardrailBuiltinConfig(
                secrets_enabled=True,
                secrets_action="block",
                secrets_categories=["aws_access_key", "github_token"],
                secrets_surfaces=["chat_input"],
                secrets_threshold=0.6,
            ),
        )
        assert req.builtin.secrets_enabled is True
        assert req.builtin.secrets_action == "block"
        assert req.builtin.secrets_categories == ["aws_access_key", "github_token"]
        assert req.builtin.secrets_surfaces == ["chat_input"]
        assert req.builtin.secrets_threshold == 0.6

    def test_bare_request_leaves_optional_fields_null(self):
        # Untouched fields stay None so a PATCH-style replay
        # doesn't clobber operator edits.
        req = GuardrailConfigCreateRequest(
            name="probe",
            builtin=GuardrailBuiltinConfig(),
        )
        assert req.builtin.secrets_categories is None
        assert req.builtin.secrets_surfaces is None
        # Bare-default action stays "redact" — that's the field
        # default, which means a fresh request explicitly sets it.
        # Behaviour pinned because a future "Optional[str]" change
        # would silently drop the action from the payload.
        assert req.builtin.secrets_action == "redact"
        assert req.builtin.secrets_enabled is False
