# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Names the Control Plane no longer reads, and the machinery that served them.

* `BRUTOR_DEPLOYMENT_MODE`: `development` behaved as `self_hosted`, and
  `saas` guarded a hosted SaaS that is not planned. Gone with its start-up
  validator and the `hosted=` rules on the variables.
* `BRUTOR_SYSTEM_ADMIN_PASSWORD_SECRET_ARN`, `..._ROTATION_ECS_CLUSTER`,
  `..._ROTATION_ECS_SERVICES` and `AWS_REGION` (Enterprise): the SaaS-only
  copy of the system administrator's password into AWS Secrets Manager.
* `JWT_EXPIRES_IN_SECS`: no reader in the Control Plane (an administrator's
  session lasts 8 hours; the variable is the Core Gateway's, for Portal
  sessions).
* `BRUTOR_OUTBOUND_DENY_PRIVATE_ENABLED`: private destinations are refused
  by default, as in the Core Gateway.
* `BRUTOR_CORS_ALLOW_ALL_ENABLED`: a switch that opened CORS to every
  origin; the Core Gateway removed it too. Origins are listed
  (`BRUTOR_CORS_ORIGINS`).
"""

from __future__ import annotations

import importlib.util

import pytest

from app.env_registry import REGISTRY

GONE = (
    "BRUTOR_DEPLOYMENT_MODE",
    "BRUTOR_SYSTEM_ADMIN_PASSWORD_SECRET_ARN",
    "BRUTOR_SYSTEM_ADMIN_PASSWORD_ROTATION_ECS_CLUSTER",
    "BRUTOR_SYSTEM_ADMIN_PASSWORD_ROTATION_ECS_SERVICES",
    "AWS_REGION",
    "JWT_EXPIRES_IN_SECS",
    "BRUTOR_OUTBOUND_DENY_PRIVATE_ENABLED",
    "BRUTOR_CORS_ALLOW_ALL_ENABLED",
)


def _names() -> set:
    return {var.name for var in REGISTRY.view(REGISTRY.holds_enterprise_variables()).vars}


@pytest.mark.parametrize("name", GONE)
def test_the_name_is_not_registered(name):
    assert name not in _names()


@pytest.mark.parametrize(
    "module",
    [
        "app.constants.deployment",
        "app.services.deployment_config_validator",
        "brutor_enterprise.multi_tenant.services.sysadmin_secret_rotation",
    ],
)
def test_the_machinery_is_gone(module):
    try:
        found = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        found = None
    assert found is None


def test_no_variable_carries_hosted_rules():
    from app.env_registry import EnvVar

    assert "hosted" not in EnvVar.__dataclass_fields__


def test_the_configuration_has_no_jwt_lifetime_getter():
    from app.services.config_manager import ConfigManager

    assert not hasattr(ConfigManager, "get_jwt_expires_in")


def test_cors_has_no_allow_every_origin_switch():
    from app.services.config_manager import ConfigManager

    assert not hasattr(ConfigManager, "is_cors_allow_all")
