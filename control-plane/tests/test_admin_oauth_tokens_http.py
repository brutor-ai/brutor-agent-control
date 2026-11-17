# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OAuth token admin: diagnosis, single and bulk revocation. Revoking is destructive
and reaches across to the IdP, so the tenant boundary and the "no token material
in the response or the audit row" rule both have teeth here.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.api.v1.endpoints.admin import oauth_tokens as module
from app.api.v1.endpoints.admin.oauth_tokens import create_admin_oauth_tokens_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.idp_server import IdPServer
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_token import OAuthToken
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/oauth-tokens"
PLAINTEXT = "plain-access-token-value"
_ids = itertools.count()


class FakeEncryption:
    def encrypt(self, value):
        return f"enc::{value}"

    def decrypt(self, value):
        if value == "undecryptable":
            raise ValueError("bad envelope")
        return value.removeprefix("enc::")


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("test-oauth-tokens")
    for url in throwaway_pg_database("oauth_tokens"):
        harness = build_admin_harness([create_admin_oauth_tokens_router(log, FakeEncryption())], database_url=url)

        def seed(s):
            for tid in ("t1", "t2"):
                s.add(MCPServer(id=f"srv-{tid}", tenant_id=tid, name=f"srv-{tid}", base_url="https://mcp.example"))
                s.add(IdPServer(id=f"idp-{tid}", tenant_id=tid, name=f"idp-{tid}", issuer=f"https://{tid}.example",
                                type="manual", revocation_endpoint=f"https://{tid}.example/revoke", enabled=True,
                                token_endpoint_auth_methods=[], scopes_supported=[], response_types_supported=[],
                                grant_types_supported=[]))
                s.add(IdPServer(id=f"idp-off-{tid}", tenant_id=tid, name=f"off-{tid}", issuer=f"https://off.{tid}.example",
                                type="manual", enabled=False, token_endpoint_auth_methods=[], scopes_supported=[],
                                response_types_supported=[], grant_types_supported=[]))

        harness.seed(seed)
        yield harness
        harness.close()


def _token(h, tenant="t1", *, hours=1, refresh=None, revoked=False, idp=None, access=None) -> int:
    def now():
        return datetime.now(timezone.utc)

    s = h.Factory()
    try:
        t = OAuthToken(
            tenant_id=tenant, gateway_user_id="1", idp_user_id=f"u{next(_ids)}", mcp_server_id=f"srv-{tenant}",
            idp_id=idp or f"idp-{tenant}", encrypted_access_token=access or f"enc::{PLAINTEXT}",
            encrypted_refresh_token=refresh, token_type="Bearer", expires_at=now() + timedelta(hours=hours),
            revoked=revoked, revoked_at=now() if revoked else None,
        )
        s.add(t)
        s.commit()
        return t.id
    finally:
        s.close()


def _row(h, tid):
    s = h.Factory()
    try:
        t = s.get(OAuthToken, tid)
        return None if t is None else {"revoked": t.revoked, "access": t.encrypted_access_token, "refresh": t.encrypted_refresh_token}
    finally:
        s.close()


@pytest.fixture()
def idp_http(monkeypatch):
    """Stand-in for the RFC 7009 call. Records what was posted."""
    state = {"calls": [], "status": 200, "raises": None}

    class Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, **kw):
            state["calls"].append({"url": url, **kw})
            if state["raises"]:
                raise state["raises"]
            return httpx.Response(state["status"], request=httpx.Request("POST", url))

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    return state


class TestDiagnosis:
    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({}, "active"),
            ({"hours": -1, "refresh": "enc::r"}, "expired_with_refresh"),
            ({"hours": -1}, "expired_no_refresh"),
            ({"revoked": True}, "revoked"),
            ({"idp": "idp-off-t1"}, "idp_unavailable"),
        ],
    )
    def test_each_state_is_reported_and_filterable(self, h, kwargs, expected):
        tid = _token(h, **kwargs)
        r = h.client.get(f"{BASE}?status={expected}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        rows = {x["id"]: x for x in r.json()["tokens"]}
        assert rows[tid]["status"] == expected
        assert all(x["status"] == expected for x in rows.values())

    def test_a_bad_status_is_refused(self, h):
        assert h.client.get(f"{BASE}?status=nope", headers=h.headers("t1")).status_code == 400

    def test_no_token_material_in_the_response(self, h):
        _token(h, refresh="enc::secret-refresh")
        r = h.client.get(BASE, headers=h.headers("t1"))
        assert PLAINTEXT not in r.text and "secret-refresh" not in r.text and "encrypted_" not in r.text

    def test_listing_is_tenant_scoped(self, h):
        mine, theirs = _token(h, "t1"), _token(h, "t2")
        ids = {x["id"] for x in h.client.get(BASE, headers=h.headers("t1")).json()["tokens"]}
        assert mine in ids and theirs not in ids

    def test_filters(self, h):
        tid = _token(h)
        for q in ("mcp_server_id=srv-t1", "idp_id=idp-t1", "gateway_user_id=1"):
            ids = {x["id"] for x in h.client.get(f"{BASE}?{q}", headers=h.headers("t1")).json()["tokens"]}
            assert tid in ids, q
        ids = {x["id"] for x in h.client.get(f"{BASE}?idp_id=other", headers=h.headers("t1")).json()["tokens"]}
        assert tid not in ids


class TestRevoke:
    def test_revokes_locally_and_at_the_idp_with_no_material_left(self, h, idp_http):
        tid = _token(h, refresh="enc::r")
        r = h.client.post(f"{BASE}/{tid}/revoke", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        row = _row(h, tid)
        assert row["revoked"] is True
        assert PLAINTEXT not in row["access"]
        assert idp_http["calls"][0]["url"] == "https://t1.example/revoke"
        assert PLAINTEXT not in r.text
        s = h.Factory()
        try:
            logs = s.query(TenantAdminAuditLog).filter_by(action="oauth_token_revoke", tenant_id="t1").all()
            assert logs and all(PLAINTEXT not in str(x.details) for x in logs)
        finally:
            s.close()

    def test_an_unreachable_idp_still_revokes_locally(self, h, idp_http):
        idp_http["raises"] = httpx.ConnectError("down")
        tid = _token(h)
        r = h.client.post(f"{BASE}/{tid}/revoke", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, tid)["revoked"] is True

    def test_an_idp_refusal_still_revokes_locally(self, h, idp_http):
        idp_http["status"] = 500
        tid = _token(h)
        assert h.client.post(f"{BASE}/{tid}/revoke", headers=h.headers("t1")).status_code == 200
        assert _row(h, tid)["revoked"] is True

    def test_no_revocation_endpoint_still_revokes_locally(self, h, idp_http):
        tid = _token(h, idp="idp-off-t1")
        assert h.client.post(f"{BASE}/{tid}/revoke", headers=h.headers("t1")).status_code == 200
        assert _row(h, tid)["revoked"] is True
        assert idp_http["calls"] == []

    def test_an_undecryptable_token_still_revokes_locally(self, h, idp_http):
        tid = _token(h, access="undecryptable")
        assert h.client.post(f"{BASE}/{tid}/revoke", headers=h.headers("t1")).status_code == 200
        assert _row(h, tid)["revoked"] is True
        assert idp_http["calls"] == []

    def test_another_tenants_token_looks_absent_and_stays_alive(self, h, idp_http):
        tid = _token(h, "t2")
        assert h.client.post(f"{BASE}/{tid}/revoke", headers=h.headers("t1")).status_code == 404
        assert _row(h, tid)["revoked"] is False
        assert idp_http["calls"] == []

    def test_read_permission_cannot_revoke(self, h, idp_http):
        tid = _token(h)
        ro = h.limited_headers("t1", [Permission.OAUTH_CLIENT_READ])
        assert h.client.get(BASE, headers=ro).status_code == 200
        assert h.client.post(f"{BASE}/{tid}/revoke", headers=ro).status_code == 403
        assert h.client.post(f"{BASE}/revoke-bulk", json={}, headers=ro).status_code == 403
        assert _row(h, tid)["revoked"] is False


class TestBulkRevoke:
    def test_only_this_tenant_and_only_the_filtered_set(self, h, idp_http):
        target = _token(h, idp="idp-t1")
        spared = _token(h, idp="idp-off-t1")
        foreign = _token(h, "t2")
        r = h.client.post(f"{BASE}/revoke-bulk", json={"idp_id": "idp-t1"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, target)["revoked"] is True
        assert _row(h, spared)["revoked"] is False
        assert _row(h, foreign)["revoked"] is False

    def test_an_empty_filter_means_every_token_of_this_tenant_only(self, h, idp_http):
        mine, foreign = _token(h), _token(h, "t2")
        r = h.client.post(f"{BASE}/revoke-bulk", json={}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, mine)["revoked"] is True
        assert _row(h, foreign)["revoked"] is False
        assert "t2.example" not in {c["url"].split("/")[2] for c in idp_http["calls"]}

    def test_status_filter_narrows_it(self, h, idp_http):
        live, stale = _token(h, hours=5), _token(h, hours=-5)
        r = h.client.post(f"{BASE}/revoke-bulk", json={"status_filter": "expired_no_refresh"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, stale)["revoked"] is True
        assert _row(h, live)["revoked"] is False

    def test_a_bad_status_filter_revokes_nothing(self, h, idp_http):
        tid = _token(h)
        assert h.client.post(f"{BASE}/revoke-bulk", json={"status_filter": "x"}, headers=h.headers("t1")).status_code == 400
        assert _row(h, tid)["revoked"] is False

    def test_filters_by_server_and_user(self, h, idp_http):
        tid = _token(h)
        for body in ({"mcp_server_id": "nope"}, {"gateway_user_id": "nope"}):
            assert h.client.post(f"{BASE}/revoke-bulk", json=body, headers=h.headers("t1")).status_code == 200
            assert _row(h, tid)["revoked"] is False
