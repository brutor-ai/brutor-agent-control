# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A guardrail check runs on the surfaces the gateway knows, and on no other.

Each check of a guardrail configuration has a list of surfaces
(`pii_surfaces`, `secrets_surfaces`, ...). The gateway asks that list for
the name of the surface a call is on (`surface_token` in
`src/services/guardrails/mod.rs`): a name it never asks for is an entry
that does nothing, and a list of only such names is a check that is on and
never runs. A list that is SQL NULL runs on the gateway's default set for
the check; a list stored as anything else that is not an array (a JSON
`null` among them) runs nowhere.

So the control plane has one list of surface names, the gateway's, read
here from its source; every validator, default, seed and template uses
it; and a check with no list of its own is stored as SQL NULL.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.models.admin import guardrail as schema
from app.constants.guardrail_surfaces import GUARDRAIL_CHECK_SURFACES
from app.database.models import guardrail_config as model
from app.services.guardrails import per_surface
from tests.core_repo import core_revision, core_source

REPO = Path(__file__).resolve().parents[1]
SURFACE_COLUMNS = (
    "pii_surfaces",
    "prompt_injection_surfaces",
    "jailbreak_surfaces",
    "toxic_content_surfaces",
    "banned_words_surfaces",
    "banned_patterns_surfaces",
    "secrets_surfaces",
)


def _gateway(*parts) -> str:
    return core_source("services", "guardrails", *parts).read_text(encoding="utf-8")


def _function(source: str, signature: str) -> str:
    body = source[source.index(signature):]
    return body[: body.index("\n}\n")]


def _array(source: str, declaration: str) -> list:
    body = source[source.index(declaration):]
    return re.findall(r'"([a-z0-9_]+)"', body[: body.index("];")])


def gateway_surfaces() -> list:
    """The names the gateway asks a check's surface list for."""
    body = _function(_gateway("mod.rs"), "pub(crate) fn surface_token(")
    return re.findall(r'=> Some\("([a-z0-9_]+)"\)', body)


def _read() -> str:
    """What was compared against, for a failure message."""
    return f" [the gateway's source read: {core_revision()}]"


def test_the_surface_names_are_the_gateways():
    names = gateway_surfaces()
    assert len(names) == len(set(names)) == 20, names
    assert sorted(GUARDRAIL_CHECK_SURFACES) == sorted(names), "surface_token changed in the gateway" + _read()


def test_the_gateway_reads_a_list_as_this_file_says():
    """What the rule above rests on: a missing list is the default set, an
    array is its entries, anything else is no surface."""
    source = re.sub(r"\s+", " ", _function(_gateway("mod.rs"), "pub(crate) fn check_runs_on_surface("))
    for piece in (
        "None => default_surfaces.contains(&surface_token),",
        "Some(serde_json::Value::Array(items)) => { items.iter().any(|v| v.as_str() == Some(surface_token)) }",
        "_ => false,",
    ):
        assert piece in source, f"check_runs_on_surface changed in the gateway ({piece!r})" + _read()


def test_a_name_the_gateway_lists_and_never_asks_for_is_not_a_surface_here():
    """`mcp_registration` is in the gateway's list of names and no check's
    surface list is asked for it (the registration scan has a switch of its
    own): accepted here, it would be an entry that does nothing."""
    listed = _array(_gateway("per_surface.rs"), "pub const VALID_SURFACE_TOKENS: &[&str] = &[")
    assert sorted(set(listed) - set(gateway_surfaces())) == ["mcp_registration"], _read()
    assert set(gateway_surfaces()) <= set(listed), _read()
    assert "mcp_registration" not in GUARDRAIL_CHECK_SURFACES


def test_the_default_sets_are_the_gateways():
    source = _gateway("mod.rs")
    for declaration, ours in (
        ("pub(crate) const DEFAULT_SURFACES_ALL: &[&str] = &[", model._DEFAULT_SURFACES_ALL),
        ("pub(crate) const DEFAULT_SURFACES_INPUT_ONLY: &[&str] = &[", model._DEFAULT_SURFACES_INPUT_ONLY),
        ("pub(crate) const DEFAULT_SURFACES_LLM_MCP: &[&str] = &[", model._DEFAULT_SURFACES_LLM_MCP),
    ):
        assert sorted(_array(source, declaration)) == sorted(ours), declaration + _read()


def test_which_default_set_each_check_has_is_the_gateways():
    """What a check with no list of its own is shown to run on is what the
    gateway runs it on."""
    source = re.sub(r"\s+", " ", _gateway("orchestrator.rs"))
    gateway_default = {
        column: re.search(rf"config\.{column}\.as_ref\(\), super::(DEFAULT_SURFACES_[A-Z_]+)", source).group(1)
        for column in SURFACE_COLUMNS
    }
    ours = {
        "DEFAULT_SURFACES_ALL": sorted(model._DEFAULT_SURFACES_ALL),
        "DEFAULT_SURFACES_INPUT_ONLY": sorted(model._DEFAULT_SURFACES_INPUT_ONLY),
        "DEFAULT_SURFACES_LLM_MCP": sorted(model._DEFAULT_SURFACES_LLM_MCP),
    }
    row = model.GuardrailConfig(id="g", tenant_id="t", name="g")
    shown = row.to_dict()["builtin"]
    assert sorted(shown["pii"]["surfaces"]) == ours[gateway_default["pii_surfaces"]]
    assert sorted(shown["prompt_injection"]["surfaces"]) == ours[gateway_default["prompt_injection_surfaces"]]
    assert sorted(shown["jailbreak"]["surfaces"]) == ours[gateway_default["jailbreak_surfaces"]]
    assert sorted(shown["toxic_content"]["surfaces"]) == ours[gateway_default["toxic_content_surfaces"]]
    assert sorted(shown["banned_words_surfaces"]) == ours[gateway_default["banned_words_surfaces"]]
    assert sorted(shown["banned_patterns_surfaces"]) == ours[gateway_default["banned_patterns_surfaces"]]
    assert sorted(shown["secrets"]["surfaces"]) == ours[gateway_default["secrets_surfaces"]]


def test_every_list_of_the_control_plane_is_that_one_list():
    assert tuple(schema.ALLOWED_CHECK_SURFACES) == tuple(GUARDRAIL_CHECK_SURFACES)
    assert tuple(per_surface.VALID_SURFACE_TOKENS) == tuple(GUARDRAIL_CHECK_SURFACES)
    assert sorted(model.ALL_SURFACES) == sorted(GUARDRAIL_CHECK_SURFACES)


def _surface_lists_of(module) -> dict:
    return {
        name: value
        for name, value in vars(module).items()
        if "SURFACES" in name
        and name != "ALLOWED_SURFACES"  # the test route's: held to the gateway below
        and isinstance(value, (list, tuple))
        and all(isinstance(v, str) for v in value)
    }


def test_every_default_seed_and_template_names_only_those():
    from app.database import seed_guardrails

    lists = {}
    for module in (model, seed_guardrails, schema, per_surface):
        for name, value in _surface_lists_of(module).items():
            lists[f"{module.__name__}.{name}"] = value
    assert len(lists) >= 12, sorted(lists)
    for name, value in lists.items():
        assert set(value) <= set(GUARDRAIL_CHECK_SURFACES), (name, sorted(set(value) - set(GUARDRAIL_CHECK_SURFACES)))


def test_no_source_names_a_surface_the_gateway_dropped():
    """`llm_input` and `llm_output` were surface names once, and `llm` a
    surface of the test route; the gateway asks no list for them."""
    dropped = re.compile(
        r"""["'](llm_input|llm_output)["']|surfaces?["']?\s*[:=]\s*\[?\s*["']llm["']"""
    )
    found = []
    for root in ("app", "enterprise/brutor_enterprise"):
        for path in sorted((REPO / root).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if dropped.search(line):
                    found.append(f"{path.relative_to(REPO)}:{number}")
    assert found == []


def test_the_surfaces_of_the_test_route_are_the_gateways():
    """`POST .../guardrails/test` names a surface the way the gateway's gate
    does: an LLM route by its own name, MCP and skills by kind with a
    direction beside it."""
    body = _function(_gateway("mod.rs"), "fn surface_enabled(")
    kinds = list(dict.fromkeys(re.findall(r'\("([a-z0-9_]+)", (?:_|SurfaceKind::\w+)\) =>', body)))
    assert sorted(schema.ALLOWED_SURFACES) == sorted(kinds), "surface_enabled changed in the gateway" + _read()
    with pytest.raises(ValidationError):
        schema.GuardrailTestRequest(content="x", surface="llm")


def test_a_check_without_a_list_is_stored_as_sql_null():
    """Not as a JSON `null`, which the gateway reads as a list it cannot
    use: the check would run on no surface while it is shown here with the
    default set."""
    table = model.GuardrailConfig.__table__
    for column in SURFACE_COLUMNS:
        assert table.c[column].type.none_as_null is True, column
        assert table.c[column].nullable is True, column
        # No default the database fills in: an absent list is the gateway's default set.
        assert table.c[column].server_default is None and table.c[column].default is None, column


def test_a_seeded_configuration_covers_the_llm_surfaces_with_secrets_detection():
    from app.database import seed_guardrails

    for name, value in _surface_lists_of(seed_guardrails).items():
        if "WIDE" in name:
            assert {"chat_input", "chat_output", "mcp_input", "mcp_output"} <= set(value), name


@pytest.mark.parametrize("field", SURFACE_COLUMNS)
@pytest.mark.parametrize("name", ["llm_input", "llm_output", "llm", "mcp", "mcp_registration", "chat", "Chat_Input", ""])
def test_a_name_the_gateway_does_not_ask_for_is_refused_at_save(field, name):
    with pytest.raises(ValidationError) as refused:
        schema.GuardrailBuiltinConfig(**{field: ["chat_input", name]})
    message = str(refused.value)
    assert repr(name)[1:-1] in message
    # The sentence lists every accepted name.
    for accepted in GUARDRAIL_CHECK_SURFACES:
        assert accepted in message


@pytest.mark.parametrize("field", SURFACE_COLUMNS)
def test_every_name_the_gateway_asks_for_is_accepted(field):
    built = schema.GuardrailBuiltinConfig(**{field: list(GUARDRAIL_CHECK_SURFACES)})
    assert getattr(built, field) == list(GUARDRAIL_CHECK_SURFACES)


@pytest.mark.parametrize("name", ["llm_input", "mcp_registration", "bogus"])
def test_an_override_for_a_surface_the_gateway_does_not_ask_for_is_refused(name):
    with pytest.raises(ValidationError) as refused:
        schema.GuardrailBuiltinConfig(pii_per_surface={name: {"action": "block"}})
    assert "chat_input" in str(refused.value) and "a2a_outbound" in str(refused.value)


def test_a_provider_is_answered_without_a_mode_or_surfaces_of_its_own():
    """A provider entry has neither: the description of the API must not
    let a client believe it has."""
    answered = schema.GuardrailProviderResponse.model_json_schema()
    assert set(answered["properties"]) == {"provider", "enabled", "endpoint", "config", "config_secrets_stored"}
    assert answered["additionalProperties"] is False
    accepted = schema.GuardrailProviderConfig.model_json_schema()
    assert not {"mode", "surfaces"} & set(accepted["properties"])
