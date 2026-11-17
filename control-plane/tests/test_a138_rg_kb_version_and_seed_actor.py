# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a138_rg_kb_version — version on resource groups + KB collections, and `system` on seeds

a133_resource_version_audit gave every AI Resource a `version` and system-stamped authorship. Two
resources were missed in a way that looked fine, and one omission was
deliberate:

  * `resource_groups` and `kb_collections` carried **hand-declared**
    `created_by` / `updated_by` columns. Identical columns, so nothing
    looked wrong — but the flush listener stamps objects it recognises by
    mixin, so a local copy silently opted both models out of automatic
    authorship. This is the failure worth a regression test: it is
    invisible in the schema and only shows up as permanently-null fields
    on two of nine resources.

  * `server_configs` still has no `version`, and should not gain one. Its
    version belongs to the MCP server it configures.

The other half is attribution for rows the platform creates itself. A null
reads as "we don't know who" — right for a row that predates authorship
tracking, wrong for one the platform demonstrably created. `system` is not
an invented account either: it cannot log in, so an operator reading the
field learns something true and unambiguous.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import inspect
import logging
import re
from pathlib import Path

import pytest

from app.database.models.kb_collection import KbCollection
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_mixins import (
    DEFAULT_RESOURCE_VERSION,
    SYSTEM_ACTOR,
    ResourceAuthorshipMixin,
    ResourceVersionMixin,
    acting_as,
    get_current_actor,
)
from app.database.models.server_config import ServerConfig


LATECOMERS = (ResourceGroup, KbCollection)


# ---------------------------------------------------------------------------
# The two latecomers
# ---------------------------------------------------------------------------


class TestLatecomersUseTheSharedMixins:
    @pytest.mark.parametrize("model", LATECOMERS, ids=lambda m: m.__tablename__)
    def test_authorship_comes_from_the_mixin_not_a_local_copy(self, model):
        # The whole point of a138_rg_kb_version. Hand-declared columns look identical and
        # are not: the flush listener keys off the mixin, so a local copy
        # leaves both fields null forever.
        assert issubclass(model, ResourceAuthorshipMixin), model.__name__

    @pytest.mark.parametrize("model", LATECOMERS, ids=lambda m: m.__tablename__)
    def test_version_comes_from_the_mixin(self, model):
        assert issubclass(model, ResourceVersionMixin), model.__name__

    @pytest.mark.parametrize("model", LATECOMERS, ids=lambda m: m.__tablename__)
    def test_columns_are_declared_exactly_once(self, model):
        # A subclass re-declaring `created_by` would shadow the mixin's and
        # put us straight back where we started.
        own = {c.name for c in model.__table__.c}
        assert {"created_by", "updated_by", "version"} <= own

        # SQLAlchemy copies mixin columns onto the subclass, so the class
        # dict cannot tell a mixin column from a hand-declared one. The
        # model's own source can: a re-declared column is a line of code in
        # this file, and that line is exactly what a138_rg_kb_version removed.
        source = Path(inspect.getsourcefile(model)).read_text()
        body = source.split(f"class {model.__name__}(", 1)[1]
        for col in ("created_by", "updated_by", "version"):
            assert not re.search(rf"^    {col} = Column\(", body, re.M), (
                f"{model.__name__} re-declares {col}; a local copy is "
                f"identical at the DB level and silently opts the model out "
                f"of the flush listener, which keys off the mixin"
            )

    @pytest.mark.parametrize("model", LATECOMERS, ids=lambda m: m.__tablename__)
    def test_version_defaults_to_1_0_and_is_not_nullable(self, model):
        col = model.__table__.c.version
        assert col.server_default.arg == DEFAULT_RESOURCE_VERSION
        assert col.nullable is False

    def test_server_config_still_has_no_version(self):
        assert "version" not in ServerConfig.__table__.c


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------


class TestApiContract:


    def test_server_config_response_carries_authorship_but_no_version(self):
        from app.api.v1.models.admin.server_config import ServerConfigResponse

        assert "created_by" in ServerConfigResponse.model_fields
        assert "updated_by" in ServerConfigResponse.model_fields
        assert "version" not in ServerConfigResponse.model_fields

    def test_to_dict_emits_the_block(self):
        # Pydantic strips undeclared fields, so a value missing from
        # to_dict() vanishes from every GET regardless of response model.
        from app.database.models.llm_routing_group import LLMRoutingGroup

        rg = LLMRoutingGroup(id="rg-1", tenant_id="t1", name="rg-1", mode="chat")
        rg.version = "2.2"
        rg.created_by = "alice"
        rg.updated_by = "bob"
        d = rg.to_dict()
        assert d["version"] == "2.2"
        assert d["created_by"] == "alice"
        assert d["updated_by"] == "bob"


# ---------------------------------------------------------------------------
# Seeds are attributed to the platform
# ---------------------------------------------------------------------------


class TestSeedActor:
    def test_system_actor_is_a_name_nobody_can_log_in_as(self):
        assert SYSTEM_ACTOR == "system"

    def test_boot_seed_runs_as_system(self, monkeypatch):
        import app.database as db_module

        seen = {}

        def fake_seed():
            seen["actor"] = get_current_actor()

        monkeypatch.setattr(db_module, "_seed_initial_data", fake_seed)
        db_module.seed_initial_data()
        assert seen["actor"] == SYSTEM_ACTOR

    def test_tenant_provisioning_runs_as_system(self, monkeypatch):
        # Provisioning seeds starter servers, skills and guardrails. They
        # are seeded content, not something the admin who created the
        # tenant authored.
        from app.services.tenant.tenant_provisioning import TenantProvisioner

        seen = {}

        def fake_provision(self, *args, **kwargs):
            seen["actor"] = get_current_actor()

        monkeypatch.setattr(TenantProvisioner, "_provision_tenant", fake_provision)
        TenantProvisioner(logger=logging.getLogger(__name__)).provision_tenant(
            db=None, tenant=None
        )
        assert seen["actor"] == SYSTEM_ACTOR

    def test_provisioning_does_not_inherit_the_calling_admin(self, monkeypatch):
        from app.services.tenant.tenant_provisioning import TenantProvisioner

        seen = {}

        def fake_provision(self, *args, **kwargs):
            seen["actor"] = get_current_actor()

        monkeypatch.setattr(TenantProvisioner, "_provision_tenant", fake_provision)
        with acting_as("alice"):
            TenantProvisioner(logger=logging.getLogger(__name__)).provision_tenant(
                db=None, tenant=None
            )
        assert seen["actor"] == SYSTEM_ACTOR

    def test_the_seed_actor_does_not_leak_past_the_call(self, monkeypatch):
        import app.database as db_module

        monkeypatch.setattr(db_module, "_seed_initial_data", lambda: None)
        db_module.seed_initial_data()
        assert get_current_actor() is None


# ---------------------------------------------------------------------------
# Migration drift
# ---------------------------------------------------------------------------
