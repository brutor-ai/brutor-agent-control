# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The seed registries: what a starting control plane writes into its database
(`INSTALL_SEED`, `app/composition/seeding.py`) and what provisioning a tenant
creates (`TENANT_PROVISIONING`, `app/composition/provisioning.py`), as ordered
lists of named steps. `_seed_initial_data` and
`TenantProvisioner._provision_tenant` run the lists and hold no step
themselves.

Why it matters: the lists are the seam another distribution (the Enterprise
Edition) extends. A step written into either function instead would be invisible to it.
And the order is behaviour: the default tenant before its provisioning, the
resource groups before the content bound to them, the models before the
defaults picked from them.

Every collaborator of a step is replaced here, so nothing touches a database.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional
from unittest import mock

import pytest

import app.database as database
from app.composition import provisioning, seeding
from app.composition.provisioning import (
    TENANT_PROVISIONING,
    ProvisioningContext,
    ProvisioningStep,
    run_provisioning_steps,
)
from app.composition.features import (
    ENTERPRISE_FEATURES,
    MCP_REGISTRY,
    MULTI_TENANT,
    POLICY_AS_CODE,
    PORTAL,
    PROMPT_CACHE,
    RESPONSE_CACHE,
)
from app.composition.extensions import PROVISIONING, SEEDING
from app.composition.seeding import INSTALL_SEED, SeedContext, SeedStep, run_seed_steps
from tests.app_factory import edition_under_test

ENTERPRISE = edition_under_test() == "enterprise"
from app.constants.tenant import TenantDefaultRole
from app.core.admin_user.permissions import Permission
from app.services.tenant.tenant_provisioning import TenantProvisioner

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------------------
# The lists.
# ---------------------------------------------------------------------------


def test_install_seed_steps_are_these_in_this_order():
    # Recorded from `_seed_initial_data` as it was when the steps were written
    # inline. Changing the order is a behaviour change; make it on purpose.
    # The built-in list is the Community Edition's; the Enterprise steps are
    # the extension's and run after it.
    assert [(step.name, step.feature) for step in INSTALL_SEED] == [
        ("default_tenant", None),
        ("provision_default_tenant", None),
        ("provisioning_warnings", None),
        ("unbound_resource_groups", None),
        ("system_health_check_key", None),
        ("role_permissions", None),
        ("iso42001_controls", None),
        ("compliance_frameworks", None),
        ("local_witness", None),
    ]


def test_tenant_provisioning_steps_are_these_in_this_order():
    # Recorded from `_provision_tenant` as it was when the steps were written
    # inline.
    assert [(step.name, step.feature) for step in TENANT_PROVISIONING] == [
        ("roles", None),
        ("tenant_admin", None),
        ("default_resource_group", None),
        ("unbound_resource_group", None),
        ("a2a_signing_key", None),
        ("system_mcp_servers", None),
        ("agent_skills", None),
        ("mcp_servers", None),
        ("guardrails", None),
        ("mcp_catalog", None),
        ("shared_mcp_servers", None),
        ("sample_agent_cards", None),
        ("llm_models", None),
        ("default_embedding_model", None),
        ("default_judge_model", None),
    ]


def _step_functions(module) -> List[str]:
    """Module-level functions that take a single `ctx`: the shape of a step."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and [a.arg for a in node.args.args] == ["ctx"]
    )


@pytest.mark.parametrize(
    "run, step_type",
    [(run_seed_steps, SeedStep), (run_provisioning_steps, ProvisioningStep)],
    ids=["install", "provisioning"],
)
def test_the_runner_runs_the_steps_in_order_with_the_context(run, step_type):
    seen: List[Any] = []
    context = object()
    run(context, [step_type("a", lambda ctx: seen.append(("a", ctx))), step_type("b", lambda ctx: seen.append(("b", ctx)))])
    assert seen == [("a", context), ("b", context)]


@pytest.mark.parametrize(
    "run, step_type",
    [(run_seed_steps, SeedStep), (run_provisioning_steps, ProvisioningStep)],
    ids=["install", "provisioning"],
)
def test_a_step_that_raises_stops_the_run(run, step_type):
    seen: List[str] = []

    def boom(ctx):
        raise RuntimeError("no")

    with pytest.raises(RuntimeError, match="no"):
        run(None, [step_type("a", lambda ctx: seen.append("a")), step_type("b", boom), step_type("c", lambda ctx: seen.append("c"))])
    assert seen == ["a"]


# ---------------------------------------------------------------------------
# The install seed, run for real with every collaborator replaced.
# ---------------------------------------------------------------------------

# The helpers the install seed calls: (where the step looks it up, short name).
# The Community steps first, then the Enterprise extension's (L3).
COMMUNITY_INSTALL_HELPERS = [
    ("app.database.ensure_unbound_resource_groups", "ensure_unbound_resource_groups"),
    ("app.database._seed_system_health_check_key", "_seed_system_health_check_key"),
    ("app.database.sync_role_permissions", "sync_role_permissions"),
    ("app.database.seed_iso42001_controls.seed_iso42001_controls", "seed_iso42001_controls"),
    ("app.database.seed_compliance_frameworks.seed_compliance_frameworks", "seed_compliance_frameworks"),
    ("app.database.seed_local_witness.seed_local_witness", "seed_local_witness"),
]
REQUIRED_INSTALL = ["get_or_create_default_tenant", "provision_tenant", "sync_role_permissions"]


# ---------------------------------------------------------------------------
# Tenant provisioning, run for real with every collaborator replaced.
# ---------------------------------------------------------------------------

REQUIRED_PROVISIONING = [
    "_seed_roles",
    "_create_tenant_admin",
    "_create_default_resource_group",
    "_create_unbound_resource_group",
]
ROLE_IDS = {role.value: f"role-{role.name.lower()}" for role in TenantDefaultRole}


# ---------------------------------------------------------------------------
# The two functions only run their list.
# ---------------------------------------------------------------------------


def _function(source: str, name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef) and node.name == name)


def _calls(function: ast.FunctionDef) -> List[str]:
    """Names called inside the function, as written (`a.b.c`)."""
    return sorted({ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)})


def _imports(function: ast.FunctionDef) -> List[str]:
    """Modules imported inside the function."""
    found = []
    for node in ast.walk(function):
        if isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return sorted(found)


def test_the_install_seed_function_only_runs_its_list():
    function = _function((APP_DIR / "database" / "__init__.py").read_text(encoding="utf-8"), "_seed_initial_data")
    assert _calls(function) == [
        "SEEDING.steps",
        "SeedContext",
        "SessionLocal",
        "db.close",
        "db.rollback",
        "logger.error",
        "run_seed_steps",
        "str",
    ]
    # A step added to the function would need an import there.
    assert _imports(function) == ["app.composition.extensions", "app.composition.seeding"]
    # SEEDING is INSTALL_SEED plus what extensions registered.
    assert "run_seed_steps(SeedContext(db=db, logger=logger), SEEDING.steps())" in ast.unparse(function)


def test_the_provisioner_only_runs_its_list():
    source = (APP_DIR / "services" / "tenant" / "tenant_provisioning.py").read_text(encoding="utf-8")
    function = _function(source, "_provision_tenant")
    assert _calls(function) == ["PROVISIONING.steps", "ProvisioningContext", "ProvisioningResult", "run_provisioning_steps"]
    assert _imports(function) == ["app.composition.extensions", "app.composition.provisioning"]
    run_call = next(
        node for node in ast.walk(function) if isinstance(node, ast.Call) and ast.unparse(node.func) == "run_provisioning_steps"
    )
    # PROVISIONING is TENANT_PROVISIONING plus what extensions registered.
    assert ast.unparse(run_call.args[1]) == "PROVISIONING.steps()"


def test_the_guards_above_see_a_step_written_into_the_function():
    function = _function(
        "def _seed_initial_data():\n"
        "    db = SessionLocal()\n"
        "    run_seed_steps(SeedContext(db=db, logger=logger), INSTALL_SEED)\n"
        "    from app.database.seed_x import seed_x\n"
        "    seed_x(db)\n",
        "_seed_initial_data",
    )
    assert _calls(function) == ["SeedContext", "SessionLocal", "run_seed_steps", "seed_x"]
    assert _imports(function) == ["app.database.seed_x"]
