# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the A2A v1.0 Python models + validator + v0.3 translator
(see `app/services/a2a_v1/`).

Coverage mirrors the Rust `services::a2a_v1::tests` in structure:
same shapes, same strict-on-the-wire assertions, same regressions.
Keeps the two sides bit-compatible.
"""

from __future__ import annotations

import base64
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.services.a2a_v1 import (
    AgentCard,
    AgentCapabilities,
    AgentInterface,
    AgentSkill,
    Artifact,
    InvalidV1CardError,
    Message,
    Part,
    PartContent,
    Role,
    Task,
    TaskState,
    TaskStatus,
    BRUTOR_DELEGATION_EXTENSION_URI,
    is_v03_card,
    is_v1_card,
    translate_v03_to_v1,
    validate_card_v1,
)


# =============================================================================
# Enums
# =============================================================================


class TestEnums:
    def test_role_screaming_snake_case_on_wire(self):
        # Pydantic `use_enum_values=True` means the wire form is the
        # string value, not the variant name.
        m = Message(
            messageId="m",
            role=Role.ROLE_USER,
            parts=[Part(text="hi")],
        )
        dumped = m.model_dump(by_alias=True)
        assert dumped["role"] == "ROLE_USER"

    def test_role_rejects_lowercase_v03_style(self):
        with pytest.raises(Exception):
            Message.model_validate(
                {
                    "messageId": "m",
                    "role": "user",
                    "parts": [{"text": "hi"}],
                }
            )

    def test_task_state_terminal_classification(self):
        for s in (
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_REJECTED,
        ):
            assert s.is_terminal()
            assert not s.is_interrupted()
        for s in (
            TaskState.TASK_STATE_INPUT_REQUIRED,
            TaskState.TASK_STATE_AUTH_REQUIRED,
        ):
            assert s.is_interrupted()
            assert not s.is_terminal()
        for s in (
            TaskState.TASK_STATE_UNSPECIFIED,
            TaskState.TASK_STATE_SUBMITTED,
            TaskState.TASK_STATE_WORKING,
        ):
            assert not s.is_terminal()
            assert not s.is_interrupted()


# =============================================================================
# Part — member-presence oneof
# =============================================================================


class TestPart:
    def test_text_round_trip(self):
        p = Part(text="hello", mediaType="text/plain")
        dumped = p.model_dump(by_alias=True, exclude_none=True)
        assert dumped == {"text": "hello", "mediaType": "text/plain"}
        back = Part.model_validate(dumped)
        assert back == p
        assert back.content == PartContent.TEXT

    def test_raw_bytes_round_trip_through_json_wire(self):
        # Python constructor accepts raw bytes; JSON serialization
        # encodes them as base64; re-parsing the JSON reads them back
        # to the identical raw bytes. This is the round trip that
        # actually matters for wire compatibility.
        raw = b"brutor-a2a-test\x00\xff"
        p = Part(raw=raw, mediaType="application/octet-stream")
        wire_json = p.model_dump_json(by_alias=True, exclude_none=True)
        # Sanity — the JSON contains a base64 string, not raw bytes.
        assert '"raw":"' in wire_json
        decoded = Part.model_validate_json(wire_json)
        assert decoded.raw == raw
        assert decoded.content == PartContent.RAW

    def test_raw_accepts_base64_string_input(self):
        raw = b"hello"
        as_b64 = base64.b64encode(raw).decode()
        p = Part.model_validate({"raw": as_b64})
        assert p.raw == raw

    def test_raw_rejects_non_base64_string(self):
        # Strings that aren't valid base64 must fail loudly, not
        # silently corrupt to UTF-8 bytes.
        with pytest.raises(Exception) as exc_info:
            Part.model_validate({"raw": "not base64 !!!"})
        assert "base64" in str(exc_info.value).lower()

    def test_url_round_trip(self):
        p = Part(url="https://x.example/doc.pdf", filename="doc.pdf", mediaType="application/pdf")
        dumped = p.model_dump(by_alias=True, exclude_none=True)
        assert dumped == {
            "url": "https://x.example/doc.pdf",
            "filename": "doc.pdf",
            "mediaType": "application/pdf",
        }
        assert Part.model_validate(dumped) == p

    def test_data_round_trip(self):
        p = Part(data={"key": "value", "n": 42}, mediaType="application/json")
        dumped = p.model_dump(by_alias=True, exclude_none=True)
        assert dumped == {
            "data": {"key": "value", "n": 42},
            "mediaType": "application/json",
        }
        assert Part.model_validate(dumped) == p

    def test_rejects_zero_oneof_variants(self):
        with pytest.raises(Exception) as exc_info:
            Part.model_validate({"mediaType": "text/plain"})
        assert "exactly one" in str(exc_info.value).lower()

    def test_rejects_multiple_oneof_variants(self):
        with pytest.raises(Exception) as exc_info:
            Part.model_validate({"text": "hi", "url": "https://x/"})
        assert "exactly one" in str(exc_info.value).lower()

    def test_rejects_unknown_sibling_fields(self):
        # extra='forbid' parity with Rust deny_unknown_fields.
        with pytest.raises(Exception) as exc_info:
            Part.model_validate({"text": "hi", "kind": "text"})
        # Pydantic reports "extra_forbidden" with the field name.
        assert "kind" in str(exc_info.value)

    def test_rejects_v03_kind_discriminator(self):
        with pytest.raises(Exception) as exc_info:
            Part.model_validate({"kind": "text", "text": "hi"})
        assert "kind" in str(exc_info.value)


# =============================================================================
# Message
# =============================================================================


class TestMessage:
    def test_minimum_required_fields_round_trip(self):
        m = Message(
            messageId="msg-1",
            role=Role.ROLE_USER,
            parts=[Part(text="hi")],
        )
        dumped = m.model_dump(by_alias=True, exclude_none=True)
        assert dumped["messageId"] == "msg-1"
        assert dumped["role"] == "ROLE_USER"
        assert dumped["parts"] == [{"text": "hi"}]
        back = Message.model_validate(dumped)
        assert back == m

    def test_rejects_missing_message_id(self):
        with pytest.raises(Exception):
            Message.model_validate({"role": "ROLE_USER", "parts": [{"text": "hi"}]})

    def test_rejects_missing_role(self):
        with pytest.raises(Exception):
            Message.model_validate({"messageId": "m", "parts": [{"text": "hi"}]})

    def test_rejects_empty_parts(self):
        with pytest.raises(Exception) as exc_info:
            Message.model_validate(
                {"messageId": "m", "role": "ROLE_USER", "parts": []}
            )
        assert "at least one" in str(exc_info.value).lower()

    def test_preserves_extensions_and_refs(self):
        m = Message(
            messageId="m",
            contextId="ctx-1",
            taskId="task-1",
            role=Role.ROLE_AGENT,
            parts=[Part(text="reply")],
            extensions=[BRUTOR_DELEGATION_EXTENSION_URI],
            referenceTaskIds=["other-task"],
        )
        dumped = m.model_dump(by_alias=True, exclude_none=True)
        assert dumped["extensions"] == [BRUTOR_DELEGATION_EXTENSION_URI]
        assert dumped["referenceTaskIds"] == ["other-task"]
        assert Message.model_validate(dumped) == m


# =============================================================================
# Artifact + Task
# =============================================================================


class TestArtifactTask:
    def test_artifact_empty_parts_rejected(self):
        with pytest.raises(Exception):
            Artifact.model_validate({"artifactId": "a", "parts": []})

    def test_task_round_trip(self):
        t = Task(
            id="task-1",
            contextId="ctx",
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            artifacts=[
                Artifact(
                    artifactId="a",
                    parts=[Part(data={"ok": True}, mediaType="application/json")],
                )
            ],
            history=[
                Message(
                    messageId="m",
                    role=Role.ROLE_USER,
                    parts=[Part(text="run")],
                )
            ],
        )
        dumped = t.model_dump(by_alias=True, exclude_none=True)
        assert Task.model_validate(dumped) == t


# =============================================================================
# Agent Card
# =============================================================================


def _minimum_v1_card_dict() -> dict:
    return {
        "name": "billing-agent",
        "description": "Issues refunds",
        "supportedInterfaces": [
            {
                "url": "https://api.example.com/a2a/v1",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "billing.refund",
                "name": "Refund",
                "description": "Issue a refund",
                "tags": ["billing"],
            }
        ],
    }


class TestAgentCard:
    def test_minimum_viable_card_round_trip(self):
        card = AgentCard.model_validate(_minimum_v1_card_dict())
        dumped = card.model_dump(by_alias=True, exclude_none=True)
        # No v0.3 top-level leak.
        assert "url" not in dumped
        assert "protocolVersion" not in dumped
        assert "preferredTransport" not in dumped
        assert dumped["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert AgentCard.model_validate(dumped) == card

    def test_rejects_v03_top_level_url(self):
        bad = _minimum_v1_card_dict()
        bad["url"] = "https://x/"
        with pytest.raises(Exception):
            AgentCard.model_validate(bad)

    def test_rejects_empty_supported_interfaces(self):
        bad = _minimum_v1_card_dict()
        bad["supportedInterfaces"] = []
        with pytest.raises(Exception) as exc_info:
            AgentCard.model_validate(bad)
        assert "at least one" in str(exc_info.value).lower()

    def test_security_schemes_on_wire_as_map(self):
        card_dict = _minimum_v1_card_dict()
        card_dict["securitySchemes"] = {
            "api_key": {
                "apiKeySecurityScheme": {
                    "description": "Tenant API key",
                    "location": "header",
                    "name": "X-API-Key",
                }
            }
        }
        card = AgentCard.model_validate(card_dict)
        assert "api_key" in card.security_schemes
        dumped = card.model_dump(by_alias=True, exclude_none=True)
        assert dumped["securitySchemes"]["api_key"]["apiKeySecurityScheme"]["location"] == "header"


# =============================================================================
# Validator
# =============================================================================


class TestValidator:
    def test_accepts_minimum_viable_card(self):
        card = validate_card_v1(_minimum_v1_card_dict())
        assert card.name == "billing-agent"

    def test_rejects_non_dict_input(self):
        with pytest.raises(InvalidV1CardError) as exc_info:
            validate_card_v1("not a dict")  # type: ignore[arg-type]
        assert "JSON object" in str(exc_info.value)

    def test_produces_actionable_summary_on_bad_field(self):
        bad = _minimum_v1_card_dict()
        bad["supportedInterfaces"][0]["protocolBinding"] = 123  # wrong type
        with pytest.raises(InvalidV1CardError) as exc_info:
            validate_card_v1(bad)
        msg = str(exc_info.value)
        # Location should be present, and it should name the field.
        assert "supportedInterfaces" in msg
        assert "protocolBinding" in msg

    def test_wrapped_error_preserves_pydantic_cause(self):
        bad = _minimum_v1_card_dict()
        del bad["name"]
        with pytest.raises(InvalidV1CardError) as exc_info:
            validate_card_v1(bad)
        # __cause__ should be the underlying ValidationError.
        assert exc_info.value.__cause__ is not None


# =============================================================================
# Translator
# =============================================================================


def _minimum_v03_card_dict() -> dict:
    """Realistic v0.3 card shape."""
    return {
        "name": "legacy-agent",
        "description": "An old card",
        "version": "1.0.0",
        "url": "https://legacy.example.com/a2a",
        "protocolVersion": "0.3",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [],
    }


class TestTranslator:
    def test_detects_v1_card(self):
        assert is_v1_card(_minimum_v1_card_dict()) is True
        assert is_v03_card(_minimum_v1_card_dict()) is False

    def test_detects_v03_card(self):
        assert is_v03_card(_minimum_v03_card_dict()) is True
        assert is_v1_card(_minimum_v03_card_dict()) is False

    def test_detects_non_dict(self):
        assert is_v1_card("str") is False  # type: ignore[arg-type]
        assert is_v03_card(None) is False  # type: ignore[arg-type]

    def test_translates_primary_url_to_supported_interfaces(self):
        out = translate_v03_to_v1(_minimum_v03_card_dict())
        assert "url" not in out
        assert "protocolVersion" not in out
        assert "preferredTransport" not in out
        interfaces = out["supportedInterfaces"]
        assert len(interfaces) == 1
        assert interfaces[0] == {
            "url": "https://legacy.example.com/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "0.3",
        }

    def test_translates_additional_interfaces(self):
        card = _minimum_v03_card_dict()
        card["additionalInterfaces"] = [
            {"url": "https://grpc.legacy/", "transport": "GRPC"},
            {"url": "https://rest.legacy/", "transport": "HTTP+JSON"},
        ]
        out = translate_v03_to_v1(card)
        assert len(out["supportedInterfaces"]) == 3
        assert out["supportedInterfaces"][1]["protocolBinding"] == "GRPC"
        assert out["supportedInterfaces"][2]["protocolBinding"] == "HTTP+JSON"

    def test_translates_supports_authenticated_extended_card_flag(self):
        card = _minimum_v03_card_dict()
        card["supportsAuthenticatedExtendedCard"] = True
        out = translate_v03_to_v1(card)
        assert "supportsAuthenticatedExtendedCard" not in out
        assert out["capabilities"]["extendedAgentCard"] is True

    def test_drops_legacy_signature_fields(self):
        card = _minimum_v03_card_dict()
        card["signature"] = "old-sig"
        card["signatureAlgorithm"] = "ed25519"
        card["signedBy"] = "admin-1"
        card["signedAt"] = "2025-01-01T00:00:00Z"
        out = translate_v03_to_v1(card)
        for legacy in ("signature", "signatureAlgorithm", "signedBy", "signedAt"):
            assert legacy not in out
        # Signatures array is empty (needs re-signing under v1.0 JWS rules).
        assert out["signatures"] == []

    def test_translator_output_passes_v1_validator(self):
        out = translate_v03_to_v1(_minimum_v03_card_dict())
        # Must validate cleanly under the strict v1.0 validator.
        card = validate_card_v1(out)
        assert card.name == "legacy-agent"

    def test_v1_input_passes_through_unchanged(self):
        v1 = _minimum_v1_card_dict()
        out = translate_v03_to_v1(v1)
        # Same shape (deep copy — not the same object).
        assert out == v1
        assert out is not v1

    def test_translation_is_idempotent(self):
        # Feeding translator output back through it must produce the
        # same output (once v1.0-shaped, always v1.0-shaped).
        once = translate_v03_to_v1(_minimum_v03_card_dict())
        twice = translate_v03_to_v1(once)
        assert once == twice

    def test_raises_on_non_dict(self):
        with pytest.raises(ValueError):
            translate_v03_to_v1("str")  # type: ignore[arg-type]

    def test_raises_when_no_url_or_additional_interfaces(self):
        # A card with no url and no additional interfaces can't be
        # usefully translated — we refuse to fabricate one.
        card = _minimum_v03_card_dict()
        del card["url"]
        with pytest.raises(ValueError) as exc_info:
            translate_v03_to_v1(card)
        assert "supportedInterfaces" in str(exc_info.value)

    def test_defaults_required_v1_fields_if_missing(self):
        # v0.3 let defaultInputModes/defaultOutputModes/skills be
        # absent. Translator supplies empty arrays so the v1.0
        # validator doesn't reject.
        card = _minimum_v03_card_dict()
        del card["defaultInputModes"]
        del card["defaultOutputModes"]
        del card["skills"]
        out = translate_v03_to_v1(card)
        assert out["defaultInputModes"] == []
        assert out["defaultOutputModes"] == []
        assert out["skills"] == []
        # And the result still validates.
        assert validate_card_v1(out).default_input_modes == []
