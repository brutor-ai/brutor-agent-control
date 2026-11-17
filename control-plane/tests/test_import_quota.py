# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The import routes ask the quota check.

`TenantService.check_quota` was asked by the admin create routes only. The
routes that create the same rows from somewhere else — an MCP catalog entry,
a model catalog entry, a skill ZIP, a skill source, a remote agent card —
inserted them past the tenant's quota (and, in the Enterprise Edition, past
the licence's ceiling, which is asked through the same check).

Each now asks, before it writes anything of the row:

* a route that creates one row answers as the create route does (429);
* a route that creates several reports the refused item with the reason,
  in its own result format, and goes on with the others;
* a row the quota does not count (an MCP server or model that is imported
  switched off) is not a creation, and is not refused. Switching it on is
  asked by the update route.
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin import agent_cards as agent_cards_module
from app.api.v1.endpoints.admin.agent_cards import create_agent_cards_router
from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router
from app.api.v1.endpoints.admin.llm_catalog import create_llm_catalog_router
from app.api.v1.endpoints.admin.mcp_catalog import create_mcp_catalog_router
from app.api.v1.endpoints.admin.skill_sources import create_skill_sources_admin_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill
from app.database.models.idp_server import IdPServer
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.models.mcp_server import MCPServer
from app.database.models.server_config import ServerConfig
from app.database.models.skill_source import SkillSource
from app.database.models.tenant import Tenant
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture()
def h():
    log = BrutorLogger("test-f1024-imports")
    harness = build_admin_harness(
        [
            create_mcp_catalog_router(log),
            create_llm_catalog_router(LLModelsManager(logger=log, encryption_service=None), log, encryption_service=None),
            create_agent_skills_admin_router(logger=log),
            create_skill_sources_admin_router(log),
            create_agent_cards_router(log),
        ],
        logger_name="test-f1024-imports",
    )

    def fn(s):
        s.add(MCPCatalogEntry(
            id="cat-remote", tenant_id="t1", slug="remote_mcp", name="Remote MCP", runtime="remote",
            auth_pattern="oauth", enabled=True, source="system", remote_config={"fixedURL": "https://mcp.example.com"},
        ))
        s.add(LlmCatalog(id="llmcat-gpt", provider_key="openai", model_name="gpt-4o", display_name="GPT-4o", mode="chat"))
        s.add(SkillSource(
            id="sks-1", tenant_id="t1", name="example", repo_url="https://github.com/example/skills",
            git_ref="main", enabled=True, auto_publish=False,
        ))

    harness.seed(fn)
    yield harness
    harness.close()


def _quota(h, **quotas):
    def fn(s):
        tenant = s.get(Tenant, "t1")
        for name, value in quotas.items():
            setattr(tenant, name, value)

    h.seed(fn)


def _count(h, model, *where):
    with h.Factory() as s:
        return s.query(model).filter(model.tenant_id == "t1", *where).count()


# -- MCP catalog -----------------------------------------------------------


def test_an_mcp_catalog_import_that_is_switched_on_is_one_more_enabled_server(h):
    h.seed(lambda s: s.add(MCPServer(id="srv-0", tenant_id="t1", name="srv-0", base_url="https://mcp.example", enabled=True)))
    _quota(h, max_mcp_servers=1)

    r = h.client.post("/v1/admin/mcp-catalog/cat-remote/import", json={"enabled": True}, headers=h.headers())
    assert r.status_code == 429, r.text
    assert r.json()["detail"]["message"] == "Quota exceeded for mcp-servers. Current: 1, Maximum: 1. Please upgrade your plan."
    # Nothing of the import is written: no server, configuration or IdP.
    assert _count(h, MCPServer) == 1
    assert _count(h, ServerConfig) == 0
    assert _count(h, IdPServer) == 0

    # Imported switched off (the default) it is a row the quota does not count.
    r = h.client.post("/v1/admin/mcp-catalog/cat-remote/import", json={}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert _count(h, MCPServer, MCPServer.enabled.is_(True)) == 1
    assert _count(h, MCPServer) == 2

    _quota(h, max_mcp_servers=2)
    r = h.client.post(
        "/v1/admin/mcp-catalog/cat-remote/import", json={"enabled": True, "name_override": "second"}, headers=h.headers()
    )
    assert r.status_code == 200, r.text


# -- model catalog -----------------------------------------------------------


def test_a_model_catalog_import_is_one_more_enabled_model(h):
    _quota(h, max_llm_models=1)
    r = h.client.post("/v1/admin/llm-catalog/llmcat-gpt/import", json={"name": "one"}, headers=h.headers())
    assert r.status_code == 201, r.text

    r = h.client.post("/v1/admin/llm-catalog/llmcat-gpt/import", json={"name": "two"}, headers=h.headers())
    assert r.status_code == 429, r.text
    assert r.json()["detail"]["message"] == "Quota exceeded for llm models. Current: 1, Maximum: 1. Please upgrade your plan."
    assert _count(h, LLModel) == 1

    # Imported switched off it is a row the quota does not count.
    r = h.client.post(
        "/v1/admin/llm-catalog/llmcat-gpt/import", json={"name": "two", "enabled": False}, headers=h.headers()
    )
    assert r.status_code == 201, r.text
    assert _count(h, LLModel, LLModel.enabled.is_(True)) == 1


# -- skills ------------------------------------------------------------------


def _skill_zip(name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: The {name} skill\n---\n# {name}\n")
        zf.writestr(f"{name}/references/notes.md", "notes")
    return (f"{name}.zip", buffer.getvalue(), "application/zip")


def test_a_skill_zip_past_the_quota_is_reported_as_refused_and_the_others_are_imported(h):
    _quota(h, max_agent_skills=2)
    files = [("files", _skill_zip(name)) for name in ("alpha", "beta", "gamma")]
    r = h.client.post("/v1/admin/agent-skills/import", files=files, headers=h.headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["success_count"], body["error_count"]) == (2, 1)
    results = {item["skill_name"]: item for item in body["results"]}
    assert results["alpha"]["success"] and results["beta"]["success"]
    assert results["gamma"]["success"] is False and results["gamma"]["skill_id"] is None
    assert results["gamma"]["error"] == (
        "Quota exceeded for agent skills. Current: 2, Maximum: 2. Please upgrade your plan."
    )
    with h.Factory() as s:
        assert {skill.name for skill in s.query(AgentSkill).filter(AgentSkill.tenant_id == "t1")} == {"alpha", "beta"}

    # Every file refused: nothing is imported, and the answer says why.
    r = h.client.post("/v1/admin/agent-skills/import", files=[("files", _skill_zip("delta"))], headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["success"] is False
    assert r.json()["results"][0]["error"].startswith("Quota exceeded for agent skills.")
    assert _count(h, AgentSkill) == 2


def _discovered(name):
    return {
        "skill_path": f"skills/{name}/SKILL.md",
        "name": name,
        "description": f"The {name} skill",
        "skill_md_body": f"# {name}\n",
    }


def test_a_skill_source_ingest_past_the_quota_skips_the_new_skill_and_keeps_the_others(h):
    _quota(h, max_agent_skills=2)
    payload = {"commit_sha": "abc1234", "discovered": [_discovered(n) for n in ("alpha", "beta", "gamma")]}
    r = h.client.post("/v1/admin/skill-sources/sks-1/sync-ingest", json=payload, headers=h.headers())
    assert r.status_code == 200, r.text
    summary = r.json()
    assert (summary["created"], summary["skipped"]) == (2, 1)
    assert summary["errors"] == [
        "skills/gamma/SKILL.md: refused: Quota exceeded for agent skills. Current: 2, Maximum: 2. "
        "Please upgrade your plan."
    ]
    # The skills before the refused one are kept.
    with h.Factory() as s:
        assert {skill.name for skill in s.query(AgentSkill).filter(AgentSkill.tenant_id == "t1")} == {"alpha", "beta"}

    # A second sync at the quota: the skills that exist are not creations.
    payload["commit_sha"] = "def5678"
    r = h.client.post("/v1/admin/skill-sources/sks-1/sync-ingest", json=payload, headers=h.headers())
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["unchanged"], r.json()["skipped"]) == (0, 2, 1)


# -- agent cards ---------------------------------------------------------------


def _card(name):
    return {"name": name, "description": f"The {name} agent", "version": "1.0.0", "capabilities": [{"name": "do"}]}


@pytest.fixture()
def remote_cards(monkeypatch):
    """`_fetch_well_known_card` answers a card named after the URL's host."""

    async def fetch(source_url, timeout_secs):
        name = source_url.split("//", 1)[1].split(".", 1)[0]
        return _card(name), f"{source_url}/.well-known/agent-card.json"

    monkeypatch.setattr(agent_cards_module, "_fetch_well_known_card", fetch)


def test_an_agent_card_import_past_the_quota_is_refused(h, remote_cards):
    _quota(h, max_agent_cards=1)
    r = h.client.post("/v1/admin/agent-cards/import", json={"source_url": "https://alpha.test"}, headers=h.headers())
    assert r.status_code == 201, r.text
    r = h.client.post("/v1/admin/agent-cards/import", json={"source_url": "https://beta.test"}, headers=h.headers())
    assert r.status_code == 429, r.text
    assert r.json()["detail"]["message"] == "Quota exceeded for agent cards. Current: 1, Maximum: 1. Please upgrade your plan."
    assert _count(h, AgentCard) == 1


def test_a_bulk_agent_card_import_reports_the_card_past_the_quota_and_keeps_the_others(h, remote_cards):
    _quota(h, max_agent_cards=2)
    r = h.client.post(
        "/v1/admin/agent-cards/import-bulk",
        json={"imports": [{"source_url": f"https://{name}.test"} for name in ("alpha", "beta", "gamma")]},
        headers=h.headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["succeeded"], body["failed"]) == (2, 1)
    refused = body["results"][2]
    assert refused["success"] is False and refused.get("card_id") is None
    assert refused["error"] == "Quota exceeded for agent cards. Current: 2, Maximum: 2. Please upgrade your plan."
    with h.Factory() as s:
        assert {card.name for card in s.query(AgentCard).filter(AgentCard.tenant_id == "t1")} == {"alpha", "beta"}
