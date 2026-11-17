# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The log tail is opened with a ticket, never with the session token.

A browser cannot put an `Authorization` header on a WebSocket upgrade, so
whatever opens the stream is in the address, and an address is written
down: by a proxy's access log, by the browser when the connection fails.
The address therefore carries a ticket: minted by an authenticated
request, random, good for thirty seconds and for one connection, bound to
the administrator, its tenant and this stream. The connection is held to
every check the session token was: the session is still open, the
administrator is still active and still holds `system:read`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.v1.endpoints.admin import system_logs_ws
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin_stream_ticket import AdminStreamTicket
from tests.admin_endpoint_harness import build_admin_harness

MINT = "/v1/admin/system/logs/tail/ticket"
TAIL = "/v1/admin/system/logs/tail"


@pytest.fixture()
def h(monkeypatch):
    harness = build_admin_harness([system_logs_ws.create_system_logs_ws_router(BrutorLogger("tail-ticket"))])
    # The upgrade runs outside a request: it opens its own session.
    monkeypatch.setattr(system_logs_ws, "SessionLocal", harness.Factory)
    yield harness
    harness.close()


def _ticket(h, headers=None) -> str:
    r = h.client.post(MINT, headers=headers or h.headers("t1"))
    assert r.status_code == 201, r.text
    assert r.json()["expires_in"] == 30
    return r.json()["ticket"]


def _opens(h, query: str) -> bool:
    try:
        with h.client.websocket_connect(f"{TAIL}?{query}") as ws:
            while True:
                if ws.receive_json().get("type") == "hello":
                    return True
    except WebSocketDisconnect:
        return False


def _rows(h) -> list:
    with h.Factory() as s:
        return s.query(AdminStreamTicket).all()


def test_a_ticket_opens_the_stream_once(h):
    ticket = _ticket(h)
    assert len(ticket) >= 43
    assert _opens(h, f"ticket={ticket}")
    # Consumed when it was presented: the same address opens nothing again.
    assert _rows(h) == []
    assert not _opens(h, f"ticket={ticket}")


def test_the_session_token_opens_nothing(h):
    token = h.headers("t1")["Authorization"].split(" ", 1)[1]
    assert not _opens(h, f"token={token}")
    assert not _opens(h, f"ticket={token}")
    assert not _opens(h, "")


def test_the_ticket_is_not_stored_and_is_bound(h):
    ticket = _ticket(h)
    (row,) = _rows(h)
    assert ticket not in {str(value) for value in vars(row).values()}
    assert len(row.ticket_hash) == 64
    assert row.stream == "system-logs-tail" and row.tenant_id == "t1" and row.admin_id
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    assert timedelta(seconds=25) < expires - datetime.now(timezone.utc) <= timedelta(seconds=30)


def test_each_ticket_is_its_own(h):
    assert len({_ticket(h) for _ in range(5)}) == 5


def test_a_ticket_past_its_thirty_seconds_opens_nothing(h):
    ticket = _ticket(h)
    with h.Factory() as s:
        s.query(AdminStreamTicket).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        s.commit()
    assert not _opens(h, f"ticket={ticket}")
    # And it is spent, not left to be tried again.
    assert _rows(h) == []


def test_a_ticket_for_another_stream_opens_nothing(h):
    ticket = _ticket(h)
    with h.Factory() as s:
        s.query(AdminStreamTicket).update({"stream": "something-else"})
        s.commit()
    assert not _opens(h, f"ticket={ticket}")


def test_minting_asks_for_what_the_stream_asks_for(h):
    assert h.client.post(MINT).status_code in (401, 403)
    limited = h.limited_headers("t1", [Permission.ROLES_READ])
    assert h.client.post(MINT, headers=limited).status_code == 403
    assert _rows(h) == []
    reader = h.limited_headers("t1", [Permission.SYSTEM_READ])
    assert _opens(h, f"ticket={_ticket(h, reader)}")


def test_a_session_that_ended_after_the_ticket_was_minted_opens_nothing(h):
    headers = h.headers("t1")
    ticket = _ticket(h, headers)
    assert h.client.post("/v1/admin/users/logout", headers=headers).status_code == 200
    h._tokens.clear()
    assert not _opens(h, f"ticket={ticket}")


def test_an_administrator_who_lost_the_permission_opens_nothing(h):
    from app.database.models.admin import TenantAdmin, TenantRolePermission

    reader = h.limited_headers("t1", [Permission.SYSTEM_READ])
    ticket = _ticket(h, reader)
    with h.Factory() as s:
        (row,) = s.query(AdminStreamTicket).all()
        role_id = s.get(TenantAdmin, row.admin_id).role_id
        s.query(TenantRolePermission).filter(TenantRolePermission.role_id == role_id).delete()
        s.commit()
    assert not _opens(h, f"ticket={ticket}")


def test_an_administrator_who_was_deactivated_opens_nothing(h):
    from app.database.models.admin import TenantAdmin

    reader = h.limited_headers("t1", [Permission.SYSTEM_READ])
    ticket = _ticket(h, reader)
    with h.Factory() as s:
        (row,) = s.query(AdminStreamTicket).all()
        s.get(TenantAdmin, row.admin_id).is_active = False
        s.commit()
    assert not _opens(h, f"ticket={ticket}")


def test_minting_sweeps_the_tickets_nobody_presented(h):
    _ticket(h)
    with h.Factory() as s:
        s.query(AdminStreamTicket).update({"expires_at": datetime.now(timezone.utc) - timedelta(minutes=5)})
        s.commit()
    _ticket(h)
    assert len(_rows(h)) == 1


def test_the_stream_takes_no_token_parameter():
    import inspect

    source = inspect.getsource(system_logs_ws)
    handler = source[source.index('@router.websocket("/logs/tail")'):]
    assert "token: str = Query" not in handler and "verify_token(" not in handler
    assert "ticket: str = Query" in handler


def test_a_reader_of_the_stream_gets_the_lines_logged_after_it_connected(h, monkeypatch):
    """The stream streams: a line logged while a reader is connected reaches it."""
    import logging
    import threading

    root = logging.getLogger()
    before = list(root.handlers)
    system_logs_ws.install_tail_handler()
    # A server has one event loop; each test connection here has its own.
    monkeypatch.setattr(system_logs_ws._RING, "_loop", None)
    try:
        with h.client.websocket_connect(f"{TAIL}?ticket={_ticket(h)}") as ws:
            # A stream that stays silent fails the test instead of holding the run.
            watchdog = threading.Timer(30, ws.close)
            watchdog.start()
            try:
                while ws.receive_json().get("type") != "hello":
                    pass
                logging.getLogger("tail-ticket-test").warning("a line for the tail")
                for _ in range(50):
                    line = ws.receive_json()
                    if line.get("message") == "a line for the tail":
                        assert line["level"] == "warn" and line["logger"] == "tail-ticket-test"
                        break
                else:
                    raise AssertionError("the line never reached the reader")
            finally:
                watchdog.cancel()
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


def test_a_system_administrator_names_the_tenant_as_on_the_other_system_routes(h):
    from tests import app_factory

    if app_factory.edition_under_test() != app_factory.ENTERPRISE:
        pytest.skip("an installation without a system administrator")
    assert h.client.post(MINT, headers=h.sys_headers()).status_code == 400
    r = h.client.post(f"{MINT}?tenant_id=t1", headers=h.sys_headers())
    assert r.status_code == 201, r.text
    (row,) = _rows(h)
    assert row.tenant_id == "t1"
    assert _opens(h, f"ticket={r.json()['ticket']}")
    # A tenant administrator's ticket is for its own tenant, whatever it names.
    assert h.client.post(f"{MINT}?tenant_id=t2", headers=h.headers("t1")).status_code == 201
    assert [row.tenant_id for row in _rows(h)] == ["t1"]


def test_a_session_that_expired_after_the_ticket_was_minted_opens_nothing(h):
    """The claims kept with the ticket say when the session's token ends."""
    ticket = _ticket(h)
    with h.Factory() as s:
        (row,) = s.query(AdminStreamTicket).all()
        row.session_claims = {**row.session_claims, "exp": int(datetime.now(timezone.utc).timestamp()) - 5}
        s.commit()
    assert not _opens(h, f"ticket={ticket}")
