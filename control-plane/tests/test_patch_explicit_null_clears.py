# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A PATCH with an explicit null clears the field; an omitted key leaves it alone.

Both routes used to drop every None (`if val is not None` / `exclude_none=True`),
so an admin clearing a judge model or a rate limit got a 200 and no change. The
clear must land as SQL NULL. For `surfaces` that is load-bearing: the core's
`SemanticPolicy::applies_to` reads an empty array as "matches no surface", so a
stored `[]` would be a policy that silently never runs.

Runs on SQLite by default and on Postgres under BRUTOR_PG_TESTS=1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.api.v1.endpoints.admin.llm_routing_groups import create_llm_router_admin_router
from app.api.v1.endpoints.admin.semantic_policies import create_semantic_policies_router
from app.api.v1.models.admin.llm_routing_group import RoutingGroupResponse, RoutingGroupUpdate
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.semantic_policy import SemanticPolicy
from app.services.llm.router.llm_router_service import (
    ROUTING_GROUP_CLEARABLE_FIELDS,
    LLMRouterService,
)
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

POLICIES = "/v1/admin/semantic-policies"
GROUPS = "/v1/admin/llm-router/routing-groups"


@pytest.fixture(scope="module")
def harness():
    logger = BrutorLogger("patch-null-tests")
    for url in throwaway_pg_database("patch_null"):
        h = build_admin_harness(
            [
                create_semantic_policies_router(logger),
                create_llm_router_admin_router(LLMRouterService(logger=logger), logger),
            ],
            database_url=url,
        )
        yield h
        h.close()


def _column_is_sql_null(harness, table: str, column: str, row_id: str) -> bool:
    """Read the raw column: a JSON `null` is not SQL NULL, and the ORM hides it."""
    s = harness.Factory()
    try:
        return s.execute(
            text(f"SELECT {column} IS NULL FROM {table} WHERE id = :id"), {"id": row_id}
        ).scalar_one() in (True, 1)
    finally:
        s.close()


def _policy(harness, name: str, **extra) -> dict:
    body = {
        "name": name,
        "description": "before",
        "constraint_text": "no PII",
        "surfaces": ["chat_input"],
        "judge_model_id": "judge-1",
        **extra,
    }
    r = harness.client.post(POLICIES, json=body, headers=harness.headers("t1"))
    assert r.status_code == 201, r.text
    return r.json()


class TestSemanticPolicyPatch:
    @pytest.mark.parametrize("field", ["description", "surfaces", "judge_model_id"])
    def test_explicit_null_clears_and_stores_sql_null(self, harness, field):
        p = _policy(harness, f"clear-{field}")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={field: None}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert r.json()[field] in (None, [])
        assert _column_is_sql_null(harness, "semantic_policies", field, p["id"])

    def test_omitted_keys_are_untouched(self, harness):
        p = _policy(harness, "omit")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={"description": "after"}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["description"] == "after"
        assert body["surfaces"] == ["chat_input"]
        assert body["judge_model_id"] == "judge-1"

    def test_empty_surfaces_is_stored_as_null_never_as_an_empty_array(self, harness):
        p = _policy(harness, "empty-array")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={"surfaces": []}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert _column_is_sql_null(harness, "semantic_policies", "surfaces", p["id"])

    @pytest.mark.parametrize("surfaces", [None, []])
    def test_create_without_surfaces_stores_sql_null(self, harness, surfaces):
        p = _policy(harness, f"create-{surfaces}", surfaces=surfaces)
        assert _column_is_sql_null(harness, "semantic_policies", "surfaces", p["id"])

    def test_a_policy_with_no_surfaces_reads_back_as_all(self, harness):
        p = _policy(harness, "reads-back", surfaces=None)
        got = harness.client.get(f"{POLICIES}/{p['id']}", headers=harness.headers("t1"))
        assert got.json()["surfaces"] == []

    @pytest.mark.parametrize(
        "field,expected",
        [("name", "required-null"), ("constraint_text", "no PII"), ("action", "block"),
         ("mode", "shadow"), ("enabled", True)],
    )
    def test_null_on_a_required_field_changes_nothing(self, harness, field, expected):
        p = _policy(harness, "required-null") if field == "name" else _policy(harness, f"req-{field}")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={field: None}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert r.json()[field] == expected

    def test_bad_surface_is_refused_and_not_stored(self, harness):
        p = _policy(harness, "bad-surface")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={"surfaces": ["nope"]}, headers=harness.headers("t1")
        )
        assert r.status_code == 400
        got = harness.client.get(f"{POLICIES}/{p['id']}", headers=harness.headers("t1"))
        assert got.json()["surfaces"] == ["chat_input"]

    @pytest.mark.parametrize("groups", [["group-default-t2"], ["nope"]])
    def test_a_group_outside_the_tenant_is_refused_not_dropped(self, harness, groups):
        """No groups means tenant default, so dropping the id would widen the policy."""
        r = harness.client.post(
            POLICIES,
            json={"name": f"bad-group-{groups[0]}", "constraint_text": "x", "resource_group_ids": groups},
            headers=harness.headers("t1"),
        )
        assert r.status_code == 400
        p = _policy(harness, f"bound-{groups[0]}", resource_group_ids=["group-default-t1"])
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={"resource_group_ids": groups}, headers=harness.headers("t1")
        )
        assert r.status_code == 400
        got = harness.client.get(f"{POLICIES}/{p['id']}", headers=harness.headers("t1"))
        assert got.json()["resource_group_ids"] == ["group-default-t1"]

    def test_a_failed_binding_sync_does_not_lose_the_save(self, harness):
        """`_sync_system_bindings` is documented as never fatal. A judge id with no
        model behind it fails the binding insert on the foreign key."""
        p = _policy(harness, "sync-fails", resource_group_ids=["group-default-t1"], judge_model_id="no-such-model")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={"description": "kept"}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        s = harness.Factory()
        try:
            assert s.get(SemanticPolicy, p["id"]).description == "kept"
        finally:
            s.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "judge_model_id is not checked on write: an id from another tenant (or none at all) is "
            "stored, and at runtime a policy with no resolvable judge refuses every call (fail closed). "
            "Needs a decision on whether to validate against the tenant's models here."
        ),
    )
    def test_a_judge_model_id_that_does_not_exist_is_refused(self, harness):
        r = harness.client.post(
            POLICIES,
            json={"name": "unknown-judge", "constraint_text": "x", "judge_model_id": "no-such-model"},
            headers=harness.headers("t1"),
        )
        assert r.status_code in (400, 404, 422)

    def test_another_tenant_cannot_clear_it(self, harness):
        p = _policy(harness, "cross-tenant")
        r = harness.client.patch(
            f"{POLICIES}/{p['id']}", json={"judge_model_id": None}, headers=harness.headers("t2")
        )
        assert r.status_code == 404
        s = harness.Factory()
        try:
            assert s.get(SemanticPolicy, p["id"]).judge_model_id == "judge-1"
        finally:
            s.close()

    def test_read_permission_cannot_clear_it(self, harness):
        p = _policy(harness, "read-only")
        ro = harness.limited_headers("t1", [Permission.SEMANTIC_POLICY_READ])
        r = harness.client.patch(f"{POLICIES}/{p['id']}", json={"judge_model_id": None}, headers=ro)
        assert r.status_code == 403
        assert harness.client.get(f"{POLICIES}/{p['id']}", headers=ro).json()["judge_model_id"] == "judge-1"


def _group(harness, group_id: str, **extra) -> dict:
    r = harness.client.post(
        GROUPS,
        json={
            "id": group_id,
            "name": group_id,
            "mode": "chat",
            "description": "before",
            "rpm_limit": 100,
            "tpm_limit": 1000,
            "max_parallel_requests": 5,
            "fallback_models": ["other"],
            **extra,
        },
        headers=harness.headers("t1"),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


class TestRoutingGroupPatch:
    @pytest.mark.parametrize("field", ["rpm_limit", "tpm_limit", "max_parallel_requests"])
    def test_explicit_null_clears_a_rate_limit(self, harness, field):
        g = _group(harness, f"clear-{field}".replace("_", "-"))
        r = harness.client.patch(f"{GROUPS}/{g['id']}", json={field: None}, headers=harness.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()[field] is None
        assert _column_is_sql_null(harness, LLMRoutingGroup.__tablename__, field, g["id"])
        # The sibling limits were not in the body.
        others = {"rpm_limit", "tpm_limit", "max_parallel_requests"} - {field}
        assert all(r.json()[o] is not None for o in others)

    def test_json_clear_stores_sql_null_not_json_null(self, harness):
        g = _group(harness, "clear-fallbacks")
        r = harness.client.patch(
            f"{GROUPS}/{g['id']}", json={"fallback_models": None}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert _column_is_sql_null(harness, LLMRoutingGroup.__tablename__, "fallback_models", g["id"])

    def test_omitted_limit_is_untouched(self, harness):
        g = _group(harness, "omit-limit")
        r = harness.client.patch(
            f"{GROUPS}/{g['id']}", json={"name": "renamed"}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert (r.json()["name"], r.json()["rpm_limit"]) == ("renamed", 100)

    def test_clearing_the_description_reaches_the_virtual_model(self, harness):
        g = _group(harness, "clear-desc")
        r = harness.client.patch(
            f"{GROUPS}/{g['id']}", json={"description": None}, headers=harness.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert r.json()["description"] is None
        s = harness.Factory()
        try:
            virtual = s.get(LLModel, f"rg-{g['id']}")
            assert virtual is not None and virtual.description is None
        finally:
            s.close()

    @pytest.mark.parametrize("field", ["cooldown_time", "num_retries", "retry_after", "enabled", "name"])
    def test_null_on_a_field_the_response_requires_changes_nothing(self, harness, field):
        g = _group(harness, f"keep-{field}".replace("_", "-"))
        r = harness.client.patch(f"{GROUPS}/{g['id']}", json={field: None}, headers=harness.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()[field] == g[field]

    def test_another_tenant_cannot_clear_it(self, harness):
        g = _group(harness, "cross-tenant-rg")
        r = harness.client.patch(f"{GROUPS}/{g['id']}", json={"rpm_limit": None}, headers=harness.headers("t2"))
        assert r.status_code == 404
        s = harness.Factory()
        try:
            assert s.get(LLMRoutingGroup, g["id"]).rpm_limit == 100
        finally:
            s.close()

    def test_every_clearable_field_is_nullable_on_both_sides(self):
        """Class guard: a field added to the set whose response type is required
        would commit the NULL and then 500 on every read of that group."""
        assert ROUTING_GROUP_CLEARABLE_FIELDS <= set(RoutingGroupUpdate.model_fields)
        for field in ROUTING_GROUP_CLEARABLE_FIELDS:
            assert not RoutingGroupResponse.model_fields[field].is_required() or (
                type(None) in getattr(RoutingGroupResponse.model_fields[field].annotation, "__args__", ())
            ), field
            assert LLMRoutingGroup.__table__.c[field].nullable, field
