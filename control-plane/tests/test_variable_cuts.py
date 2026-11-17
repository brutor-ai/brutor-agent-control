# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Control Plane's operator interface after the variable audit (round C2,
`.scratch/envaudit/env-audit.md`, owner decisions 5, 6 and 14).

- Gunicorn: one listen address (`BRUTOR_CONTROL_PLANE_LISTEN_ADDR`, which
  replaces HOST, PORT and GUNICORN_BIND) and `GUNICORN_WORKERS`; every other
  Gunicorn setting is fixed in `gunicorn.conf.py`, its log level follows
  `BRUTOR_LOG_LEVEL`, and there is no built-in TLS (decision 14).
- The database engine: `DATABASE_URL` carries TLS (libpq's own
  parameters), the pool is fixed in code except its size, and SQL echo is a
  development option of `python main.py`, not a variable.
- One embedding-model name, `BRUTOR_EMBEDDING_MODEL` (decision 6).
- No retired-name machinery (decision 5): a name the service no longer
  reads is not refused, because no such name was ever public.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import env_registry, env_vars
from app.env_registry import BOTH, REGISTRY, EnvVar
from tests import entry_points

REPO = Path(__file__).resolve().parents[1]
COMMUNITY = REGISTRY.view(enterprise=False)

GONE = (
    # Gunicorn: the listen address replaces three; the rest is fixed in code.
    "HOST", "PORT", "GUNICORN_BIND", "GUNICORN_BACKLOG", "GUNICORN_TIMEOUT", "GUNICORN_GRACEFUL_TIMEOUT",
    "GUNICORN_KEEPALIVE", "GUNICORN_MAX_REQUESTS", "GUNICORN_MAX_REQUESTS_JITTER", "GUNICORN_PRELOAD_APP",
    "GUNICORN_ACCESS_LOG", "GUNICORN_ERROR_LOG", "GUNICORN_LOG_LEVEL", "GUNICORN_ACCESS_LOG_FORMAT",
    "GUNICORN_PROC_NAME", "GUNICORN_WORKER_TMP_DIR", "GUNICORN_DAEMON", "GUNICORN_PIDFILE", "GUNICORN_USER",
    "GUNICORN_GROUP", "GUNICORN_UMASK", "GUNICORN_CERTFILE", "GUNICORN_KEYFILE",
    # The database engine.
    "BRUTOR_CONTROL_PLANE_DB_MAX_OVERFLOW", "BRUTOR_DB_POOL_TIMEOUT_SECS", "BRUTOR_DB_POOL_RECYCLE_SECS",
    "BRUTOR_DB_POOL_PRE_PING_ENABLED", "BRUTOR_DB_ECHO_ENABLED", "BRUTOR_DB_ECHO_POOL_ENABLED",
    "BRUTOR_DB_CONNECT_TIMEOUT_SECS", "BRUTOR_DB_SSL_MODE", "BRUTOR_DB_SSL_ROOT_CERT", "BRUTOR_DB_SSL_CERT",
    "BRUTOR_DB_SSL_KEY", "BRUTOR_DB_ISOLATION_LEVEL",
    # Other internal tuning and a duplicate.
    "BRUTOR_BULK_OP_POOL_RESERVE", "HOSTNAME", "BRUTOR_DEFAULT_EMBEDDING_MODEL",
)


def test_the_cut_variables_are_gone():
    names = {var.name for var in REGISTRY.view(enterprise=REGISTRY.holds_enterprise_variables()).vars}
    assert sorted(set(GONE) & names) == []
    for name in GONE:
        assert not hasattr(env_vars, name), name
    # What stays of Gunicorn and of the engine.
    assert {"BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "GUNICORN_WORKERS", "BRUTOR_CONTROL_PLANE_DB_POOL_SIZE",
            "BRUTOR_DB_COMMAND_TIMEOUT_SECS", "DATABASE_URL"} <= names
    assert [var.name for var in COMMUNITY.vars if var.name.startswith("GUNICORN_")] == ["GUNICORN_WORKERS"]


def test_there_is_no_retired_name_machinery(monkeypatch):
    assert not hasattr(env_registry, "RemovedVar")
    assert "retired" not in {field.name for field in dataclasses.fields(EnvVar)}
    assert set(COMMUNITY.json()) == {"schema_version", "service", "variables"}
    assert all("retired_names" not in variable for variable in COMMUNITY.json()["variables"])
    assert "Retired names" not in COMMUNITY.markdown()
    # A name this service once read is now simply not read: it does not stop the start.
    for old in ("LOG_LEVEL", "PROXY_HEALTH_CHECK_API_KEY", "REDIS_ENABLED", "VAULT_ADDR", "DB_SSL_MODE"):
        monkeypatch.setenv(old, "x")
    assert COMMUNITY.problems(missing=False) == []


def test_the_listen_address_is_one_variable(monkeypatch):
    listen = env_vars.BRUTOR_CONTROL_PLANE_LISTEN_ADDR
    assert listen.default == "0.0.0.0:5050" and listen.edition == BOTH
    for bad in ("5050", "0.0.0.0", "host:port", "0.0.0.0:0", "0.0.0.0:70000", "[::1:5050", "http://x:5050"):
        assert listen.mismatch(bad), bad
    for good in ("127.0.0.1:5050", "[::]:5050", "control-plane:8080"):
        assert listen.mismatch(good) is None, good
    from app.services.config_manager import ConfigManager

    monkeypatch.setenv("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "[::1]:5999")
    config = ConfigManager(validate=False)
    assert (config.get_host(), config.get_port()) == ("::1", 5999)


def test_gunicorn_binds_the_listen_address_and_fixes_the_rest():
    environment = {"GUNICORN_WORKERS": "3", "BRUTOR_CONTROL_PLANE_LISTEN_ADDR": "127.0.0.1:5999",
                   "BRUTOR_LOG_LEVEL": "warning"}
    probe = (
        "import runpy, sys, json\n"
        "sys.argv = ['gunicorn', 'app.app:create_app', '--config', 'gunicorn.conf.py']\n"
        "c = runpy.run_path('gunicorn.conf.py', run_name='__config__')\n"
        "print(json.dumps({k: c.get(k) for k in ('bind', 'workers', 'preload_app', 'loglevel', 'timeout', "
        "'graceful_timeout', 'backlog', 'keepalive', 'max_requests', 'max_requests_jitter', 'accesslog', "
        "'errorlog', 'proc_name', 'daemon', 'pidfile', 'user', 'group', 'certfile', 'keyfile', 'umask')}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(REPO), capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ.get("PATH", ""), **entry_points.BASE_ENVIRONMENT, **environment},
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "bind": "127.0.0.1:5999", "workers": 3, "preload_app": False, "loglevel": "warning", "timeout": 300,
        "graceful_timeout": 30, "backlog": 256, "keepalive": 5, "max_requests": 5000, "max_requests_jitter": 500,
        "accesslog": "-", "errorlog": "-", "proc_name": "brutor-gateway-control", "daemon": None, "pidfile": None,
        "user": None, "group": None, "certfile": None, "keyfile": None, "umask": None,
    }


def test_the_engine_takes_tls_from_the_url_and_fixes_the_pool(monkeypatch):
    from app.database.database_config import load_database_config

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://u:p@db.example:5432/brutor?sslmode=verify-full&sslrootcert=/etc/ssl/root.crt",
    )
    monkeypatch.setenv("BRUTOR_CONTROL_PLANE_DB_POOL_SIZE", "7")
    monkeypatch.delenv("BRUTOR_DB_COMMAND_TIMEOUT_SECS", raising=False)
    engine = load_database_config().get_engine_config()
    assert engine == {
        "pool_pre_ping": True,
        "pool_size": 7,
        "max_overflow": 7,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "connect_args": {"connect_timeout": 10, "options": "-c statement_timeout=60000"},
    }


def test_sql_echo_is_a_development_option_of_main_py():
    from click.testing import CliRunner

    from app import launcher

    names = [option.name for option in launcher.run.params]
    assert "sql_echo" in names and "host" not in names and "port" not in names and "listen_addr" in names
    assert "--sql-echo" in CliRunner().invoke(launcher.run, ["--help"]).output


def test_one_embedding_model_name_for_every_service():
    model = env_vars.BRUTOR_EMBEDDING_MODEL
    assert model.edition == BOTH and model.default is None
    assert "no default embedding model" in model.description
    source = (REPO / "app" / "composition" / "provisioning.py").read_text(encoding="utf-8")
    assert "BRUTOR_EMBEDDING_MODEL.string()" in source and "BRUTOR_DEFAULT_EMBEDDING_MODEL" not in source


def test_bulk_operations_leave_a_fixed_share_of_the_gateways_pool(monkeypatch):
    from app.services.concurrency.bulk_runner import derive_concurrency_cap

    monkeypatch.setenv("BRUTOR_GATEWAY_DB_POOL_SIZE", "50")
    assert derive_concurrency_cap() == 30


def test_the_replica_is_named_by_its_host_name():
    import socket

    from app.api.v1.endpoints.admin import system_logs_ws

    assert system_logs_ws.replica_id() == socket.gethostname()


@pytest.mark.parametrize("name", ["GUNICORN_DAEMON", "GUNICORN_CERTFILE", "HOST", "BRUTOR_DB_SSL_MODE"])
def test_a_cut_name_is_listed_as_unread_not_refused(monkeypatch, name):
    monkeypatch.setenv(name, "x")
    assert COMMUNITY.problems(missing=False) == []
