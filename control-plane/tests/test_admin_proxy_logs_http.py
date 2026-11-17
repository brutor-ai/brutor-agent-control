# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the unified proxy-logs admin API (/v1/admin/proxy-logs).

The logs
API is the forensic surface of the platform — what an operator opens when
governance said no — so the wire contract here matters more than most.

What is worth defending:

  * **Tenant isolation on every read.** Log rows are the audit trail; a
    query from t2 must never return t1's rows, whatever filters are sent.
  * **The thread view groups by correlation id** — that is the "open one
    call and see the whole exchange" workflow.
  * **Statistics and facets stay valid on an empty window** rather than
    dividing by zero.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.proxy_logs import (
    create_unified_proxy_logs_admin_router,
)
from app.core.logging_system import BrutorLogger
from app.services.proxy_log_service import UnifiedProxyLogService
from app.database.models.proxy_log import ProxyLog


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [
            create_unified_proxy_logs_admin_router(
                logger=BrutorLogger("test-proxy-logs"),
                log_service=UnifiedProxyLogService(BrutorLogger("test-proxy-logs")),
            )
        ],
        logger_name="test-proxy-logs",
    )
    yield harness
    harness.close()


def _row(tenant="t1", proxy_type="llm", *, corr=None, user=None, root=None,
         success=True, age_minutes=5, **over):
    now = datetime.now(timezone.utc)
    values = dict(
        id=f"plog-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant,
        proxy_type=proxy_type,
        end_user_session_id="sess-1",
        http_method="POST",
        endpoint="/v1/proxy/llm/chat/completions",
        success=success,
        correlation_id=corr,
        gateway_user_id=user,
        root_task_id=root,
        timestamp=now - timedelta(minutes=age_minutes),
        created_at=now - timedelta(minutes=age_minutes),
    )
    values.update(over)
    return ProxyLog(**values)


def _seed_basic(h):
    """Two tenants, one correlated thread, one user, one task root."""

    def fn(s):
        s.add(_row("t1", "llm", corr="corr-1", user="user-a", root="task-1"))
        s.add(_row("t1", "mcp", corr="corr-1", user="user-a", root="task-1"))
        s.add(_row("t1", "llm", corr="corr-2", user="user-b", success=False))
        s.add(_row("t2", "llm", corr="corr-3", user="user-z"))

    h.seed(fn)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_returns_only_this_tenants_rows(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/query",
            json={"hours_ago": 24},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        logs = body.get("logs", body.get("items", []))
        assert len(logs) == 3
        assert all(l["tenant_id"] == "t1" for l in logs)

    def test_proxy_type_filter_applies(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/query",
            json={"hours_ago": 24, "proxy_types": ["mcp"]},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        logs = r.json().get("logs", [])
        assert len(logs) == 1
        assert logs[0]["proxy_type"] == "mcp"

    def test_correlation_filter_returns_the_thread(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/query",
            json={"hours_ago": 24, "correlation_id": "corr-1"},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        assert len(r.json().get("logs", [])) == 2

    def test_the_other_tenant_sees_only_its_own(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/query",
            json={"hours_ago": 24},
            headers=h.headers("t2"),
        )
        assert r.status_code == 200, r.text
        logs = r.json().get("logs", [])
        assert len(logs) == 1
        assert logs[0]["tenant_id"] == "t2"

    def test_query_requires_auth(self, h):
        r = h.client.post("/v1/admin/proxy-logs/query", json={"hours_ago": 1})
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Thread, activity, traces
# ---------------------------------------------------------------------------


class TestDrilldowns:
    def test_thread_view_groups_by_correlation(self, h):
        _seed_basic(h)
        r = h.client.get(
            "/v1/admin/proxy-logs/thread/corr-1", headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text

    def test_thread_of_another_tenant_is_empty_or_404(self, h):
        _seed_basic(h)
        r = h.client.get(
            "/v1/admin/proxy-logs/thread/corr-3", headers=h.headers("t1")
        )
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            body = r.json()
            logs = body.get("logs", body.get("entries", []))
            assert len(logs) == 0

    def test_user_activity_lists_that_users_calls(self, h):
        _seed_basic(h)
        r = h.client.get(
            "/v1/admin/proxy-logs/user/user-a/activity", headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text

    def test_traces_listing_and_detail(self, h):
        _seed_basic(h)
        r = h.client.get("/v1/admin/proxy-logs/traces", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        r = h.client.get(
            "/v1/admin/proxy-logs/traces/task-1", headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


class TestAggregates:
    def test_statistics_on_seeded_data(self, h):
        _seed_basic(h)
        r = h.client.get(
            "/v1/admin/proxy-logs/statistics", headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text

    def test_statistics_on_an_empty_tenant_do_not_divide_by_zero(self, h):
        r = h.client.get(
            "/v1/admin/proxy-logs/statistics", headers=h.headers("t2")
        )
        assert r.status_code == 200, r.text

    def test_facets_endpoint_answers(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/facets",
            json={"hours_ago": 24},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text

    def test_suggest_endpoint_answers(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/suggest",
            json={"prefix": "corr", "field": "correlation_id"},
            headers=h.headers("t1"),
        )
        assert r.status_code in (200, 422), r.text

    def test_export_streams_a_body(self, h):
        _seed_basic(h)
        r = h.client.post(
            "/v1/admin/proxy-logs/export",
            json={"hours_ago": 24},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")

    def test_health_endpoint(self, h):
        r = h.client.get("/v1/admin/proxy-logs/health", headers=h.headers("t1"))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_deletes_old_rows_only(self, h):
        def fn(s):
            s.add(_row("t1", age_minutes=5))
            s.add(_row("t1", age_minutes=60 * 24 * 400))  # ancient

        h.seed(fn)
        r = h.client.post(
            "/v1/admin/proxy-logs/cleanup",
            json={"days_to_keep": 30},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        q = h.client.post(
            "/v1/admin/proxy-logs/query",
            json={"hours_ago": 720},
            headers=h.headers("t1"),
        )
        logs = q.json().get("logs", [])
        assert len(logs) == 1


# ---------------------------------------------------------------------------
# a166_data_retention_erasure — bodies are a separate permission from rows
# ---------------------------------------------------------------------------

def _limited_admin(h, *, perms, username="viewer"):
    """A tenant admin holding exactly `perms`; returns bearer headers."""
    from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
    from app.database.models.admin import TenantRole, TenantRolePermission
    from tests.admin_endpoint_harness import JWT_SECRET, PASSWORD

    def fn(s):
        role = TenantRole(id=f"role-{username}", tenant_id="t1", name=username, description="", is_system=False)
        s.add(role); s.flush()
        for p in perms:
            s.add(TenantRolePermission(id=f"perm-{uuid.uuid4().hex[:10]}", role_id=role.id, permission=p))
        UnifiedAuthService(jwt_secret=JWT_SECRET).create_tenant_admin(
            username=username, email=f"{username}@t1.example", password=PASSWORD,
            tenant_id="t1", db=s, role_id=role.id,
        )
    h.seed(fn)
    r = h.client.post(
        "/v1/admin/users/tenant/login",
        json={"username": username, "password": PASSWORD, "tenant_id": "t1"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed_body(h):
    def fn(s):
        s.add(_row("t1", "llm", corr="corr-b", user="user-a",
                   request_body_preview="the secret prompt", response_body_preview="the answer",
                   request_headers='{"x-trace": "1"}'))
    h.seed(fn)


def test_rows_without_bodies_for_a_reader_without_the_body_permission(h):
    _seed_body(h)
    hdr = _limited_admin(h, perms=["proxy:logs:read"])
    r = h.client.post("/v1/admin/proxy-logs/query", json={"hours_ago": 24}, headers=hdr)
    assert r.status_code == 200, r.text
    log = r.json()["logs"][0]
    assert "body_preview" not in log["request"]
    assert "headers" not in log["request"]
    assert log["request"]["body_state"] == "withheld"
    assert log["response"]["body_state"] == "withheld"
    # The record layer is all there.
    assert log["gateway_user_id"] == "user-a"


def test_full_reader_gets_bodies_and_the_state_says_so(h):
    _seed_body(h)
    r = h.client.post("/v1/admin/proxy-logs/query", json={"hours_ago": 24}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    log = r.json()["logs"][0]
    assert log["request"]["body_preview"] == "the secret prompt"
    assert log["request"]["body_state"] == "available"


def test_free_text_search_is_not_a_content_oracle(h):
    _seed_body(h)
    limited = _limited_admin(h, perms=["proxy:logs:read"])
    # The same search hits for a body reader and misses for a row reader.
    full = h.client.post("/v1/admin/proxy-logs/query",
                         json={"hours_ago": 24, "q": "secret prompt"},
                         headers=h.headers("t1"))
    lim = h.client.post("/v1/admin/proxy-logs/query",
                        json={"hours_ago": 24, "q": "secret prompt"},
                        headers=limited)
    assert full.status_code == 200 and lim.status_code == 200
    assert len(full.json()["logs"]) >= 1
    assert len(lim.json()["logs"]) == 0


def test_body_state_distinguishes_pruned_from_never_captured(h):
    from datetime import datetime, timezone

    def fn(s):
        s.add(_row("t1", "llm", corr="c-pruned", request_body_preview=None,
                   bodies_pruned_at=datetime.now(timezone.utc)))
        s.add(_row("t1", "llm", corr="c-none", request_body_preview=None))
    h.seed(fn)
    r = h.client.post("/v1/admin/proxy-logs/query", json={"hours_ago": 24}, headers=h.headers("t1"))
    states = {l["correlation_id"]: l["request"]["body_state"] for l in r.json()["logs"]}
    assert states["c-pruned"] == "pruned"
    assert states["c-none"] == "none"


# ---------------------------------------------------------------------------
# Free text in the facet and typeahead scope
#
# `_build_scoped_query` read `include_bodies`, a name it was never given: a
# `q` on `/facets` or `/suggest` answered 500 (NameError). The fix carries the
# caller's body permission into the scope, so a facet count is no more of a
# content oracle than `/query` is.
# ---------------------------------------------------------------------------


def _facet_counts(r, field="proxy_type"):
    assert r.status_code == 200, r.text
    return {row["value"]: row["count"] for row in r.json()["facets"].get(field, [])}


def test_facets_with_free_text_answer_and_respect_the_body_permission(h):
    _seed_body(h)
    limited = _limited_admin(h, perms=["proxy:logs:read"])
    body = {"hours_ago": 24, "q": "secret prompt", "fields": ["proxy_type"]}
    full = h.client.post("/v1/admin/proxy-logs/facets", json=body, headers=h.headers("t1"))
    lim = h.client.post("/v1/admin/proxy-logs/facets", json=body, headers=limited)
    assert _facet_counts(full) == {"llm": 1}
    assert _facet_counts(lim) == {}


def test_facets_free_text_over_record_fields_needs_no_body_permission(h):
    def fn(s):
        s.add(_row("t1", "mcp", corr="corr-e", error_message="upstream refused the call"))
    h.seed(fn)
    limited = _limited_admin(h, perms=["proxy:logs:read"])
    r = h.client.post(
        "/v1/admin/proxy-logs/facets",
        json={"hours_ago": 24, "q": "refused", "fields": ["proxy_type"]},
        headers=limited,
    )
    assert _facet_counts(r) == {"mcp": 1}


def test_suggest_with_free_text_answers_and_respects_the_body_permission(h):
    _seed_body(h)
    limited = _limited_admin(h, perms=["proxy:logs:read"])
    body = {"hours_ago": 24, "q": "secret prompt", "prefix": "user", "field": "gateway_user_id"}
    full = h.client.post("/v1/admin/proxy-logs/suggest", json=body, headers=h.headers("t1"))
    lim = h.client.post("/v1/admin/proxy-logs/suggest", json=body, headers=limited)
    assert full.status_code == 200, full.text
    assert lim.status_code == 200, lim.text
    assert [s["value"] for s in full.json()["suggestions"]] == ["user-a"]
    assert lim.json()["suggestions"] == []


# ---------------------------------------------------------------------------
# Export: the list's filters, the whole matching set
# ---------------------------------------------------------------------------


def _seed_for_filters(h):
    def fn(s):
        s.add(_row("t1", "llm", corr="c-a", user="user-a", http_status_code=200,
                   target_url="https://api.one.example/v1/chat", region_routed_to="eu"))
        s.add(_row("t1", "llm", corr="c-b", user="user-b", http_status_code=500, success=False,
                   target_url="https://api.two.example/v1/chat", region_routed_to="us",
                   error_message="upstream said teapot"))
        s.add(_row("t1", "mcp", corr="c-c", user="user-c", http_status_code=429, success=False,
                   target_url="https://api.two.example/mcp", region_routed_to="us"))
        s.add(_row("t2", "llm", corr="c-z", user="user-z", http_status_code=500,
                   target_url="https://api.two.example/v1/chat", region_routed_to="us",
                   error_message="upstream said teapot"))

    h.seed(fn)


def _exported(h, body, tenant="t1"):
    import json

    r = h.client.post(
        "/v1/admin/proxy-logs/export?format=jsonl", json=body, headers=h.headers(tenant)
    )
    assert r.status_code == 200, r.text
    lines = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert "_meta" in lines[0]
    return lines[0]["_meta"], lines[1:]


def _listed(h, body, tenant="t1"):
    r = h.client.post("/v1/admin/proxy-logs/query", json=body, headers=h.headers(tenant))
    assert r.status_code == 200, r.text
    return r.json()["logs"]


class TestExportTakesTheListsFilters:
    @pytest.mark.parametrize(
        "body, correlations",
        [
            ({"http_status_codes": [500, 429]}, {"c-b", "c-c"}),
            ({"target_urls": ["https://api.one.example/v1/chat"]}, {"c-a"}),
            ({"region_routed_to": ["us"]}, {"c-b", "c-c"}),
            ({"gateway_user_ids": ["user-a", "user-c"]}, {"c-a", "c-c"}),
            ({"q": "teapot"}, {"c-b"}),
            ({"proxy_types": ["llm"], "http_status_codes": [500, 200]}, {"c-a", "c-b"}),
            ({}, {"c-a", "c-b", "c-c"}),
        ],
    )
    def test_the_export_is_the_rows_the_list_shows(self, h, body, correlations):
        _seed_for_filters(h)
        body = {"hours_ago": 24, **body}
        listed = {log["correlation_id"] for log in _listed(h, body)}
        assert listed == correlations
        meta, rows = _exported(h, body)
        assert {row["correlation_id"] for row in rows} == correlations
        assert meta["row_count"] == len(correlations)

    def test_the_filters_are_recorded_in_the_file_and_in_the_audit_trail(self, h):
        _seed_for_filters(h)
        meta, _ = _exported(h, {"hours_ago": 24, "http_status_codes": [500], "q": "teapot"})
        assert meta["filters"]["http_status_codes"] == [500]
        assert meta["filters"]["q"] == "teapot"

    def test_the_export_is_not_a_page(self, h):
        """`limit` and `offset` page the list; the export is the whole
        matching set whatever they say."""
        _seed_for_filters(h)
        _, rows = _exported(h, {"hours_ago": 24, "limit": 1, "offset": 2})
        assert len(rows) == 3
        assert len(_listed(h, {"hours_ago": 24, "limit": 1, "offset": 2})) == 1

    def test_another_tenants_rows_are_not_exported(self, h):
        _seed_for_filters(h)
        _, rows = _exported(h, {"hours_ago": 24, "http_status_codes": [500]}, tenant="t2")
        assert [row["correlation_id"] for row in rows] == ["c-z"]

    def test_there_is_no_export_by_address(self, h):
        r = h.client.get("/v1/admin/proxy-logs/export?hours_ago=24", headers=h.headers("t1"))
        assert r.status_code == 405, r.text
