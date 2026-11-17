# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pausing, resuming and triggering a background scheduler.

A scheduler belongs to the installation, not to a tenant: pausing the
audit-export scheduler pauses it for everyone. The three actions take the
scheduler's name in the body and `system:operate`, and they are the
installation's operator's: the system administrator where the installation
has one; in a single-tenant installation, which has none, the tenant's
administrator.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin import system_ops
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.scheduler_runtime_state import KNOWN_SCHEDULERS
from tests import app_factory
from tests.admin_endpoint_harness import build_admin_harness

BASE = "/v1/admin/system/tasks"
ACTIONS = ("pause", "resume", "trigger")
MULTI_TENANT = app_factory.edition_under_test() == app_factory.ENTERPRISE


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([system_ops.create_system_ops_router(BrutorLogger("t"))])
    yield harness
    harness.close()


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def record(action):
        def fn(db, *, scheduler_name, actor, tenant_id, reason, request):
            assert db is not None and db.get_bind() is not None
            if scheduler_name not in KNOWN_SCHEDULERS:
                raise system_ops.UnknownSchedulerError(f"Scheduler {scheduler_name!r} is not known")
            seen.append((action, scheduler_name, reason, getattr(actor, "username", None)))
            return {"channel": f"brutor:scheduler:{action}", "payload": scheduler_name}

        return fn

    for action in ACTIONS:
        monkeypatch.setattr(system_ops, f"{action}_scheduler", record(action))
    return seen


def _operator(h):
    """Who may act on a scheduler in the edition under test."""
    return h.sys_headers() if MULTI_TENANT else h.headers("t1")


@pytest.mark.parametrize("action", ACTIONS)
def test_the_action_takes_the_schedulers_name(h, calls, action):
    name = KNOWN_SCHEDULERS[0]
    r = h.client.post(f"{BASE}/{action}", json={"scheduler_name": name, "reason": "maintenance"}, headers=_operator(h))
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    assert [(c[0], c[1], c[2]) for c in calls] == [(action, name, "maintenance")]


@pytest.mark.parametrize("action", ACTIONS)
def test_the_name_is_required_and_known(h, calls, action):
    assert h.client.post(f"{BASE}/{action}", json={}, headers=_operator(h)).status_code == 422
    assert h.client.post(f"{BASE}/{action}", json={"scheduler_name": "no_such"}, headers=_operator(h)).status_code == 404
    r = h.client.post(f"{BASE}/{action}", json={"scheduler_name": KNOWN_SCHEDULERS[0], "until": "x"}, headers=_operator(h))
    assert r.status_code == 422
    assert calls == []


@pytest.mark.parametrize("action", ACTIONS)
def test_reading_the_system_page_does_not_open_it(h, calls, action):
    reader = h.limited_headers("t1", [Permission.SYSTEM_READ])
    r = h.client.post(f"{BASE}/{action}", json={"scheduler_name": KNOWN_SCHEDULERS[0]}, headers=reader)
    assert r.status_code == 403
    assert calls == []


@pytest.mark.skipif(not MULTI_TENANT, reason="an installation with a system administrator")
@pytest.mark.parametrize("action", ACTIONS)
def test_a_tenants_administrator_does_not_act_on_the_installations_scheduler(h, calls, action):
    r = h.client.post(f"{BASE}/{action}", json={"scheduler_name": KNOWN_SCHEDULERS[0]}, headers=h.headers("t1"))
    assert r.status_code == 403, r.text
    assert "System administrator" in r.text
    assert calls == []


@pytest.mark.skipif(MULTI_TENANT, reason="a single-tenant installation")
def test_in_a_single_tenant_installation_the_tenants_operator_acts(h, calls):
    operator = h.limited_headers("t1", [Permission.SYSTEM_OPERATE])
    r = h.client.post(f"{BASE}/pause", json={"scheduler_name": KNOWN_SCHEDULERS[0]}, headers=operator)
    assert r.status_code == 202, r.text


def test_the_documents_name_the_routes_that_exist(h):
    paths = h.app.openapi()["paths"]
    for action in ACTIONS:
        operation = paths[f"{BASE}/{action}"]["post"]
        assert "SchedulerActionRequest" in str(operation["requestBody"])
        assert "installation" in operation["description"]
    schema = h.app.openapi()["components"]["schemas"]["SchedulerActionRequest"]
    assert schema["required"] == ["scheduler_name"]
    assert "/schedulers/" not in schema["description"]
    assert "/tasks/" in schema["description"]


# --- the reason of a pause, and the answer to a name nobody knows --------------


def _row(h, name) -> dict:
    # The system page is read for a tenant; the schedulers are the installation's.
    r = h.client.get(BASE, params={"tenant_id": "t1"} if MULTI_TENANT else {}, headers=_operator(h))
    assert r.status_code == 200, r.text
    (row,) = [item for item in r.json()["items"] if item["scheduler_name"] == name]
    return row


def test_a_paused_scheduler_says_why(h):
    name = KNOWN_SCHEDULERS[-1]
    assert _row(h, name)["pause_reason"] is None

    r = h.client.post(f"{BASE}/pause", json={"scheduler_name": name, "reason": "provider maintenance"}, headers=_operator(h))
    assert r.status_code == 202, r.text
    row = _row(h, name)
    assert (row["paused"], row["pause_reason"]) == (True, "provider maintenance")

    # A pause given no reason has none: the earlier one is not kept for it.
    assert h.client.post(f"{BASE}/pause", json={"scheduler_name": name}, headers=_operator(h)).status_code == 202
    assert _row(h, name)["pause_reason"] is None

    h.client.post(f"{BASE}/pause", json={"scheduler_name": name, "reason": "again"}, headers=_operator(h))
    assert _row(h, name)["pause_reason"] == "again"
    # A trigger does not touch it; a resume ends the pause and its reason.
    h.client.post(f"{BASE}/trigger", json={"scheduler_name": name, "reason": "one tick"}, headers=_operator(h))
    assert _row(h, name)["pause_reason"] == "again"
    r = h.client.post(f"{BASE}/resume", json={"scheduler_name": name, "reason": "done"}, headers=_operator(h))
    assert r.status_code == 202, r.text
    row = _row(h, name)
    assert (row["paused"], row["pause_reason"]) == (False, None)


@pytest.mark.parametrize("action", ACTIONS)
def test_an_unknown_scheduler_is_answered_for_an_administrator(h, action):
    r = h.client.post(f"{BASE}/{action}", json={"scheduler_name": "no_such"}, headers=_operator(h))
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert "'no_such'" in detail
    for name in KNOWN_SCHEDULERS:
        assert name in detail
    # Nothing about the source code.
    for word in ("KNOWN_SCHEDULERS", "app/", ".py", "constant", "wiring"):
        assert word not in detail, word
