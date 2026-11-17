# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Enterprise Edition's variables are read by the Enterprise Edition.

`BRUTOR_SYSTEM_ADMIN_USERNAME`, `BRUTOR_SYSTEM_ADMIN_PASSWORD`, `BRUTOR_SYSTEM_ADMIN_EMAIL` and the
`SYSADMIN_PASSWORD_*` rotation variables belong to the system administrator,
who exists only in the Enterprise Edition. The Community package named three
of them (an `EnvVarNames` member, the stock-password warning, two getters
nothing called). `MULTI_TENANCY_ENABLED` and `DEFAULT_TENANT_ID` were
`EnvVarNames` members no code read: deleted, not moved.
"""

from __future__ import annotations

import pytest

import re

from tests.source_trees import REPO

NAMES = re.compile(
    r"SYSTEM_ADMIN_(?:USERNAME|PASSWORD|EMAIL)|SYSADMIN_PASSWORD_|MULTI_TENANCY_ENABLED|\bDEFAULT_TENANT_ID\b"
)


def test_no_community_module_names_an_enterprise_variable():
    found = sorted(
        f"{path.relative_to(REPO).as_posix()}:{number}"
        for path in (REPO / "app").rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if NAMES.search(line)
    )
    assert found == []


def test_the_configuration_has_no_system_administrator_getter():
    from app.services.config_manager import ConfigManager

    assert not hasattr(ConfigManager, "get_system_admin_username")
    assert not hasattr(ConfigManager, "get_system_admin_email")


def test_the_stock_password_refuses_the_start_for_the_tenant_administrator(monkeypatch):
    """Refused, not warned about: a configured first password that is
    the former stock one stops the start like a short one does."""
    from app import env_vars
    from app.core.initial_credentials import configured_password
    from app.core.password_rule import PasswordRefused

    monkeypatch.setenv("BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD", "Admin" + "123!")
    with pytest.raises(PasswordRefused, match="BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD"):
        configured_password(env_vars.BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD)
