# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Guardrail streaming_output_mode round-trip

Pins the two contracts the admin endpoint relies on for the streaming
output posture (auto | sync | async):

  1. Request schema accepts only the three valid modes  (write path)
  2. SQLAlchemy attribute → Response schema + to_dict()  (read path)

Regression guard: `_config_to_response` previously omitted
`streaming_output_mode`, so the GET response was pinned to the Pydantic
default ("auto") regardless of the stored value — an operator's sync/async
choice silently reverted on the next admin-form load. These tests fail if
that read-path wiring is dropped again.

The streaming posture is load-bearing for output-guardrail behaviour:
`sync` engages buffer-and-release redaction on the stream, `async` is
block-only (lower latency, no redaction), `auto` derives from whether a
redact-action output check is active.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.admin.guardrails import _config_to_response
from app.api.v1.models.admin.guardrail import GuardrailConfigCreateRequest
from app.database.models.guardrail_config import GuardrailConfig


def _bare_config(**overrides) -> GuardrailConfig:
    """A complete-enough config row for the response builder. By default
    `streaming_output_mode` is left unset (NULL) so the None→'auto'
    fallback is exercised; pass it via overrides to pin a stored value."""
    defaults = dict(
        id="gc-streaming",
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
        secrets_detection_enabled=False,
        secrets_action="redact",
        secrets_categories=None,
        secrets_surfaces=None,
        secrets_threshold=0.8,
    )
    defaults.update(overrides)
    return GuardrailConfig(**defaults)


# --- Read path: ORM attribute → Response model -----------------------------

@pytest.mark.parametrize("mode", ["auto", "sync", "async"])
def test_response_surfaces_stored_streaming_output_mode(mode):
    resp = _config_to_response(_bare_config(streaming_output_mode=mode))
    assert resp.streaming_output_mode == mode, (
        "the stored streaming posture must round-trip to the GET response; "
        "if this is 'auto' regardless of input, _config_to_response dropped "
        "the field and the operator's choice reverts on form reload"
    )


def test_response_defaults_streaming_output_mode_to_auto_when_null():
    # Pre-a088 rows (NULL column) must read back as the documented default.
    resp = _config_to_response(_bare_config())
    assert resp.streaming_output_mode == "auto"


# --- Read path: ORM → to_dict() (cluster cache / proxy load shape) ----------

@pytest.mark.parametrize("mode", ["sync", "async"])
def test_to_dict_carries_streaming_output_mode(mode):
    assert _bare_config(streaming_output_mode=mode).to_dict()["streaming_output_mode"] == mode


def test_to_dict_defaults_streaming_output_mode_to_auto():
    assert _bare_config().to_dict()["streaming_output_mode"] == "auto"


# --- Write path: request validation ----------------------------------------

@pytest.mark.parametrize("mode", ["auto", "sync", "async"])
def test_create_request_accepts_valid_streaming_output_mode(mode):
    req = GuardrailConfigCreateRequest(name="x", streaming_output_mode=mode)
    assert req.streaming_output_mode == mode


def test_create_request_rejects_invalid_streaming_output_mode():
    with pytest.raises(ValidationError):
        GuardrailConfigCreateRequest(name="x", streaming_output_mode="buffered")
