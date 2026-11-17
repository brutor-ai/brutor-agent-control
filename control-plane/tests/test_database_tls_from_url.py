# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Control Plane's TLS towards PostgreSQL is what `DATABASE_URL` says.

SQLAlchemy hands `connect_args` to the driver after the URL's query, so a
`sslmode` passed there would replace the URL's (`?sslmode=verify-full`
became `prefer`). TLS is set in the URL only, with libpq's own parameters,
as for the Core Gateway:

* nothing in `connect_args` names a TLS parameter: the URL's `sslmode`,
  `sslcert`, `sslkey`, `sslrootcert` reach libpq as written, and a URL that
  states none gets libpq's own default;
* the statement timeout is added to the URL's own `options`, never put in
  their place, and the URL's `connect_timeout` wins over the fixed one.
"""

from __future__ import annotations

import pytest

from app.database.database_config import DatabaseConfig

BASE = "postgresql+psycopg2://u:p@db.example:5432/brutor"


def _args(url: str, **over) -> dict:
    return DatabaseConfig(url=url, **over).get_engine_config().get("connect_args", {})


def test_the_urls_sslmode_is_not_replaced():
    args = _args(f"{BASE}?sslmode=verify-full&sslrootcert=/etc/ca.pem")
    assert "sslmode" not in args
    assert "sslrootcert" not in args


@pytest.mark.parametrize("query", ["", "?sslmode=disable", "?sslmode=require&sslcert=/c.pem&sslkey=/k.pem"])
def test_no_tls_parameter_is_ever_added(query):
    args = _args(f"{BASE}{query}")
    assert not {"sslmode", "sslcert", "sslkey", "sslrootcert"} & set(args)


def test_the_urls_connect_timeout_wins():
    assert _args(BASE)["connect_timeout"] == 10
    assert "connect_timeout" not in _args(f"{BASE}?connect_timeout=3")


def test_the_statement_timeout_joins_the_urls_options():
    args = _args(f"{BASE}?options=-c%20search_path%3Dbrutor", command_timeout=60)
    assert args["options"] == "-c search_path=brutor -c statement_timeout=60000"


def test_without_url_options_the_timeout_is_the_options():
    assert _args(BASE, command_timeout=60)["options"] == "-c statement_timeout=60000"


def test_the_driver_receives_what_the_url_says():
    """End to end through SQLAlchemy and psycopg2's DSN: the URL's
    `sslmode` reaches libpq, `disable` included, and none is added."""
    from sqlalchemy import create_engine

    def dsn_mode(url: str, **over) -> str:
        config = DatabaseConfig(url=url, **over).get_engine_config()
        engine = create_engine(url, connect_args=config.get("connect_args", {}))
        cargs, cparams = engine.dialect.create_connect_args(engine.url)
        merged = {**cparams, **config.get("connect_args", {})}
        engine.dispose()
        return merged.get("sslmode")

    assert dsn_mode(f"{BASE}?sslmode=verify-full") == "verify-full"
    assert dsn_mode(f"{BASE}?sslmode=disable") == "disable"
    assert dsn_mode(BASE) is None
