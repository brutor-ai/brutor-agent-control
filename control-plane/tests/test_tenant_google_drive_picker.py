# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Google Drive picker of the Portal is configured per tenant.

Its OAuth client id and browser API key were two variables of the Portal's
container (`BRUTOR_PORTAL_GOOGLE_CLIENT_ID`, `BRUTOR_PORTAL_GOOGLE_API_KEY`):
one Google project for every tenant of a deployment, whereas the OAuth client
is a trust relationship with one Google Workspace organisation. They are now
two columns of `tenants`, set by the tenant's administrator with
`PATCH /v1/admin/tenants/current` (audited like every other tenant change)
and read by the Enterprise Core Gateway for the signed-in Portal user.

Both values are public by design (the client id is public; the key is a
browser key, restricted by HTTP referrer and to the Picker API), so they are
stored as written, not encrypted. The two are set and cleared together.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.tenant import Tenant
from tests.admin_endpoint_harness import build_admin_harness

URL = "/v1/admin/tenants/current"
CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
API_KEY = "AIza" + "B" * 30 + "x_y-z"  # the shape of a browser key, not a key


@pytest.fixture()
def h():
    log = BrutorLogger("test-google-drive-picker")
    harness = build_admin_harness([create_tenant_settings_router(log)], logger_name="test-drive-picker")
    yield harness
    harness.close()


def _stored(h) -> tuple:
    s = h.Factory()
    try:
        tenant = s.get(Tenant, "t1")
        return tenant.portal_google_drive_client_id, tenant.portal_google_drive_api_key
    finally:
        s.close()


def _set(h, client_id, api_key):
    return h.client.patch(URL, json={"portal_google_drive_client_id": client_id,
                                     "portal_google_drive_api_key": api_key}, headers=h.headers())


def test_unset_by_default_and_readable(h):
    r = h.client.get(URL, headers=h.headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["portal_google_drive_client_id"] is None and body["portal_google_drive_api_key"] is None


def test_set_read_back_audited_and_cleared(h):
    r = _set(h, CLIENT_ID, API_KEY)
    assert r.status_code == 200, r.text
    assert (r.json()["portal_google_drive_client_id"], r.json()["portal_google_drive_api_key"]) == (CLIENT_ID, API_KEY)
    # Stored as written: public values, not encrypted.
    assert _stored(h) == (CLIENT_ID, API_KEY)
    r = h.client.get(URL, headers=h.headers())
    assert r.json()["portal_google_drive_client_id"] == CLIENT_ID
    s = h.Factory()
    try:
        record = s.query(TenantAdminAuditLog).filter_by(action="tenant_update").order_by(
            TenantAdminAuditLog.timestamp.desc()).first()
        assert record is not None and "portal_google_drive_client_id" in str(record.details)
    finally:
        s.close()
    # Empty strings clear, and so does an explicit null.
    r = _set(h, "", "")
    assert r.status_code == 200, r.text
    assert _stored(h) == (None, None)
    assert _set(h, CLIENT_ID, API_KEY).status_code == 200
    r = _set(h, None, None)
    assert r.status_code == 200, r.text
    assert _stored(h) == (None, None)


def test_one_alone_is_judged_by_what_is_stored(h):
    """A PATCH names what changes: the key alone may be rotated while the
    client id stays; one alone may not be cleared while the other stays."""
    assert _set(h, CLIENT_ID, API_KEY).status_code == 200
    rotated = "AIza" + "C" * 35
    r = h.client.patch(URL, json={"portal_google_drive_api_key": rotated}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert _stored(h) == (CLIENT_ID, rotated)
    r = h.client.patch(URL, json={"portal_google_drive_client_id": ""}, headers=h.headers())
    assert r.status_code == 422, r.text
    assert _stored(h) == (CLIENT_ID, rotated)


@pytest.mark.parametrize("body", [
    {"portal_google_drive_client_id": CLIENT_ID},
    {"portal_google_drive_api_key": API_KEY},
    {"portal_google_drive_client_id": CLIENT_ID, "portal_google_drive_api_key": ""},
    {"portal_google_drive_client_id": "my-client", "portal_google_drive_api_key": API_KEY},
    {"portal_google_drive_client_id": CLIENT_ID, "portal_google_drive_api_key": "not-a-key"},
    {"portal_google_drive_client_id": CLIENT_ID, "portal_google_drive_api_key": API_KEY + "x"},
    {"portal_google_drive_picker": {"client_id": CLIENT_ID}},
])
def test_one_without_the_other_a_wrong_shape_or_an_unknown_field_is_refused(h, body):
    r = h.client.patch(URL, json=body, headers=h.headers())
    assert r.status_code == 422, r.text
    assert _stored(h) == (None, None)
