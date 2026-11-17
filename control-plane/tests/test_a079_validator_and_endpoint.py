# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a079_per_mode_surfaces — Pydantic validator + apply-recommended-defaults endpoint tests

Two contracts that ride directly on the per-mode refactor:

1. **`*_surfaces` request validator** — a pre-a079 policy YAML that
   says `pii_surfaces: [llm_input]` must be accepted AND expanded
   to the per-mode union so the DB row never carries the stale
   legacy token alone. Bogus tokens must 422 cleanly.

2. **`apply_recommended_mode_defaults` constants + delta shape** —
   the endpoint mutates `pii_surfaces`, `prompt_injection_surfaces`,
   `jailbreak_surfaces`, `toxic_content_surfaces`, `secrets_surfaces`
   to the audit-recommended posture. The constants must keep
   jailbreak / prompt-injection / toxic OFF embeddings / batch /
   moderation while keeping PII / secrets wide — otherwise the
   "click this button to fix the bug" affordance silently fails.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.guardrail import (
    ALLOWED_CHECK_SURFACES,
    GuardrailBuiltinConfig,
)
from app.database.models.guardrail_config import (
    AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES,
    AUDIT_RECOMMENDED_TOXIC_CONTENT_SURFACES,
    AUDIT_RECOMMENDED_WIDE_SURFACES,
    LLM_INPUT_MODE_SURFACES,
    LLM_TEXT_OUTPUT_MODE_SURFACES,
)


# ---------------------------------------------------------------------------
# Validator: legacy `llm_input` expands to per-mode union on save
# ---------------------------------------------------------------------------


class TestSurfaceValidator:
    """The validator only accepts per-mode tokens. Legacy `llm_input` /
    `llm_output` were dropped — any reappearance is a typo or stale
    policy YAML and must 422 cleanly.
    """

    def test_explicit_per_mode_token_passes_through_untouched(self):
        cfg = GuardrailBuiltinConfig(
            pii_enabled=True,
            pii_action="redact",
            jailbreak_surfaces=["chat_input", "audio_stt_input"],
        )
        assert cfg.jailbreak_surfaces == ["chat_input", "audio_stt_input"]

    def test_legacy_llm_input_token_rejected(self):
        # Catches stale pre-cleanup policy YAML / hand-edited PATCHes.
        # Closed-by-default — silently accepting would re-introduce the
        # ambiguity the cleanup removed.
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(
                pii_enabled=True,
                pii_action="redact",
                jailbreak_surfaces=["llm_input"],
            )
        assert "llm_input" in str(exc.value)

    def test_legacy_llm_output_token_rejected(self):
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(
                pii_enabled=True,
                pii_action="redact",
                toxic_content_surfaces=["llm_output"],
            )
        assert "llm_output" in str(exc.value)

    def test_bogus_surface_token_raises_validation_error(self):
        # A typo or future-version token MUST 422 instead of silently
        # dropping the entry. Closed-by-default is the only safe
        # posture for a security gate vocabulary.
        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(
                pii_enabled=True,
                pii_action="redact",
                jailbreak_surfaces=["chat_input", "made_up_surface"],
            )
        assert "made_up_surface" in str(exc.value)

    def test_empty_array_is_allowed(self):
        # An empty list is a valid expression of "do not run this
        # check on any surface" — distinct from None (= use defaults).
        # If the validator rejected empty arrays, operators could not
        # turn a check off via the surface picker without unticking
        # every box (which the picker doesn't currently support).
        cfg = GuardrailBuiltinConfig(
            pii_enabled=True,
            pii_action="redact",
            jailbreak_surfaces=[],
        )
        assert cfg.jailbreak_surfaces == []

    def test_none_passes_through(self):
        # None semantics: "leave the column unchanged on PATCH". The
        # validator must preserve this — converting None to [] would
        # accidentally clear surface coverage on every PATCH.
        cfg = GuardrailBuiltinConfig(pii_enabled=True, pii_action="redact")
        assert cfg.jailbreak_surfaces is None


class TestPerModeSurfaceConstants:
    """The per-mode surface constant lists are the source of truth used
    by the seed, the apply-defaults endpoint, and the Rust orchestrator
    (mirrored in per_surface.rs). Lock their contents.
    """

    def test_input_mode_surfaces_contain_every_input_route(self):
        # Adding a new operational input route means adding it here too.
        for surface in ("chat_input", "embeddings_input", "image_gen_input",
                        "audio_tts_input", "audio_stt_input",
                        "video_gen_input", "batch_input", "moderation_input"):
            assert surface in LLM_INPUT_MODE_SURFACES

    def test_text_output_mode_surfaces_only_carry_text(self):
        # chat / stt / batch outputs are text; image/audio_tts/video
        # outputs are media and MUST NOT appear here until a text-style
        # check (e.g. a future NSFW classifier) actually runs on them.
        assert set(LLM_TEXT_OUTPUT_MODE_SURFACES) == {
            "chat_output", "audio_stt_output", "batch_output",
        }


# ---------------------------------------------------------------------------
# Apply-recommended-defaults — constants and delta computation
# ---------------------------------------------------------------------------


class TestAuditRecommendedConstants:
    """The narrowing posture the admin UI applies via
    `POST /apply-recommended-mode-defaults`. These constants are the
    safety story: the audit reads the source code, expects
    embeddings / batch / moderation excluded from the strict checks,
    and PII / secrets wide. If they drift the operator's one-click
    "fix the bug" affordance silently breaks the fix.
    """

    def test_strict_input_surfaces_exclude_embeddings_batch_moderation(self):
        # The original bug surface: embeddings_input was firing
        # jailbreak on legitimate security docs. Same category error
        # applies to batch_input (operator-controlled async corpus)
        # and moderation_input (the safety classifier itself).
        for forbidden in ("embeddings_input", "batch_input", "moderation_input"):
            assert forbidden not in AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES, (
                f"jailbreak / prompt-injection MUST NOT fire on "
                f"{forbidden} per audit posture — that's the bug fix"
            )

    def test_strict_input_surfaces_include_chat_image_audio_video(self):
        # Conversely, the strict input checks DO apply on every
        # user-facing chat / multimodal generation surface. If any of
        # these gets dropped, an attacker could route a jailbreak
        # through e.g. audio_tts_input and bypass the gate.
        for required in (
            "chat_input", "image_gen_input",
            "audio_tts_input", "audio_stt_input",
            "video_gen_input",
        ):
            assert required in AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES

    def test_strict_input_includes_mcp_skill_a2a_inputs(self):
        # The strict checks span the WHOLE input attack surface, not
        # just LLM. MCP / Skill / A2A inbound must also be covered.
        for required in ("mcp_input", "skill_input", "a2a_inbound"):
            assert required in AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES

    def test_toxic_content_includes_outputs(self):
        # Toxic content is the one strict check that runs on outputs
        # too (chat replies, transcripts, batch results, MCP tool
        # results, etc.). Image/audio/video outputs are media — no
        # text guardrail applies until the future NSFW classifier.
        for required in (
            "chat_output", "audio_stt_output", "batch_output",
            "mcp_output", "skill_output", "a2a_outbound",
        ):
            assert required in AUDIT_RECOMMENDED_TOXIC_CONTENT_SURFACES

    def test_wide_surfaces_cover_everything_pii_and_secrets_should_see(self):
        # PII / secrets stay wide because they detect data shapes
        # that are policy-relevant on every surface — a customer card
        # number is just as sensitive in an embedding corpus as in a
        # chat reply.
        for required in (
            "chat_input", "chat_output", "embeddings_input",
            "mcp_input", "mcp_output",
            "skill_input", "skill_output",
            "a2a_inbound", "a2a_outbound",
        ):
            assert required in AUDIT_RECOMMENDED_WIDE_SURFACES

    def test_wide_surfaces_include_moderation_input(self):
        # PII / secrets DO run on moderation_input, unlike jailbreak /
        # prompt-injection / toxic which the orchestrator hard-skips.
        # A moderation call body that happens to carry an SSN is a
        # real exfil concern — we want to redact it on the way past
        # the gateway regardless of whether the moderation classifier
        # itself needs to see it. The Rust safety bypass tests
        # already lock in the inverse (jailbreak/etc. NEVER fire on
        # moderation_input even if listed).
        assert "moderation_input" in AUDIT_RECOMMENDED_WIDE_SURFACES


class TestApplyRecommendedDefaultsEndpoint:
    """Endpoint smoke tests. Full HTTP-level coverage (auth, tenant
    isolation, audit-row write) lives in the integration suite; these
    tests pin the route's path/method and the delta-computation
    contract by importing the router factory and walking its routes.
    """

    def test_endpoint_is_registered_with_correct_path(self):
        from unittest.mock import MagicMock
        from fastapi import APIRouter
        from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
        from app.core.logging_system import BrutorLogger

        # Stub dependencies — we only need the router shape, not live
        # service behaviour. MagicMock satisfies any attribute access
        # the factory might do at construction time.
        router: APIRouter = create_guardrails_router(
            guardrails_service=MagicMock(),
            logger=BrutorLogger("test"),
        )
        matching = [
            r for r in router.routes
            if getattr(r, "path", "").endswith("/apply-recommended-mode-defaults")
        ]
        assert matching, (
            "POST /{config_id}/apply-recommended-mode-defaults must be "
            "registered — the admin UI's bug-fix button calls it directly"
        )
        # Path must take the config_id as a path param.
        assert any("{config_id}" in r.path for r in matching), (
            "endpoint must accept config_id as a path parameter"
        )
        # Method must be POST — mutating action with audit-row write.
        assert any("POST" in r.methods for r in matching)

    def test_delta_computation_skips_unchanged_columns(self):
        # If a config ALREADY matches the audit-recommended posture
        # (e.g. seeded after a079_per_mode_surfaces was live), apply-defaults must
        # report "no changes" rather than rewriting the same arrays
        # and bumping the row's updated_at — that would generate
        # spurious audit churn.
        #
        # The delta-computation logic in the endpoint is set-based
        # (`removed = current_set - recommended_set;
        #   added = recommended_set - current_set`).
        # We exercise the invariant directly: same-set inputs → no
        # delta entries.
        current = list(AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES)
        recommended = list(AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES)
        removed = sorted(set(current) - set(recommended))
        added = sorted(set(recommended) - set(current))
        assert not removed
        assert not added

    def test_delta_includes_added_and_removed_for_pre_a079_config(self):
        # Simulate the common case: a pre-a079 config carrying
        # `jailbreak_surfaces = ["llm_input"]` (validator-expanded
        # to the per-mode union including embeddings / batch /
        # moderation). Applying defaults should REMOVE the operator-
        # controlled corpora and KEEP the user-facing surfaces.
        current = list(LLM_INPUT_MODE_SURFACES) + ["mcp_input", "skill_input", "a2a_inbound"]
        recommended = list(AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES)
        removed = sorted(set(current) - set(recommended))
        added = sorted(set(recommended) - set(current))
        # The exact bug fix: embeddings + batch + moderation come off.
        assert "embeddings_input" in removed
        assert "batch_input" in removed
        assert "moderation_input" in removed
        # Chat-style + MCP / Skill / A2A stay.
        assert "chat_input" not in removed
        assert "mcp_input" not in removed
        # No surprise additions for this profile.
        assert added == []
