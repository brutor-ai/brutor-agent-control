# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`acting_tenant_id`: the tenant a request acts on, for a route that takes it
from whoever called.

The routes that read `admin.tenant_id` answered 500 to an administrator whose
row has no such attribute (the Enterprise Edition's system administrator;
its half is `enterprise/tests/test_sysadmin_names_no_tenant.py`). What
the function answers without one registered is held here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.admin_user.dependencies import acting_tenant_id
from app.database.models.admin import TenantAdmin
from tests.unknown_principal import robot_admin, robot_registered


def _request(**query):
    return SimpleNamespace(query_params=query)


def test_an_administrator_of_a_tenant_acts_on_it():
    assert acting_tenant_id(_request(), TenantAdmin(tenant_id="t1")) == "t1"
    assert acting_tenant_id(_request(), SimpleNamespace(tenant_id="t1")) == "t1"
    # These routes never look at the tenant a tenant administrator names.
    assert acting_tenant_id(_request(tenant_id="t2"), TenantAdmin(tenant_id="t1")) == "t1"


def test_a_tenant_administrator_without_a_tenant_acts_on_none():
    for named in ({}, {"tenant_id": "t1"}):
        with pytest.raises(HTTPException) as e:
            acting_tenant_id(_request(**named), TenantAdmin(tenant_id=None))
        assert (e.value.status_code, e.value.detail) == (403, "Administrator has no tenant")


@pytest.mark.parametrize("admin", [SimpleNamespace(), SimpleNamespace(tenant_id=None), SimpleNamespace(tenant_id=""), None])
def test_an_object_no_principal_owns_acts_on_no_tenant(admin):
    for named in ({}, {"tenant_id": "t1"}):
        with pytest.raises(HTTPException) as e:
            acting_tenant_id(_request(**named), admin)
        assert (e.value.status_code, e.value.detail) == (403, "Unknown administrator type")


def test_a_registered_principal_without_a_tenant_is_not_handed_the_one_it_names():
    with robot_registered():
        with pytest.raises(HTTPException) as e:
            acting_tenant_id(_request(tenant_id="t1"), robot_admin())
        assert e.value.status_code == 403
