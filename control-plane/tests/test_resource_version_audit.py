# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Resource version + authorship (migration a133_resource_version_audit)

Three properties are worth defending here, because each fails silently if
it breaks:

  * **Authorship cannot be forged.** `created_by` / `updated_by` are absent
    from every request model, so a client that supplies them is dropped by
    Pydantic. A field recording *who did this* that the caller can set
    records nothing.

  * **`updated_by` is not rewritten on a no-op save.** `session.dirty` is
    optimistic — it lists objects that were touched, not changed. Stamping
    those would name the wrong person as the last editor, which is worse
    than a null because it looks like an answer.

  * **`server_config` has no version.** Its version belongs to the MCP
    server it configures; a second one here would invite the two to
    disagree and the local one would be the wrong answer.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.agent_identity import AgentGrant, AgentIdentity
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_mixins import (
    DEFAULT_RESOURCE_VERSION,
    ResourceAuthorshipMixin,
    ResourceVersionMixin,
    acting_as,
    get_current_actor,
)
from app.database.models.server_config import ServerConfig


VERSIONED = (LLModel, MCPServer, LLMRoutingGroup, LlmProvider)
UNVERSIONED = (ServerConfig, AgentIdentity)
ALL_RESOURCES = VERSIONED + UNVERSIONED


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestFieldsPresent:
    @pytest.mark.parametrize("model", ALL_RESOURCES, ids=lambda m: m.__tablename__)
    def test_authorship_on_every_resource(self, model):
        cols = model.__table__.c
        assert "created_by" in cols
        assert "updated_by" in cols

    @pytest.mark.parametrize("model", ALL_RESOURCES, ids=lambda m: m.__tablename__)
    def test_timestamps_keep_their_existing_names(self, model):
        # `created_at` / `updated_at` already existed platform-wide.
        # Renaming them to `_date` would have touched every model, API
        # response, UI field and Rust struct for no behavioural gain.
        cols = model.__table__.c
        assert "created_at" in cols
        assert "updated_at" in cols

    @pytest.mark.parametrize("model", VERSIONED, ids=lambda m: m.__tablename__)
    def test_version_where_the_operator_authors_the_thing(self, model):
        assert "version" in model.__table__.c

    def test_server_config_has_no_version(self):
        # Its version belongs to the MCP server it configures.
        assert "version" not in ServerConfig.__table__.c

    def test_agent_identity_has_no_version(self):
        # A credential is not an authored artifact — it has no releases.
        assert "version" not in AgentIdentity.__table__.c

    def test_agent_grant_is_not_an_ai_resource(self):
        # AgentGrant sits in the same module and has its own created_by.
        # It must not have picked up the mixin by accident.
        assert not issubclass(AgentGrant, ResourceAuthorshipMixin)
        assert "updated_by" not in AgentGrant.__table__.c

    @pytest.mark.parametrize("model", VERSIONED, ids=lambda m: m.__tablename__)
    def test_new_version_columns_default_to_1_0(self, model):
        assert model.__table__.c.version.server_default.arg == DEFAULT_RESOURCE_VERSION

    def test_existing_semver_defaults_are_left_alone(self):
        # agent_skill / agent_card shipped with a semver convention. New
        # columns default to '1.0' per spec; rewriting the two that already
        # exist would change behaviour for no gain.
        from app.database.models.agent_card import AgentCard
        from app.database.models.agent_skill import AgentSkill

        assert AgentSkill.__table__.c.version.default.arg == "1.0.0"
        assert AgentCard.__table__.c.version.default.arg == "1.0.0"

    @pytest.mark.parametrize("model", VERSIONED, ids=lambda m: m.__tablename__)
    def test_version_is_not_nullable(self, model):
        # A resource always has a version; nullable would force every
        # reader to handle an absent one forever.
        assert model.__table__.c.version.nullable is False

    def test_mixins_are_the_single_definition(self):
        for model in ALL_RESOURCES:
            assert issubclass(model, ResourceAuthorshipMixin), model.__name__
        for model in VERSIONED:
            assert issubclass(model, ResourceVersionMixin), model.__name__


# ---------------------------------------------------------------------------
# Automatic stamping
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[MCPServer.__table__])
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


def make_server(session, sid: str = "mcp-1") -> MCPServer:
    srv = MCPServer(
        id=sid,
        tenant_id="t1",
        name=sid,
        base_url="https://example.invalid",
    )
    session.add(srv)
    return srv


class TestAuthorshipStamping:
    def test_created_and_updated_by_are_stamped_on_insert(self, session):
        with acting_as("alice"):
            srv = make_server(session)
            session.flush()
        assert srv.created_by == "alice"
        assert srv.updated_by == "alice"

    def test_updated_by_changes_on_a_real_edit(self, session):
        with acting_as("alice"):
            srv = make_server(session)
            session.flush()
        with acting_as("bob"):
            srv.description = "now with a description"
            session.flush()

        assert srv.created_by == "alice", "creator must not be rewritten"
        assert srv.updated_by == "bob"

    def test_no_op_save_does_not_rewrite_updated_by(self, session):
        with acting_as("alice"):
            srv = make_server(session)
            session.flush()
        with acting_as("bob"):
            # Touched but unchanged — `session.dirty` would still list it.
            srv.description = srv.description
            session.flush()

        assert srv.updated_by == "alice", (
            "naming the wrong person as last editor is worse than a null, "
            "because it looks like an answer"
        )

    def test_no_actor_leaves_the_fields_null(self, session):
        # A seed at boot, a migration, a test harness. A null honestly says
        # "we do not know who"; inventing 'system' would look like an account.
        srv = make_server(session)
        session.flush()
        assert srv.created_by is None
        assert srv.updated_by is None
        assert get_current_actor() is None

    def test_explicit_created_by_is_not_overwritten(self, session):
        # An importer that genuinely knows the original author keeps it.
        with acting_as("alice"):
            srv = make_server(session)
            srv.created_by = "imported-from-legacy"
            session.flush()
        assert srv.created_by == "imported-from-legacy"
        assert srv.updated_by == "alice"

    def test_actor_context_does_not_leak(self, session):
        with acting_as("alice"):
            assert get_current_actor() == "alice"
        assert get_current_actor() is None

    def test_version_defaults_without_being_asked(self, session):
        srv = make_server(session)
        session.flush()
        assert srv.version == DEFAULT_RESOURCE_VERSION

    def test_version_is_operator_settable(self, session):
        with acting_as("alice"):
            srv = make_server(session)
            srv.version = "2.3"
            session.flush()
        assert srv.version == "2.3"


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------


class TestApiContract:
    def test_authorship_is_absent_from_request_models(self):
        from app.api.v1.models.admin.llm_model import LLModelCreate, LLModelUpdate
        from app.api.v1.models.admin.llm_provider import LLProviderUpdate
        from app.api.v1.models.admin.llm_routing_group import RoutingGroupUpdate

        for model in (LLModelCreate, LLModelUpdate, LLProviderUpdate, RoutingGroupUpdate):
            assert "created_by" not in model.model_fields, model.__name__
            assert "updated_by" not in model.model_fields, model.__name__

    def test_forged_authorship_is_refused(self):
        from pydantic import ValidationError

        from app.api.v1.models.admin.llm_model import LLModelUpdate

        with pytest.raises(ValidationError) as refused:
            LLModelUpdate(version="2.1", created_by="attacker", updated_by="attacker")
        assert {error["loc"][0] for error in refused.value.errors()} == {"created_by", "updated_by"}
        assert LLModelUpdate(version="2.1").model_dump(exclude_unset=True) == {"version": "2.1"}

    def test_version_is_writable(self):
        from app.api.v1.models.admin.llm_model import LLModelUpdate

        assert LLModelUpdate(version="3.0").version == "3.0"

    def test_responses_expose_the_general_tab_block(self):
        from app.api.v1.models.admin.llm_model import LLModelResponse
        from app.api.v1.models.admin.mcp_server import ServerResponse

        for model in (LLModelResponse, ServerResponse):
            for field in ("version", "created_by", "updated_by"):
                assert field in model.model_fields, f"{model.__name__}.{field}"

    def test_server_config_request_has_no_version(self):
        from app.api.v1.models.admin.server_config import (
            ServerConfigCreate,
            ServerConfigUpdate,
        )

        assert "version" not in ServerConfigCreate.model_fields
        assert "version" not in ServerConfigUpdate.model_fields

    def test_to_dict_emits_the_block(self):
        # Pydantic strips undeclared fields, so a value missing from
        # to_dict() vanishes from every GET regardless of the response model.
        srv = MCPServer(
            id="m1", tenant_id="t1", name="m1", base_url="https://example.invalid"
        )
        srv.version = "1.4"
        srv.created_by = "alice"
        srv.updated_by = "bob"
        d = srv.to_dict()
        assert d["version"] == "1.4"
        assert d["created_by"] == "alice"
        assert d["updated_by"] == "bob"
        assert "created_at" in d and "updated_at" in d


# ---------------------------------------------------------------------------
# Migration drift
# ---------------------------------------------------------------------------
