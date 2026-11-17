# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A guardrail policy names only what the gateway knows.

In the gateway a provider selector that selects nothing, a PII or secrets
category the built-in detector does not know, and a `providers[]` entry of a
provider it does not know are each a check that cannot be evaluated
(`src/services/guardrails/orchestrator.rs`). The control plane refuses each
at save; the lists it refuses by are the gateway's, read here from its
source.
"""

from __future__ import annotations

import os
import re

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.models.admin import guardrail as g
from tests.core_repo import core_revision, core_source


def _read(*parts):
    return core_source("services", "guardrails", *parts).read_text(encoding="utf-8")


def _known_categories(source: str) -> list:
    body = source[source.index("pub fn known_categories() -> &'static [&'static str] {"):]
    body = body[body.index("&["):]
    return re.findall(r'"([a-z0-9_]+)"', body[: body.index("]")])


def test_the_categories_are_the_gateways():
    read = f" [the gateway's source read: {core_revision()}]"
    assert sorted(_known_categories(_read("pii.rs"))) == sorted(g.ALLOWED_PII_CATEGORIES), (
        "PiiDetector::known_categories changed in the gateway" + read
    )
    assert sorted(_known_categories(_read("secrets.rs"))) == sorted(g.ALLOWED_SECRETS_CATEGORIES), (
        "SecretsDetector::known_categories changed in the gateway" + read
    )


def test_the_provider_selectors_are_the_gateways():
    read = f" [the gateway's source read: {core_revision()}]"
    source = re.sub(r"\s+", " ", _read("orchestrator.rs"))
    for piece in (
        '"pii" => ("presidio", Selected::BuiltIn),',
        '"prompt_injection" | "jailbreak" => ("lakera_guard", Selected::BuiltIn),',
        '"toxic_content" => ("openai_moderation", Selected::Remote),',
        'Some("built_in") => Ok(Selected::BuiltIn), Some(s) if s == remote => Ok(Selected::Remote), Some(_) => Err(',
    ):
        assert piece in source, f"known_selector changed in the gateway ({piece!r})" + read
    assert set(g.ALLOWED_PII_PROVIDERS) == {"built_in", "presidio"}
    assert set(g.ALLOWED_PROMPT_INJECTION_PROVIDERS) == {"built_in", "lakera_guard"}
    assert set(g.ALLOWED_JAILBREAK_PROVIDERS) == {"built_in", "lakera_guard"}
    assert set(g.ALLOWED_TOXIC_CONTENT_PROVIDERS) == {"built_in", "openai_moderation"}


def test_the_provider_entries_are_the_gateways():
    read = f" [the gateway's source read: {core_revision()}]"
    source = re.sub(r"\s+", " ", _read("orchestrator.rs"))
    # The entries wired by a selector, and the standalone ones.
    assert 'Some("lakera" | "lakera_guard" | "openai_moderation" | "presidio")' in source, read
    arms = source[source.index("let built: Result<Box<dyn Detector>, Unbuilt> = match provider_id {"):]
    arms = arms[: arms.index('"Unknown guardrail provider entry"')]
    assert re.findall(r'"([a-z_]+)" => [A-Z]', arms) == ["custom", "bedrock"], read
    known = {"lakera", "lakera_guard", "openai_moderation", "presidio", "custom", "bedrock"}
    assert set(g.ALLOWED_PROVIDERS) <= known and {"custom", "bedrock"} <= set(g.ALLOWED_PROVIDERS)


@pytest.mark.parametrize(
    "fields",
    [
        {"pii_provider": "presidio_v2"},
        {"prompt_injection_provider": "lakera"},
        {"jailbreak_provider": "openai_moderation"},
        {"toxic_content_provider": "perspective"},
        {"pii_categories": ["email", "e-mail"]},
        {"secrets_categories": ["github_tokens"]},
        # The per-surface override of a selector is a selector.
        {"pii_per_surface": {"chat_input": {"provider": "presidio_v2"}}},
        {"prompt_injection_per_surface": {"chat_input": {"provider": "lakera"}}},
        {"jailbreak_per_surface": {"mcp_input": {"provider": "presidio"}}},
        {"toxic_content_per_surface": {"chat_output": {"provider": "lakera_guard"}}},
        {"pii_per_surface": {"chat_input": {"provider": 7}}},
    ],
)
def test_what_the_gateway_cannot_evaluate_is_refused(fields):
    with pytest.raises(ValidationError):
        g.GuardrailBuiltinConfig(**fields)


def test_what_it_knows_is_saved():
    saved = g.GuardrailBuiltinConfig(
        pii_provider="presidio",
        toxic_content_provider="built_in",
        pii_categories=list(g.ALLOWED_PII_CATEGORIES),
        secrets_categories=list(g.ALLOWED_SECRETS_CATEGORIES),
        pii_per_surface={"chat_input": {"provider": "presidio"}, "chat_output": {"provider": "built_in"}},
        toxic_content_per_surface={"chat_output": {"provider": "openai_moderation", "threshold": 0.5}},
    )
    assert saved.pii_per_surface["chat_input"]["provider"] == "presidio"


@pytest.mark.parametrize("provider", ["bedrok", "custom_webhook", "lakera_guard", ""])
def test_an_entry_of_a_provider_the_control_plane_does_not_offer_is_refused(provider):
    with pytest.raises(ValidationError):
        g.GuardrailProviderConfig(provider=provider)
