# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The version field has to survive the round trip

`a133_resource_version_audit` added `version` to the models and
`a138_rg_kb_version` finished the set. Declaring the
column is the easy half. The Audit Info tab makes it editable on all nine
resource forms, and an edit that reports success and changes nothing is
worse than a field that was never offered — the operator walks away
believing something that isn't true.

Three ways that silently happened, each pinned below:

  * **Absent from `to_dict()`.** Pydantic strips undeclared fields, so a
    value missing from the dict vanishes from every GET no matter what the
    response model says. The provider's block was missing entirely, which
    would have rendered the whole tab blank.

  * **Absent from the request model.** The MCP server and agent skill forms
    both sent `version`; neither request model declared it, so Pydantic
    dropped it before the endpoint ever saw it.

  * **Written to the column but not the document.** An agent card's
    `card_json` is the source of truth and the columns are denormalised
    from it. Setting only the column leaves the served card advertising the
    old version — and the card is the thing clients trust.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.resource_mixins import DEFAULT_RESOURCE_VERSION


# ---------------------------------------------------------------------------
# to_dict()
# ---------------------------------------------------------------------------


class TestToDictEmitsTheBlock:
    def test_provider(self):
        from app.database.models.llm_provider import LlmProvider

        row = LlmProvider(id="prov-1", tenant_id="t1", key="openai", display_name="OpenAI")
        row.version = "2.0"
        row.created_by = "alice"
        row.updated_by = "bob"

        d = row.to_dict()
        assert d["version"] == "2.0"
        assert d["created_by"] == "alice"
        assert d["updated_by"] == "bob"
        # The timestamps the block replaced must still be there — the
        # Audit Info tab renders creator and creation time together.
        assert "created_at" in d
        assert "updated_at" in d

    def test_resource_group(self):
        from app.database.models.resource_group import ResourceGroup

        row = ResourceGroup(id="rg-1", tenant_id="t1", name="Engineering")
        row.version = "1.2"
        row.created_by = "alice"
        row.updated_by = "bob"

        d = row.to_dict()
        assert d["version"] == "1.2"
        assert d["created_by"] == "alice"
        assert d["updated_by"] == "bob"

    def test_kb_collection(self):
        from app.database.models.kb_collection import KbCollection

        row = KbCollection(id="kb-1", tenant_id="t1", name="Handbook")
        row.version = "3.0"
        row.created_by = "alice"

        d = row.to_dict()
        assert d["version"] == "3.0"
        assert d["created_by"] == "alice"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class TestRequestModelsAcceptVersion:
    def test_mcp_server_create_and_update(self):
        from app.api.v1.models.admin.mcp_server import (
            ServerCreateRequest,
            ServerUpdateRequest,
        )

        assert (
            ServerUpdateRequest(version="2.0").model_dump(exclude_unset=True)
            == {"version": "2.0"}
        )
        created = ServerCreateRequest(
            name="s", base_url="https://example.invalid", version="2.0"
        )
        assert created.version == "2.0"

    def test_mcp_server_authorship_is_still_unforgeable(self):
        from app.api.v1.models.admin.mcp_server import ServerUpdateRequest

        from pydantic import ValidationError

        with pytest.raises(ValidationError) as refused:
            ServerUpdateRequest(version="2.0", created_by="attacker", updated_by="attacker")
        assert {error["loc"][0] for error in refused.value.errors()} == {"created_by", "updated_by"}
        assert ServerUpdateRequest(version="2.0").model_dump(exclude_unset=True) == {"version": "2.0"}

    def test_omitted_version_is_not_sent_as_an_explicit_null(self):
        # A PATCH that doesn't mention version must not clear it.
        from app.api.v1.models.admin.mcp_server import ServerUpdateRequest

        assert "version" not in ServerUpdateRequest(name="s").model_dump(
            exclude_unset=True
        )

    def test_skill_update_accepts_name_and_version(self):
        # Both were sent by the admin form and silently dropped, so the
        # form reported success on an edit that never happened.
        from app.api.v1.models.admin.agent_skill import AgentSkillUpdate

        patch = AgentSkillUpdate(name="query-sales", version="2.1.0")
        assert patch.model_dump(exclude_unset=True) == {
            "name": "query-sales",
            "version": "2.1.0",
        }

    def test_skill_update_validates_version_the_same_way_create_does(self):
        from app.api.v1.models.admin.agent_skill import AgentSkillUpdate

        with pytest.raises(ValueError):
            AgentSkillUpdate(version="2.1")  # not major.minor.patch

    def test_skill_update_validates_name_the_same_way_create_does(self):
        from app.api.v1.models.admin.agent_skill import AgentSkillUpdate

        with pytest.raises(ValueError):
            AgentSkillUpdate(name="Not A Valid Name")

    def test_skill_update_still_allows_leaving_both_alone(self):
        from app.api.v1.models.admin.agent_skill import AgentSkillUpdate

        assert AgentSkillUpdate(enabled=False).model_dump(exclude_unset=True) == {
            "enabled": False
        }


# ---------------------------------------------------------------------------
# Explicit null must not defeat the column default
# ---------------------------------------------------------------------------


def test_explicit_none_still_lands_on_the_default():
    # Several create paths splat `model_dump()` (not `exclude_unset`), so
    # an omitted version arrives as an explicit None. That must produce
    # "1.0", not a NOT NULL violation.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    from app.database.models.mcp_server import MCPServer

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[MCPServer.__table__])
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    try:
        srv = MCPServer(
            id="m1",
            tenant_id="t1",
            name="m1",
            base_url="https://example.invalid",
            version=None,
        )
        session.add(srv)
        session.flush()
        assert srv.version == DEFAULT_RESOURCE_VERSION
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Agent card: the document is the source of truth
# ---------------------------------------------------------------------------


class TestAgentCardVersionSync:
    @pytest.fixture()
    def apply(self):
        from app.api.v1.endpoints.admin.agent_cards import _apply_version_to_card_json

        return _apply_version_to_card_json

    def test_version_lands_in_the_served_document(self, apply):
        result = apply({"name": "agent", "version": "1.0.0"}, "2.0.0")
        assert result == {"name": "agent", "version": "2.0.0"}

    def test_the_rest_of_the_document_is_untouched(self, apply):
        card = {"name": "agent", "version": "1.0.0", "skills": [{"id": "s1"}]}
        assert apply(card, "2.0.0")["skills"] == [{"id": "s1"}]

    def test_the_original_is_not_mutated_in_place(self, apply):
        # SQLAlchemy's JSON column only sees a change if the attribute is
        # reassigned; mutating the dict would flip the in-memory value and
        # never write it.
        card = {"name": "agent", "version": "1.0.0"}
        apply(card, "2.0.0")
        assert card["version"] == "1.0.0"

    def test_an_unchanged_version_reports_no_edit(self, apply):
        # Returning the document here would invalidate a good signature on
        # a PATCH that changed nothing about the card.
        assert apply({"name": "agent", "version": "1.0.0"}, "1.0.0") is None

    def test_an_absent_version_reports_no_edit(self, apply):
        assert apply({"name": "agent", "version": "1.0.0"}, None) is None

    def test_a_missing_document_is_left_alone(self, apply):
        assert apply(None, "2.0.0") is None
