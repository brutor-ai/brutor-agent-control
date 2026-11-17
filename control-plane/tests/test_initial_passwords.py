# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No default password: generated on first start or required; no hard-coded
fallback.

The install seed used to create the tenant administrator with `Admin123!`
when `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` was not set, and a second account,
`<username>-readonly`, with the same password. Now:

* a first start with the variable unset or blank refuses to start, naming the
  variable, and creates no administrator;
* a start that has no account to create asks for nothing;
* the Community Edition seeds one account, the administrator;
* no production source holds a password as the default of an environment
  read or of a request field.

Refusing was chosen over generating one and printing it: the control plane
runs as several workers and replicas that all seed at start, its log is
shipped and retained, and a password that was printed once is in every copy
of that log; a refusal has nothing to leak and nothing to find later. The
Community package writes a generated password into its `.env` before the
first start, which is the same guarantee without the log line.

The Enterprise Edition's own accounts (system administrator, trial portal
user) are held in `enterprise/tests/test_initial_passwords.py`.
"""

from __future__ import annotations

import ast
import os
from unittest import mock

import pytest

from app.core.initial_credentials import InitialPasswordMissing, initial_password
from tests import seed_recorder
from tests.edition_boundary import REPO
from tests.source_trees import python_sources

PASSWORD_VARIABLES = ("BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD", "BRUTOR_SYSTEM_ADMIN_PASSWORD", "BRUTOR_TRIAL_USER_PASSWORD")

# The passwords that used to be built in. A production source may hold them
# only where listed, with the reason.
FORMER_DEFAULTS = ("Admin123!", "Trial123!")
MAY_NAME_A_FORMER_DEFAULT: dict = {}
# Spelled in pieces, the way one was hidden from this scan before.
FORMER_DEFAULT_PIECES = ('"Admin" + "123!"',)


def _sources():
    files = list(python_sources(REPO / "app"))
    files += [path for path in (REPO / "main.py", REPO / "gunicorn.conf.py") if path.is_file()]
    enterprise = REPO / "enterprise"
    if enterprise.is_dir():
        files += [path for path in enterprise.glob("*.py")]
        files += list(python_sources(enterprise / "brutor_enterprise"))
    return sorted(set(files))


def source_label(path) -> str:
    return str(path.resolve().relative_to(REPO))


def _string(node: ast.AST):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _secret_name(name) -> bool:
    return isinstance(name, str) and any(word in name.lower() for word in ("password", "passwd"))


def defaults_for_a_password(tree: ast.AST) -> list:
    """`(line, name, default)` for every read `x.get(NAME, DEFAULT)` /
    `os.getenv(NAME, DEFAULT)` / `os.environ.get(NAME, DEFAULT)` whose NAME
    names a password and whose DEFAULT is a non-empty string."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        function = node.func
        called = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if called not in ("get", "getenv"):
            continue
        name, default = _string(node.args[0]), _string(node.args[1])
        if _secret_name(name) and default:
            found.append((node.lineno, name, default))
    return found


class TestNoPasswordIsBuiltIn:
    def test_the_scan_reads_both_trees(self):
        labels = {source_label(path) for path in _sources()}
        assert "app/composition/seeding.py" in labels
        assert len(labels) > 600

    def test_no_read_of_a_password_has_a_default(self):
        offenders = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders += [f"{source_label(path)}:{line} {name}" for line, name, _ in defaults_for_a_password(tree)]
        assert offenders == []

    def test_no_source_names_a_former_default(self):
        offenders = []
        for path in _sources():
            label = source_label(path)
            if label in MAY_NAME_A_FORMER_DEFAULT:
                continue
            text = path.read_text(encoding="utf-8")
            offenders += [f"{label}: {word}" for word in (*FORMER_DEFAULTS, *FORMER_DEFAULT_PIECES) if word in text]
        assert offenders == []

    def test_every_exception_still_names_one(self):
        for label in MAY_NAME_A_FORMER_DEFAULT:
            text = (REPO / label).read_text(encoding="utf-8")
            assert any(word in text for word in FORMER_DEFAULTS), f"{label} no longer needs its entry"

    def test_the_scan_notices_a_default(self):
        tree = ast.parse(
            "import os\n"
            "a = os.getenv('X_PASSWORD', 'hunter2')\n"
            "b = os.environ.get('ADMIN_PASSWORD', 'x')\n"
            "c = body.get('password', 'y')\n"
            "ok1 = os.getenv('X_PASSWORD')\n"
            "ok2 = os.getenv('X_PASSWORD', '')\n"
            "ok3 = os.getenv('X_USERNAME', 'admin')\n"
        )
        assert [(line, name) for line, name, _ in defaults_for_a_password(tree)] == [
            (2, "X_PASSWORD"), (3, "ADMIN_PASSWORD"), (4, "password"),
        ]


class TestInitialPassword:
    """`initial_password` takes the variable's definition: the password is
    read through the registry, as every setting is."""

    VARIABLE = "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD"

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_unset_or_blank_is_refused_and_the_message_names_the_variable(self, value):
        from app.env_vars import BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD

        env = {} if value is None else {self.VARIABLE: value}
        with mock.patch.dict(os.environ, env):
            if value is None:
                os.environ.pop(self.VARIABLE, None)
            with pytest.raises(InitialPasswordMissing) as refused:
                initial_password(BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD, "the first administrator")
        message = str(refused.value)
        assert self.VARIABLE in message and "the first administrator" in message
        assert "no default" in message

    def test_a_set_value_is_returned_as_it_is(self):
        from app.env_vars import BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD

        with mock.patch.dict(os.environ, {self.VARIABLE: " s3cret with spaces "}):
            assert initial_password(BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD, "x") == " s3cret with spaces "

    def test_no_password_variable_has_a_default(self):
        """A password is a secret, and a secret has no default: the registry
        refuses a definition that carries one (`View.defects`)."""
        from app import env_vars  # noqa: F401 - the table registers itself
        from app.env_registry import REGISTRY

        passwords = [var for var in REGISTRY.view(enterprise=True).vars if "PASSWORD" in var.name and "_SECRET_ARN" not in var.name and "ROTATION" not in var.name]
        assert self.VARIABLE in [var.name for var in passwords]
        for var in passwords:
            assert var.secret and var.default is None, var.name


def _seed_with(env: dict):
    """`seed_initial_data()` on the recorder's private database, with `env`
    over the recorder's pinned environment (None removes a variable)."""
    import app.database as database

    for name, value in env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    database.seed_initial_data()


def _admins(factory) -> list:
    from app.database.models.admin import TenantAdmin

    db = factory()
    try:
        return sorted(row.username for row in db.query(TenantAdmin).all())
    finally:
        db.close()


class TestFirstStart:
    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_without_the_administrators_password_it_refuses_and_creates_no_account(self, value):
        with seed_recorder._private_database() as (_engine, factory):
            with pytest.raises(InitialPasswordMissing, match="BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD"):
                _seed_with({"BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": value})
            assert _admins(factory) == []
            # Refused before the first step: not even the default tenant.
            assert seed_recorder._tables(_engine) == {}

    def test_a_refused_start_can_be_repeated_with_the_password_set(self):
        with seed_recorder._private_database() as (_engine, factory):
            with pytest.raises(InitialPasswordMissing):
                _seed_with({"BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": None})
            _seed_with({"BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": "chosen-by-the-operator"})
            from app.database.models.admin import TenantAdmin
            import bcrypt

            db = factory()
            try:
                admin = db.query(TenantAdmin).filter(TenantAdmin.username == "admin").one()
                assert bcrypt.checkpw(b"chosen-by-the-operator", admin.password_hash.encode("utf-8"))
                for former in FORMER_DEFAULTS:
                    assert not bcrypt.checkpw(former.encode("utf-8"), admin.password_hash.encode("utf-8"))
            finally:
                db.close()

    def test_a_restart_asks_for_no_password(self):
        """Nothing is created on a restart, so nothing is required: an
        operator may take the variable out of the environment after the
        first start."""
        with seed_recorder._private_database() as (_engine, factory):
            _seed_with({})
            before = _admins(factory)
            _seed_with({name: None for name in PASSWORD_VARIABLES})
            assert _admins(factory) == before


class TestTheCommunitySeedCreatesOneAccount:
    """No `<username>-readonly` account,
    which carried the administrator's password."""

    def test_the_recorded_community_install_has_one_tenant_administrator(self):
        import json

        recorded = json.loads((REPO / "tests" / "fixtures" / "seed_recording_community.json").read_text("utf-8"))
        rows = recorded["install"]["rows"]["tenant_admins"]["added"]
        assert [row["username"] for row in rows] == ["admin"]

    def test_the_community_seed_has_no_read_only_step(self):
        from app.composition.seeding import INSTALL_SEED

        assert "read_only_admin" not in [step.name for step in INSTALL_SEED]
        import app.composition.seeding as seeding

        assert "-readonly" not in (REPO / "app" / "composition" / "seeding.py").read_text("utf-8")
        assert not hasattr(seeding, "_read_only_admin")
