# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The catalog states which models take function tools on Chat Completions only
with `reasoning_effort: "none"`.

The gateway reads the rule from the model's row
(`capabilities.tools_require_reasoning_effort_none`) and falls back to the
model's name where the row says nothing
(`brutor-gateway-core/src/services/llm_adapter/openai_family.rs`,
`tools_need_effort_none_by_name`). The seeded catalog said nothing, so the
rule held for these models only as long as the gateway's name list was
right. The seed states it now, on exactly the rows the catalog's change notes
name: the `gpt-6-sol` and `gpt-6-luna` rows and the three `azure/gpt-5.6-*`
rows.

Held three ways: the seed against the list written here; the list against the
gateway's rule, read from its source; the list against the notes, when
`BRUTOR_EDITION_RECORDS_DIR` names the edition records directory (the notes
are in it, and nowhere else is looked at).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.database.init_llms import get_default_llm_configurations
from tests.core_repo import core_revision, core_source
from tests.edition_boundary import EDITION_RECORDS_ENV, edition_record
from tests.source_trees import REPO

CAPABILITY = "tools_require_reasoning_effort_none"

# (provider, model_name) of the rows the catalog notes say have the rule.
ROWS_WITH_THE_RULE = {
    ("openai", "gpt-6-sol"),
    ("openai", "gpt-6-luna"),
    ("azure", "azure/gpt-5.6-sol"),
    ("azure", "azure/gpt-5.6-terra"),
    ("azure", "azure/gpt-5.6-luna"),
}

NOTES_NAME = "model-catalog-changes-2026-11-11.md"


def _notes() -> Path:
    """The catalog change notes, in the edition records directory. Skipped
    without `BRUTOR_EDITION_RECORDS_DIR`; a failure when it is set and the
    notes are not in it."""
    return edition_record(NOTES_NAME)


def _catalog():
    return get_default_llm_configurations()


def _seeded_with_the_rule():
    return {
        (m["provider"], m["model_name"]): m["capabilities"][CAPABILITY]
        for m in _catalog()
        if CAPABILITY in (m.get("capabilities") or {})
    }


def test_the_seed_states_the_rule_on_exactly_those_rows():
    assert _seeded_with_the_rule() == {row: True for row in ROWS_WITH_THE_RULE}


def test_every_row_with_the_rule_is_a_reasoning_chat_model_with_tools():
    by_key = {(m["provider"], m["model_name"]): m for m in _catalog()}
    for key in sorted(ROWS_WITH_THE_RULE):
        row = by_key[key]
        caps = row["capabilities"]
        assert row["mode"] == "chat", key
        assert caps.get("supports_function_calling") is True, key
        assert caps.get("supports_reasoning") is True, key
        # A model that takes tools on the Responses API only is seeded as
        # such and has no `none` level: the two do not go together.
        assert not caps.get("requires_responses_api"), key


def test_the_description_says_so():
    by_key = {(m["provider"], m["model_name"]): m for m in _catalog()}
    for key in sorted(ROWS_WITH_THE_RULE):
        assert re.search(r'only with\s+reasoning_effort\s+"none"', by_key[key]["description"]), key


# --- the gateway's fallback, read from its source ---------------------------

RULE = re.compile(
    r'fn tools_need_effort_none_by_name\(name: &str, azure: bool\) -> bool \{\s*'
    r'let is = \|id: &str\| name == id \|\| name\.strip_prefix\(id\)\.is_some_and\(\|r\| r\.starts_with\(\'-\'\)\);\s*'
    r'is\("gpt-6-sol"\) \|\| is\("gpt-6-luna"\) \|\| \(azure && name\.starts_with\("gpt-5\.6-"\)\)\s*\}'
)


def _family_name(provider: str, model_name: str) -> str:
    """`family_name` of the gateway: lower-cased, without `azure/` on Azure."""
    lower = model_name.strip().lower()
    if provider in ("azure", "azure_ai") and lower.startswith("azure/"):
        return lower[len("azure/"):]
    return lower


def _rule_by_name(provider: str, model_name: str) -> bool:
    """`tools_need_effort_none_by_name` of the gateway, as written there."""
    name = _family_name(provider, model_name)
    azure = provider in ("azure", "azure_ai")

    def is_(model_id: str) -> bool:
        return name == model_id or name.startswith(model_id + "-")

    return is_("gpt-6-sol") or is_("gpt-6-luna") or (azure and name.startswith("gpt-5.6-"))


def test_the_port_of_the_gateways_rule_is_the_gateways_rule():
    source = core_source("services", "llm_adapter", "openai_family.rs").read_text(encoding="utf-8")
    assert RULE.search(source), (
        "tools_need_effort_none_by_name changed in the gateway: bring `_rule_by_name` and "
        f"ROWS_WITH_THE_RULE here, and the seed, in line with it [the gateway's source read: {core_revision()}]"
    )
    assert f'TOOLS_NEED_EFFORT_NONE_CAPABILITY: &str = "{CAPABILITY}"' in source, core_revision()


def test_the_seed_and_the_gateways_fallback_agree_on_every_openai_family_row():
    """For every seeded OpenAI or Azure OpenAI chat row: the row has the
    capability exactly when the gateway's name rule says so. A row the name
    rule misses would only be covered by the seed, and the other way round."""
    disagree = []
    for m in _catalog():
        if m["provider"] not in ("openai", "azure", "azure_ai"):
            continue
        seeded = bool((m.get("capabilities") or {}).get(CAPABILITY))
        if seeded != _rule_by_name(m["provider"], m["model_name"]):
            disagree.append((m["provider"], m["model_name"], seeded))
    assert not disagree, disagree


def test_the_rule_is_not_seeded_on_a_provider_the_gateway_does_not_read_it_for():
    assert {provider for provider, _ in _seeded_with_the_rule()} <= {"openai", "azure", "azure_ai"}


# --- the notes ---------------------------------------------------------------


def test_the_list_is_the_one_the_catalog_notes_give():
    rows = {}
    for line in _notes().read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| new \| — \| add `([^`]+)` \| (.*)", line)
        if m:
            rows[m.group(1)] = m.group(2)
    restricted = set()
    for name, text in rows.items():
        lowered = text.lower()
        if ("only with `reasoning_effort: \"none\"`" in lowered or "only with reasoning_effort none" in lowered
                or "same chat completions tool restriction" in lowered
                or "same capabilities and restriction" in lowered):
            restricted.add(name)
    assert restricted == {name for _, name in ROWS_WITH_THE_RULE}


def test_the_notes_are_read_from_the_records_directory_alone(tmp_path, monkeypatch):
    """Only `BRUTOR_EDITION_RECORDS_DIR` says where the notes are: notes in
    the directory that holds it are not read, and their absence fails."""
    records = tmp_path / "records"
    records.mkdir()
    (tmp_path / NOTES_NAME).write_text("| new | — | add `x` | elsewhere\n", encoding="utf-8")
    monkeypatch.setenv(EDITION_RECORDS_ENV, str(records))
    with pytest.raises(AssertionError, match=NOTES_NAME):
        _notes()
    (records / NOTES_NAME).write_text("| new | — | add `x` | here\n", encoding="utf-8")
    assert _notes() == records / NOTES_NAME
