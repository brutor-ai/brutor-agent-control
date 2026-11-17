# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Guardrail per-check surfaces round-trip

The admin endpoint at `/v1/admin/guardrails` reads a request
(`GuardrailConfigCreateRequest`), writes the row, and serialises
back to `GuardrailConfigResponse` via `_config_to_response`. The
per-check surface columns (`pii_surfaces`, etc.) are the
load-bearing wire for Feature 2 — if any one of them is dropped
on the way out, the admin UI silently shows defaults even though
the DB still has the operator's edits, and a future PATCH would
overwrite them.

Heavyweight FastAPI TestClient + auth setup would test the
*transport* but the actual contract is at two boundaries:

  1. Pydantic request → SQLAlchemy attribute   (write path)
  2. SQLAlchemy attribute → Pydantic response  (read path)

This suite pins both contracts directly. Auth + tenant-isolation
are orthogonal to the surfaces wire and already covered by the
Pydantic validator suite + the `_set_if_changed` infrastructure.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.guardrails import _config_to_response
from app.api.v1.models.admin.guardrail import (
    GuardrailBuiltinConfig,
    GuardrailConfigCreateRequest,
)
from app.database.models.guardrail_config import (
    ALL_SURFACES,
    INPUT_SURFACES,
    LLM_MCP_SURFACES,
    GuardrailConfig,
)


# ---------------------------------------------------------------------------
# Round-trip helpers
# ---------------------------------------------------------------------------


def _bare_config(**overrides) -> GuardrailConfig:
    """Build a GuardrailConfig with sensible defaults for the
    response builder. Per-check surface columns default to None
    so each test can exercise the null-defaults-to-backfill
    behaviour vs explicit-value behaviour selectively."""
    defaults = dict(
        id="gc-test",
        tenant_id="t1",
        name="probe",
        is_default=True,
        enabled=True,
        priority=0,
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
        # Per-check surfaces ALL default to None → response uses
        # baked-in defaults (matches the backfill of migration a067_per_check_surfaces).
        pii_surfaces=None,
        prompt_injection_surfaces=None,
        jailbreak_surfaces=None,
        toxic_content_surfaces=None,
        banned_patterns_surfaces=None,
        banned_words_surfaces=None,
    )
    defaults.update(overrides)
    return GuardrailConfig(**defaults)


# ---------------------------------------------------------------------------
# Response builder — read path
# ---------------------------------------------------------------------------


class TestResponseNullDefaultsToBackfill:
    """When a row's `*_surfaces` column is NULL (pre-migration or
    brand-new row with no operator edit), the response builder
    must substitute the same defaults the migration backfill uses so
    the admin UI sees a consistent picture regardless of when
    the row was created."""

    def test_pii_null_falls_back_to_all_surfaces(self):
        resp = _config_to_response(_bare_config(pii_surfaces=None))
        assert resp.builtin.pii.surfaces == ALL_SURFACES

    def test_prompt_injection_null_falls_back_to_input_only(self):
        resp = _config_to_response(_bare_config(prompt_injection_surfaces=None))
        assert resp.builtin.prompt_injection.surfaces == INPUT_SURFACES

    def test_jailbreak_null_falls_back_to_input_only(self):
        resp = _config_to_response(_bare_config(jailbreak_surfaces=None))
        assert resp.builtin.jailbreak.surfaces == INPUT_SURFACES

    def test_toxic_content_null_falls_back_to_all_surfaces(self):
        resp = _config_to_response(_bare_config(toxic_content_surfaces=None))
        assert resp.builtin.toxic_content.surfaces == ALL_SURFACES

    def test_banned_words_null_falls_back_to_llm_mcp(self):
        resp = _config_to_response(_bare_config(banned_words_surfaces=None))
        assert resp.builtin.banned_words_surfaces == LLM_MCP_SURFACES

    def test_banned_patterns_null_falls_back_to_llm_mcp(self):
        resp = _config_to_response(_bare_config(banned_patterns_surfaces=None))
        assert resp.builtin.banned_patterns_surfaces == LLM_MCP_SURFACES


class TestResponseExplicitValuesPreserved:
    """When the row has explicit per-check surfaces (the common
    case post-edit), the response must return those values
    verbatim, not the defaults."""

    def test_pii_explicit_subset_preserved(self):
        cfg = _bare_config(pii_surfaces=["chat_input", "mcp_input"])
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.surfaces == ["chat_input", "mcp_input"]

    def test_prompt_injection_explicit_empty_preserved(self):
        # Empty list = operator deliberately silenced the check
        # without flipping the toplevel enable. Response MUST NOT
        # substitute defaults here — that would mask the operator
        # intent.
        cfg = _bare_config(prompt_injection_surfaces=[])
        resp = _config_to_response(cfg)
        assert resp.builtin.prompt_injection.surfaces == []

    def test_each_check_surface_is_independent(self):
        # Six checks, six distinct values. Pin that no field
        # leaks into another's slot (a copy-paste bug in the
        # response builder would manifest here).
        cfg = _bare_config(
            pii_surfaces=["chat_input"],
            prompt_injection_surfaces=["mcp_input"],
            jailbreak_surfaces=["skill_input"],
            toxic_content_surfaces=["a2a_inbound"],
            banned_words_surfaces=["chat_output"],
            banned_patterns_surfaces=["mcp_output"],
        )
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.surfaces == ["chat_input"]
        assert resp.builtin.prompt_injection.surfaces == ["mcp_input"]
        assert resp.builtin.jailbreak.surfaces == ["skill_input"]
        assert resp.builtin.toxic_content.surfaces == ["a2a_inbound"]
        assert resp.builtin.banned_words_surfaces == ["chat_output"]
        assert resp.builtin.banned_patterns_surfaces == ["mcp_output"]


# ---------------------------------------------------------------------------
# Per-surface `*_enabled` booleans — read path
#
# These are the toggles that, if dropped from `_config_to_response`'s
# `surfaces=GuardrailSurfaceResponse(...)` block (the exact "Pydantic
# strips / builder omits → silent revert" bug class), would silently
# DISABLE a guardrail surface on the next admin-form load — the
# control-plane twin of the Rust `surface_enabled` wiring bug.
# ---------------------------------------------------------------------------


class TestSurfaceEnablesRoundTrip:
    # response-builder default for each surface (the value returned when the
    # column is NULL / the field is dropped). The test sets each column to the
    # OPPOSITE so a dropped field is caught regardless of its default.
    BUILDER_DEFAULTS = {
        "chat_input": True,
        "chat_output": False,
        "embeddings_input": True,
        "image_gen_input": True,
        "image_gen_output": False,
        "audio_tts_input": True,
        "audio_tts_output": False,
        "audio_stt_input": True,
        "audio_stt_output": True,
        "video_gen_input": True,
        "video_gen_output": False,
        "batch_input": True,
        "batch_output": False,
        "moderation_input": True,
        "mcp_input": True,
        "mcp_output": True,
        "mcp_registration": True,
        "skill_input": True,
        "skill_output": True,
        "a2a_inbound": False,
        "a2a_outbound": False,
    }

    def _non_default_config(self) -> GuardrailConfig:
        overrides = {
            f"{surface}_enabled": (not default)
            for surface, default in self.BUILDER_DEFAULTS.items()
        }
        return _bare_config(**overrides)

    def test_every_surface_enable_round_trips_through_response_builder(self):
        resp = _config_to_response(self._non_default_config())
        for surface, default in self.BUILDER_DEFAULTS.items():
            assert getattr(resp.surfaces, surface) == (not default), (
                f"surfaces.{surface} did not round-trip through "
                f"_config_to_response — likely dropped from the builder, "
                f"which would silently revert to the {default} default"
            )

    def test_every_surface_enable_survives_model_dump(self):
        # model_dump() catches a field declared on the DB row + builder but
        # missing from the Pydantic response model (Pydantic strips it).
        dumped = _config_to_response(self._non_default_config()).model_dump()
        for surface, default in self.BUILDER_DEFAULTS.items():
            assert dumped["surfaces"][surface] == (not default), (
                f"surfaces.{surface} missing/wrong after model_dump — "
                f"likely not declared on GuardrailSurfaceResponse"
            )


# ---------------------------------------------------------------------------
# Request schema — write path
# ---------------------------------------------------------------------------


class TestCreateRequestCarriesSurfaces:
    """`GuardrailConfigCreateRequest.builtin.*_surfaces` reach
    the endpoint handler verbatim. This pins the path-level
    contract: a create payload with per-check surfaces deserialises
    into the same surfaces on the Python side."""

    def test_create_request_round_trips_explicit_surfaces(self):
        req = GuardrailConfigCreateRequest(
            name="probe",
            is_default=True,
            builtin=GuardrailBuiltinConfig(
                pii_surfaces=["chat_input", "mcp_input"],
                banned_words_surfaces=[],
                prompt_injection_surfaces=["a2a_inbound"],
            ),
        )
        assert req.builtin.pii_surfaces == ["chat_input", "mcp_input"]
        assert req.builtin.banned_words_surfaces == []
        assert req.builtin.prompt_injection_surfaces == ["a2a_inbound"]
        # Untouched fields stay None — PATCH semantics depend on
        # this so an admin UI saving "I only changed PII" doesn't
        # wipe the other surface lists.
        assert req.builtin.jailbreak_surfaces is None
        assert req.builtin.toxic_content_surfaces is None
        assert req.builtin.banned_patterns_surfaces is None

    def test_create_request_with_null_surfaces_is_pass_through(self):
        # Omitting *_surfaces in a request means "use the column
        # default" (which on a fresh row IS NULL, and the response
        # builder falls back to the documented defaults). The
        # request schema must NOT eagerly fill in defaults — that
        # would burn the placeholder into the DB and prevent a
        # future migration from changing the default.
        req = GuardrailConfigCreateRequest(
            name="probe",
            builtin=GuardrailBuiltinConfig(),  # nothing set
        )
        for field in (
            "pii_surfaces",
            "prompt_injection_surfaces",
            "jailbreak_surfaces",
            "toxic_content_surfaces",
            "banned_words_surfaces",
            "banned_patterns_surfaces",
        ):
            assert getattr(req.builtin, field) is None, (
                f"{field} should default to None on a bare builtin payload"
            )

    @pytest.mark.parametrize(
        "field",
        [
            "pii_surfaces",
            "prompt_injection_surfaces",
            "jailbreak_surfaces",
            "toxic_content_surfaces",
            "banned_words_surfaces",
            "banned_patterns_surfaces",
        ],
    )
    def test_create_request_rejects_unknown_surface(self, field: str):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            GuardrailBuiltinConfig(**{field: ["chat_input", "future_surface"]})
        assert "future_surface" in str(exc.value)


# ---------------------------------------------------------------------------
# Per-surface override read/write contract
# ---------------------------------------------------------------------------


class TestPerSurfaceResponseContract:
    """Bug: editor saved per-surface overrides successfully but reopen
    showed nothing. Root cause: Pydantic response models silently
    stripped the `per_surface` field because it wasn't declared on
    `GuardrailPiiResponse` / siblings. CLAUDE.md gotcha. These pin
    the read path so the column → Pydantic → JSON → admin UI chain
    cannot regress."""

    def test_pii_per_surface_round_trips_through_response(self):
        cfg = _bare_config(pii_per_surface={
            "chat_output": {"action": "block", "provider": "presidio"},
        })
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.per_surface == {
            "chat_output": {"action": "block", "provider": "presidio"},
        }

    def test_null_per_surface_serialises_as_null(self):
        # Column NULL must produce null on the wire (not dropped, not
        # empty dict). The admin UI preloads from null = "no
        # overrides on any surface."
        cfg = _bare_config(pii_per_surface=None)
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.per_surface is None

    def test_every_check_carries_its_per_surface_field(self):
        # All seven per-surface columns are forwarded — a future
        # editor on any check stays preloadable. Catches the
        # "I added the field to one check and forgot the others"
        # regression.
        cfg = _bare_config(
            pii_per_surface={"chat_input": {"action": "block"}},
            prompt_injection_per_surface={"mcp_input": {"action": "warn"}},
            jailbreak_per_surface={"chat_input": {"action": "block"}},
            toxic_content_per_surface={"chat_output": {"threshold": 0.4}},
            secrets_per_surface={"mcp_output": {"action": "redact"}},
            banned_words_per_surface={"mcp_input": {"words": ["DROP TABLE"]}},
            banned_patterns_per_surface={"chat_output": {"patterns": ["foo"]}},
        )
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.per_surface == {"chat_input": {"action": "block"}}
        assert resp.builtin.prompt_injection.per_surface == {"mcp_input": {"action": "warn"}}
        assert resp.builtin.jailbreak.per_surface == {"chat_input": {"action": "block"}}
        assert resp.builtin.toxic_content.per_surface == {"chat_output": {"threshold": 0.4}}
        assert resp.builtin.secrets.per_surface == {"mcp_output": {"action": "redact"}}
        assert resp.builtin.banned_words_per_surface == {"mcp_input": {"words": ["DROP TABLE"]}}
        assert resp.builtin.banned_patterns_per_surface == {"chat_output": {"patterns": ["foo"]}}

    def test_per_surface_survives_model_dump(self):
        # Pydantic strips silently if the field isn't declared. Round
        # trip through model_dump() to confirm the field actually
        # exists on the schema, not just on the instance.
        cfg = _bare_config(pii_per_surface={"chat_input": {"action": "block"}})
        resp = _config_to_response(cfg)
        dumped = resp.model_dump()
        assert dumped["builtin"]["pii"]["per_surface"] == {
            "chat_input": {"action": "block"},
        }


class TestPiiThresholdContract:
    """Presidio score_threshold round-trip.

    The column is Nullable; the orchestrator forwards Some(threshold)
    to Presidio's /analyze.score_threshold when set, and falls back
    to the detector's own default when null. These tests pin the
    Pydantic response side so the admin UI preloads correctly."""

    def test_pii_threshold_round_trips_through_response(self):
        cfg = _bare_config(pii_threshold=0.42)
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.threshold == 0.42

    def test_null_threshold_serialises_as_null(self):
        # NULL column means "use Presidio's built-in default" — must
        # not be coerced to 0.0 or omitted (the admin UI tells these
        # cases apart).
        cfg = _bare_config(pii_threshold=None)
        resp = _config_to_response(cfg)
        assert resp.builtin.pii.threshold is None

    def test_threshold_survives_model_dump(self):
        # CLAUDE.md gotcha: response models silently strip undeclared
        # fields. Pin the schema by dumping and inspecting.
        cfg = _bare_config(pii_threshold=0.7)
        resp = _config_to_response(cfg)
        dumped = resp.model_dump()
        assert dumped["builtin"]["pii"]["threshold"] == 0.7


class TestPerSurfacePatchHelper:
    """The PATCH endpoint uses `_set_per_surface_if_present` to
    distinguish 'absent in body' (leave column alone) from
    'explicit null' (clear column). This pins those branches so
    an older admin UI that omits per_surface won't silently wipe
    an existing operator override."""

    def _builtin_request(self, **kwargs):
        return GuardrailBuiltinConfig(**kwargs)

    def test_absent_per_surface_leaves_column_untouched(self):
        from app.api.v1.endpoints.admin.guardrails import _set_per_surface_if_present
        cfg = _bare_config(pii_per_surface={"chat_input": {"action": "block"}})
        # Builtin payload with NO per_surface fields set — legacy
        # client style.
        b = self._builtin_request(pii_action="redact")
        _set_per_surface_if_present(b, cfg, {})
        assert cfg.pii_per_surface == {"chat_input": {"action": "block"}}

    def test_explicit_null_clears_column(self):
        from app.api.v1.endpoints.admin.guardrails import _set_per_surface_if_present
        cfg = _bare_config(pii_per_surface={"chat_input": {"action": "block"}})
        b = self._builtin_request(pii_per_surface=None)  # explicit null
        changes = {}
        _set_per_surface_if_present(b, cfg, changes)
        assert cfg.pii_per_surface is None
        assert "pii_per_surface" in changes  # change was recorded

    def test_dict_value_writes_through(self):
        from app.api.v1.endpoints.admin.guardrails import _set_per_surface_if_present
        cfg = _bare_config(pii_per_surface=None)
        new_map = {"mcp_output": {"action": "redact", "provider": "built_in"}}
        b = self._builtin_request(pii_per_surface=new_map)
        changes = {}
        _set_per_surface_if_present(b, cfg, changes)
        assert cfg.pii_per_surface == new_map
