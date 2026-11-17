# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The DATABASE_URL must resolve to a PostgreSQL driver that is installed.

Regression (0.11.12, 2026-10-05): the trial bundle's driverless
``postgresql://`` URL let SQLAlchemy choose the DBAPI. An unpinned rebuild
pulled SQLAlchemy 2.1, whose default for ``postgresql://`` is psycopg (v3);
the image ships psycopg2 only, so every gunicorn worker died on import
(``No module named 'psycopg'``) and the control plane never came up.

The guard is on the class of bug, not the version: whatever SQLAlchemy is
installed, the URL the app builds must name a driver whose module imports.
"""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy.engine import make_url

from app.database.database_config import (
    POSTGRES_DRIVER,
    load_database_config,
    normalize_database_url,
)

TRIAL_BUNDLE_URL = "postgresql://brutor:brutor123@postgres:5432/ai_gateway"


def test_driverless_postgres_url_is_pinned_to_the_shipped_driver():
    assert normalize_database_url(TRIAL_BUNDLE_URL) == (
        f"postgresql+{POSTGRES_DRIVER}://brutor:brutor123@postgres:5432/ai_gateway"
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@h/d",
        "postgresql+psycopg2://u:p@h/d",
        "postgresql+asyncpg://u:p@h/d",
        "sqlite:///:memory:",
    ],
)
def test_url_that_names_a_driver_is_left_alone(url):
    assert normalize_database_url(url) == url


def test_password_and_query_survive_untouched():
    url = "postgresql://u:p%40ss%25w@h:5432/d?sslmode=require"
    assert normalize_database_url(url) == "postgresql+psycopg2://u:p%40ss%25w@h:5432/d?sslmode=require"


def test_app_config_resolves_to_an_installed_dbapi(monkeypatch):
    """What create_engine will import for the app's URL is actually installed."""
    monkeypatch.setenv("DATABASE_URL", TRIAL_BUNDLE_URL)
    url = make_url(load_database_config().url)
    dialect = url.get_dialect()
    assert dialect.driver == POSTGRES_DRIVER
    dialect.import_dbapi()  # raises ModuleNotFoundError when the driver is missing


def test_shipped_driver_is_a_declared_dependency():
    importlib.import_module(POSTGRES_DRIVER)
