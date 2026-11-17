# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a214_admin_locked_until — `locked_until` on `tenant_admins` and `system_admins`.

The account lock that failed sign-ins set had no end and nothing unlocked it.
The column holds the moment the lock ends (`app/core/admin_user/lockout.py`;
behaviour: `tests/test_sign_in_lockout.py`). Nullable; no data is migrated.
"""

from __future__ import annotations


import pytest
import sqlalchemy as sa

from app.database.models.admin import SystemAdmin, TenantAdmin

MODELS = {"tenant_admins": TenantAdmin, "system_admins": SystemAdmin}


@pytest.mark.parametrize("table", sorted(MODELS))
def test_the_column(table):
    column = MODELS[table].__table__.columns["locked_until"]
    assert column.nullable is True
    assert isinstance(column.type, sa.DateTime) and column.type.timezone is True
