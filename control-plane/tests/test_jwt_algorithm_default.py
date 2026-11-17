# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`JWT_ALGORITHM` has one default, read in one place.

The token issuer, the tenant middleware and the log-tail socket defaulted the
variable to HS256; the administrator sign-in path
(`app.core.admin_user.dependencies.get_auth_service`) did not, and raised
when it was unset. The process started, and every sign-in answered 500.

Now `app.core.jwt_settings.jwt_algorithm()` is the only read: unset or blank
is HS256 everywhere, and a value the shared secret cannot sign with (anything
but HS256, HS384 or HS512) stops the application from being built, so there
is no state in which the process runs and can sign nobody in.
"""

from __future__ import annotations

import ast

import pytest

from tests.app_factory import create_app
from tests.edition_boundary import REPO
from tests.source_trees import python_sources

THE_ONE_READER = "app/core/jwt_settings.py"


@pytest.fixture()
def fresh_auth_service(monkeypatch):
    """`get_auth_service` builds its service once per process; build it anew."""
    from app.core.admin_user import dependencies

    monkeypatch.setattr(dependencies, "_auth_service", None)
    return dependencies


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_blank_the_sign_in_path_uses_the_issuers_default(fresh_auth_service, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("JWT_ALGORITHM", raising=False)
    else:
        monkeypatch.setenv("JWT_ALGORITHM", value)
    from app.core.jwt_settings import DEFAULT_JWT_ALGORITHM, jwt_algorithm
    from app.services.config_manager import ConfigManager

    service = fresh_auth_service.get_auth_service()
    assert service.jwt_algorithm == DEFAULT_JWT_ALGORITHM == "HS256"
    # The same answer as the token issuer's configuration.
    assert ConfigManager(validate=False).get_jwt_algorithm() == jwt_algorithm() == "HS256"


@pytest.mark.parametrize("value", ["HS256", "HS384", "HS512"])
def test_a_supported_algorithm_is_used_as_set(fresh_auth_service, monkeypatch, value):
    monkeypatch.setenv("JWT_ALGORITHM", value)
    assert fresh_auth_service.get_auth_service().jwt_algorithm == value


@pytest.mark.parametrize("value", ["none", "RS256", "HS999", "hs256"])
def test_an_algorithm_the_secret_cannot_sign_with_stops_the_application_being_built(monkeypatch, value):
    monkeypatch.setenv("JWT_ALGORITHM", value)
    with pytest.raises(ValueError, match="JWT_ALGORITHM"):
        create_app()


def test_the_application_is_built_with_the_variable_unset(monkeypatch):
    monkeypatch.delenv("JWT_ALGORITHM", raising=False)
    assert create_app() is not None


def _reads_of_the_variable(tree: ast.AST) -> list:
    """Lines that reach the variable: its name as a string, or its
    definition (`JWT_ALGORITHM` of `app/env_vars.py`) by name."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "JWT_ALGORITHM":
            lines.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "JWT_ALGORITHM":
            lines.append(node.lineno)
        if isinstance(node, ast.Name) and node.id == "JWT_ALGORITHM" and isinstance(node.ctx, ast.Load):
            lines.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and any(alias.name == "JWT_ALGORITHM" for alias in node.names):
            lines.append(node.lineno)
    return lines


def test_the_variable_is_read_in_one_place():
    readers = {}
    for path in python_sources(REPO / "app"):
        label = str(path.resolve().relative_to(REPO))
        lines = _reads_of_the_variable(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            readers[label] = lines
    # The table that defines the variable is not a read of it.
    assert "app/env_vars.py" in readers
    readers.pop("app/env_vars.py")
    assert sorted(readers) == [THE_ONE_READER]
