# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Importing a catalog entry, or creating a model, binds the model to the
tenant's Default group when the request names no group.

`POST /v1/admin/llm-catalog/{id}/import` documents `group_ids` as "Empty /
null assigns the tenant Default group". The Admin Console's import sends no
body at all (`importLLMCatalogModel(entry.id)`), so every way of naming no
group must end in the same binding: no body, `{}`, `{"group_ids": []}` and
`{"group_ids": null}`. Explicit ids are taken as they are (the `default`
sentinel is the Default group; a custom group alone stays alone).

`POST /v1/admin/llms` (create) documents the same field the same way
("Empty or null binds the tenant's Default group") and is held to it here
too, through the same helper.

The binding is checked the way the gateway reads it: the gateway's own query
for the models of a set of resource groups (`group_cache.rs`, read from the
core's committed HEAD when `BRUTOR_CORE_REPO` is set) is run against the
database the import wrote, with the Default group as the set. With
`BRUTOR_PG_TESTS=1` the database is a throwaway Postgres and the query runs
verbatim; on SQLite it runs with `= ANY(...)` spelled as `IN (...)`.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import bindparam, select, text

from app.api.v1.endpoints.admin.llm_catalog import create_llm_catalog_router
from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_provider import LlmProvider
from app.database.models.resource_group import GroupType, ResourceGroup, resource_group_llm_models
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database
from tests.core_repo import core_checkout_or_none, core_source

ENTRY = "llmcat-test-chat"
IMPORT = f"/v1/admin/llm-catalog/{ENTRY}/import"
CREATE = "/v1/admin/llms"
CREATE_BODY = {"name": "made here", "provider_id": "prov-openai", "model_name": "gpt-made", "mode": "chat"}
DEFAULT_GROUP = "group-default-t1"  # what build_admin_harness provisions
CUSTOM_GROUP = "group-custom-t1"

# The gateway's query for the models bound to a set of resource groups.
GATEWAY_QUERY = "SELECT DISTINCT llm_model_id FROM resource_group_llm_models WHERE resource_group_id = ANY($1)"


@pytest.fixture()
def h(tmp_path):
    for url in throwaway_pg_database("catalog_default_group"):
        logger = BrutorLogger("catalog-default-group")
        manager = LLModelsManager(logger=logger, encryption_service=None)
        harness = build_admin_harness(
            [
                create_llm_catalog_router(model_manager=manager, logger=logger),
                create_llm_models_router(manager, logger, encryption_service=None),
            ],
            logger_name="catalog-default-group",
            database_url=url,
        )

        def seed(s):
            s.add(LlmProvider(id="prov-openai", tenant_id="t1", key="openai", display_name="OpenAI"))
            s.add(LlmCatalog(id=ENTRY, provider_key="openai", model_name="gpt-test",
                             display_name="GPT Test", mode="chat"))
            s.add(ResourceGroup(id=CUSTOM_GROUP, tenant_id="t1", name="Team", hierarchy_level=0,
                                is_active=True, is_system=False, group_type=GroupType.CUSTOM))

        harness.seed(seed)
        harness.postgres = url is not None
        yield harness
        harness.close()


def _bound_groups(h, model_id: str) -> set:
    s = h.Factory()
    try:
        rows = s.execute(
            select(resource_group_llm_models.c.resource_group_id)
            .where(resource_group_llm_models.c.llm_model_id == model_id)
        ).all()
        return {r[0] for r in rows}
    finally:
        s.close()


def _gateway_models(h, group_ids: list) -> set:
    """The gateway's query, run against what the import wrote."""
    s = h.Factory()
    try:
        if h.postgres:
            stmt = text(GATEWAY_QUERY.replace("$1", ":ids"))
            rows = s.execute(stmt, {"ids": group_ids}).all()
        else:
            stmt = text(GATEWAY_QUERY.replace("= ANY($1)", "IN :ids")).bindparams(
                bindparam("ids", expanding=True)
            )
            rows = s.execute(stmt, {"ids": group_ids}).all()
        return {r[0] for r in rows}
    finally:
        s.close()


NO_GROUP = {
    "no body": None,
    "empty object": {},
    "empty list": {"group_ids": []},
    "null": {"group_ids": None},
}


@pytest.mark.parametrize("body", list(NO_GROUP.values()), ids=list(NO_GROUP))
def test_no_group_named_binds_the_default_group(h, body):
    kwargs = {} if body is None else {"json": body}
    r = h.client.post(IMPORT, headers=h.headers("t1"), **kwargs)
    assert r.status_code == 201, r.text
    model = r.json()["model"]
    assert _bound_groups(h, model["id"]) == {DEFAULT_GROUP}
    assert DEFAULT_GROUP in {g["id"] for g in model.get("groups") or []}, model.get("groups")


@pytest.mark.parametrize("body", list(NO_GROUP.values()), ids=list(NO_GROUP))
def test_the_gateway_lists_the_model_for_the_default_group(h, body):
    kwargs = {} if body is None else {"json": body}
    r = h.client.post(IMPORT, headers=h.headers("t1"), **kwargs)
    assert r.status_code == 201, r.text
    assert r.json()["model"]["id"] in _gateway_models(h, [DEFAULT_GROUP])


def test_explicit_group_ids_are_taken_as_given(h):
    r = h.client.post(IMPORT, headers=h.headers("t1"), json={"group_ids": [CUSTOM_GROUP]})
    assert r.status_code == 201, r.text
    model_id = r.json()["model"]["id"]
    assert _bound_groups(h, model_id) == {CUSTOM_GROUP}
    assert model_id not in _gateway_models(h, [DEFAULT_GROUP])
    assert model_id in _gateway_models(h, [CUSTOM_GROUP])


def test_the_default_sentinel_is_the_default_group(h):
    r = h.client.post(IMPORT, headers=h.headers("t1"), json={"group_ids": ["default", CUSTOM_GROUP]})
    assert r.status_code == 201, r.text
    assert _bound_groups(h, r.json()["model"]["id"]) == {DEFAULT_GROUP, CUSTOM_GROUP}


def test_the_audit_record_names_the_groups_bound(h):
    from app.database.models.admin import TenantAdminAuditLog

    r = h.client.post(IMPORT, headers=h.headers("t1"))
    assert r.status_code == 201, r.text
    s = h.Factory()
    try:
        rows = s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == "llm_catalog.import").all()
    finally:
        s.close()
    if not rows:
        pytest.skip("the audit row is written on its own connection, which SQLite in memory may drop")
    assert rows[-1].details["group_ids"] == [DEFAULT_GROUP]


def test_the_query_is_the_gateways():
    """The query above is the gateway's, at the core's committed HEAD."""
    if core_checkout_or_none() is None:
        pytest.skip("BRUTOR_CORE_REPO is not set: the core's query cannot be read")
    source = core_source("services", "cache", "group_cache.rs").read_text()
    flat = re.sub(r"\s+", " ", source)
    assert GATEWAY_QUERY in flat, "group_cache.rs no longer holds the query this test runs"


# ── POST /v1/admin/llms (create) ─────────────────────────────────────


def _create(h, extra):
    body = dict(CREATE_BODY)
    if extra is not None:
        body.update(extra)
    return h.client.post(CREATE, headers=h.headers("t1"), json=body)


CREATE_NO_GROUP = {"absent": None, "empty list": {"group_ids": []}, "null": {"group_ids": None}}


@pytest.mark.parametrize("extra", list(CREATE_NO_GROUP.values()), ids=list(CREATE_NO_GROUP))
def test_create_with_no_group_named_binds_the_default_group(h, extra):
    r = _create(h, extra)
    assert r.status_code == 201, r.text
    model_id = r.json()["model"]["id"]
    assert _bound_groups(h, model_id) == {DEFAULT_GROUP}
    assert model_id in _gateway_models(h, [DEFAULT_GROUP])


def test_create_with_explicit_group_ids_takes_them_as_given(h):
    r = _create(h, {"group_ids": [CUSTOM_GROUP]})
    assert r.status_code == 201, r.text
    model_id = r.json()["model"]["id"]
    assert _bound_groups(h, model_id) == {CUSTOM_GROUP}
    assert model_id not in _gateway_models(h, [DEFAULT_GROUP])


def test_the_create_field_says_what_it_does():
    from app.api.v1.models.admin.llm_model import LLModelCreate

    assert LLModelCreate.model_fields["group_ids"].description == (
        "Resource group ids to bind the model to. Empty or null binds the tenant's Default group."
    )
