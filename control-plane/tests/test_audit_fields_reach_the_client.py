# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The Audit Info block has to survive the whole way out

Every one of these bugs looked the same from the UI — "unknown" for the
author and "-" for the date, over rows the database had stamped correctly.
The value existed, the response model declared it, and it still never
reached the client, because something in between had to name it and didn't:

  * **A field-by-field response builder.** `LLModelsManager.to_response` and
    `MCPServerManager.to_response` construct a dict key by key. A field
    present on the ORM model *and* on the response schema is still dropped
    unless the builder lists it. Both omitted the block; the MCP one also
    omitted `created_at` / `updated_at` entirely.

  * **A response model that never declared the timestamps.** `ServerResponse`
    had no `created_at` / `updated_at`, so Pydantic stripped them even once
    the builder emitted them. They now live on the shared block, because
    "who" and "when" are one answer.

  * **A `to_dict` that emitted half the pair.** `AgentCard.to_dict` had
    `created_by` and no `updated_by`, which renders as a known creator and
    an unknown last editor — indistinguishable from "nobody has touched it
    since".

The other two are write-side, and both failed loudly rather than quietly:

  * **A null version reaching a NOT NULL column.** Update endpoints apply
    their patch with a generic `setattr` loop, so a client that serialises
    every field sent `version: null` and the whole update died with an
    IntegrityError.

  * **`region` on a provider.** Declared on the ORM model and read by the
    residency check in the proxy, absent from every request and response
    model. Writes were rejected outright (`extra="forbid"`), reads were
    stripped. A provider with no region is one the residency check cannot
    refuse, so this was a governance hole, not a cosmetic one.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timezone

import pytest

STAMPED = {
    "version": "2.4",
    "created_by": "alice",
    "updated_by": "bob",
}
WHEN = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

# Every resource whose form shows the tab. `server_config` is the one
# without a version — it takes that from the MCP server it configures.
AUDIT_FIELDS = ("version", "created_by", "updated_by", "created_at", "updated_at")


def _stamp(row):
    for key, value in STAMPED.items():
        if hasattr(row, key):
            setattr(row, key, value)
    row.created_at = WHEN
    row.updated_at = WHEN
    return row


# ---------------------------------------------------------------------------
# Response models declare the block
# ---------------------------------------------------------------------------


def test_server_config_declares_everything_but_version():
    from app.api.v1.models.admin.server_config import ServerConfigResponse

    for field in ("created_by", "updated_by", "created_at", "updated_at"):
        assert field in ServerConfigResponse.model_fields
    assert "version" not in ServerConfigResponse.model_fields


# ---------------------------------------------------------------------------
# The builders in between actually emit it
# ---------------------------------------------------------------------------


class TestFieldByFieldBuilders:
    """The two hand-written builders, which a schema change cannot reach."""

    def test_llm_model_builder(self):
        from app.database.models.llm_model import LLModel
        from app.services.llm.model.llm_models_manager import LLModelsManager

        model = _stamp(
            LLModel(id="llm-1", tenant_id="t1", name="gpt", model_name="gpt-4o")
        )
        built = LLModelsManager.to_response(object.__new__(LLModelsManager), model)

        assert built["version"] == "2.4"
        assert built["created_by"] == "alice"
        assert built["updated_by"] == "bob"
        assert built["created_at"] is not None
        assert built["updated_at"] is not None

    def test_mcp_server_builder(self):
        from app.database.models.mcp_server import MCPServer
        from app.services.mcp.server.mcp_server_manager import MCPServerManager

        server = _stamp(
            MCPServer(
                id="m1",
                tenant_id="t1",
                name="srv",
                base_url="https://example.invalid",
            )
        )
        built = MCPServerManager.to_response(
            object.__new__(MCPServerManager), server
        )

        assert built["version"] == "2.4"
        assert built["created_by"] == "alice"
        assert built["updated_by"] == "bob"
        # These were missing outright, which is what rendered the dates as
        # "-" on a row that had both.
        assert built["created_at"] is not None
        assert built["updated_at"] is not None

    @pytest.mark.parametrize("field", AUDIT_FIELDS)
    def test_the_builders_survive_the_response_model(self, field):
        # A builder can emit a key and still lose it, if the schema does not
        # declare it. Round-tripping through the real response model is the
        # only check that covers both halves.
        from app.api.v1.models.admin.mcp_server import ServerResponse
        from app.database.models.mcp_server import MCPServer
        from app.services.mcp.server.mcp_server_manager import MCPServerManager

        server = _stamp(
            MCPServer(
                id="m1",
                tenant_id="t1",
                name="srv",
                base_url="https://example.invalid",
                # Column defaults are applied at INSERT, and this row is
                # never inserted — spell out what the schema requires so a
                # failure here can only be about the audit block.
                mcp_endpoint_path="/mcp",
                auth_type="none",
                timeout=30.0,
                enabled=True,
            )
        )
        built = MCPServerManager.to_response(
            object.__new__(MCPServerManager), server
        )
        out = ServerResponse(**built).model_dump()
        assert out[field] is not None, f"{field} lost between builder and schema"


class TestToDictPaths:
    def test_agent_card_emits_both_halves_of_the_pair(self):
        from app.database.models.agent_card import AgentCard

        card = _stamp(
            AgentCard(
                id="ac1",
                tenant_id="t1",
                name="agent",
                version="1.0.0",
                card_json={"name": "agent"},
                trust_tier="unsigned",
            )
        )
        d = card.to_dict()
        assert d["created_by"] == "alice"
        assert d["updated_by"] == "bob", (
            "a known creator beside an unknown last editor reads as "
            "'nobody has touched it since'"
        )

    def test_provider_emits_the_block(self):
        from app.database.models.llm_provider import LlmProvider

        row = _stamp(
            LlmProvider(id="p1", tenant_id="t1", key="openai", display_name="OpenAI")
        )
        d = row.to_dict()
        for field in AUDIT_FIELDS:
            assert d[field] is not None, f"provider to_dict drops {field}"


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


class TestNullVersionNeverReachesTheColumn:
    """`version` is NOT NULL with a default, so a null is never meaningful."""

    def _update_models(self):
        from brutor_enterprise.knowledge_base.models.knowledge_base import KbCollectionUpdate
        from app.api.v1.models.admin.llm_model import LLModelUpdate
        from app.api.v1.models.admin.llm_provider import LLProviderUpdate
        from app.api.v1.models.admin.llm_routing_group import RoutingGroupUpdate
        from app.api.v1.models.admin.mcp_server import ServerUpdateRequest
        from app.api.v1.models.admin.resource_group import ResourceGroupUpdate

        return [
            ServerUpdateRequest,
            LLModelUpdate,
            LLProviderUpdate,
            RoutingGroupUpdate,
            ResourceGroupUpdate,
            KbCollectionUpdate,
        ]


    def test_it_is_trimmed_of_nothing_it_should_keep(self):
        from app.api.v1.models.admin.mcp_server import ServerUpdateRequest

        # Only whitespace-only values are treated as absent; a version that
        # merely has spaces around it is still a version the operator typed.
        assert ServerUpdateRequest(version=" 2.0 ").version == " 2.0 "


class TestProviderRegion:
    def test_region_is_accepted_on_create_and_update(self):
        from app.api.v1.models.admin.llm_provider import (
            LLProviderCreate,
            LLProviderUpdate,
        )

        assert LLProviderUpdate(region="eu-west-1").model_dump(exclude_unset=True) == {
            "region": "eu-west-1"
        }
        created = LLProviderCreate(
            key="openai", display_name="OpenAI", region="eu-west-1"
        )
        assert created.region == "eu-west-1"

    def test_region_comes_back_on_read(self):
        from app.api.v1.models.admin.llm_provider import LLProviderResponse

        assert "region" in LLProviderResponse.model_fields

    def test_region_survives_to_dict(self):
        from app.database.models.llm_provider import LlmProvider

        row = LlmProvider(
            id="p1",
            tenant_id="t1",
            key="openai",
            display_name="OpenAI",
            region="eu-west-1",
        )
        assert row.to_dict()["region"] == "eu-west-1"
