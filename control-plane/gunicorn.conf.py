# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Gunicorn Configuration for Brutor Agent Control Plane

The control plane is a low-traffic admin API serving a few concurrent
administrators who configure the platform (resources, resource groups,
guardrails, ...). All runtime traffic (LLM, MCP, OAuth, skills) is carried
by the Core Gateway.

This means the Gunicorn configuration should be conservative:
- 2 workers is sufficient for most deployments
- each worker has its own database pool (the gateway has its own for
  runtime traffic)
- modest resource allocation

Two settings are environment variables defined in `app/env_vars.py` and
read through their definitions: the listen address
(`BRUTOR_CONTROL_PLANE_LISTEN_ADDR`) and the number of workers
(`GUNICORN_WORKERS`); the log level is the application's
(`BRUTOR_LOG_LEVEL`). Everything else is fixed here.
"""

import importlib
import os.path
import platform
import sys

# Gunicorn reads this file before it puts the working directory, or a
# `--pythonpath`, on the module path: the packages are looked for beside
# this file and where the command line says.
_ARGUMENTS = sys.argv[1:]
_PATHS = [os.path.dirname(os.path.abspath(__file__))]
for _index, _argument in enumerate(_ARGUMENTS):
    if _argument == "--pythonpath" and _index + 1 < len(_ARGUMENTS):
        _PATHS += _ARGUMENTS[_index + 1].split(",")
    elif _argument.startswith("--pythonpath="):
        _PATHS += _argument.split("=", 1)[1].split(",")
for _path in _PATHS:
    if _path and os.path.abspath(_path) not in sys.path:
        sys.path.insert(0, os.path.abspath(_path))

from app import env_registry, env_vars as E  # noqa: E402

# The environment is checked before anything of it is used: a value that
# is not of its variable's type, or a required variable that is not set,
# stops Gunicorn here, one line per name and never a value. The edition is
# the application factory named on the command line: a factory of another
# package than `app` brings that package's own table of variables
# (`<package>.env_vars`), which joins the registry when it is loaded.
_FACTORY = next((argument for argument in _ARGUMENTS if ":" in argument and not argument.startswith("-")), "")
_PACKAGE = _FACTORY.split(":", 1)[0].split(".", 1)[0]
if _PACKAGE and _PACKAGE != "app":
    try:
        importlib.import_module(f"{_PACKAGE}.env_vars")
    except ModuleNotFoundError as _missing:
        if _missing.name not in (_PACKAGE, f"{_PACKAGE}.env_vars"):
            raise
ENTERPRISE = env_registry.REGISTRY.holds_enterprise_variables()

env_registry.refuse_or_exit(ENTERPRISE, missing=True)

# =============================================================================
# Server Socket
# =============================================================================

# One listen address, BRUTOR_CONTROL_PLANE_LISTEN_ADDR (host:port; an IPv6
# host in brackets). Gunicorn takes it as written.
bind = E.BRUTOR_CONTROL_PLANE_LISTEN_ADDR.require()
backlog = 256

# =============================================================================
# Worker Processes
# =============================================================================

# Worker class - MUST be uvicorn for FastAPI/ASGI
worker_class = 'uvicorn.workers.UvicornWorker'

# The control plane handles only admin configuration requests (low traffic).
# 2 workers is sufficient for most deployments. GUNICORN_WORKERS is the one
# Gunicorn setting an operator sets; everything below is fixed.
workers = E.GUNICORN_WORKERS.integer()

# =============================================================================
# Worker Lifecycle
# =============================================================================

# Restart workers after this many requests (prevents memory leaks), with
# jitter so that they do not restart together.
max_requests = 5000
max_requests_jitter = 500

# Request timeout. Covers the slowest realistic admin request (an image
# pull during an MCP catalog import takes 60-180s on a first run) while
# still bounding a hung one.
timeout = 300
graceful_timeout = 30
keepalive = 5

# =============================================================================
# Application Loading
# =============================================================================

# Never preload: the application in the master would open database
# sockets that the forked workers then share (see `when_ready`).
preload_app = False

# =============================================================================
# Logging
# =============================================================================

# Standard output and error. Gunicorn's own messages follow the
# application's level (BRUTOR_LOG_LEVEL); its format follows
# BRUTOR_LOG_JSON_ENABLED in the application (`app/app.py`).
accesslog = '-'
errorlog = '-'
loglevel = E.BRUTOR_LOG_LEVEL.require().lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

proc_name = 'brutor-gateway-control'

# Worker heartbeat files: shared memory on Linux.
worker_tmp_dir = '/dev/shm' if platform.system() == 'Linux' and os.path.isdir('/dev/shm') else '/tmp'

# No TLS here: it is terminated in front of the Control Plane (a reverse
# proxy or an ingress).

# Gunicorn and its Uvicorn workers believe no forwarded header themselves
# (FORWARDED_ALLOW_IPS has no effect): the application judges them, by
# BRUTOR_TRUSTED_PROXIES (app/middleware/trusted_proxies.py).
forwarded_allow_ips = ""

# =============================================================================
# Server Hooks
# =============================================================================

def on_starting(server):
    """Called just before the master process is initialized"""
    # What the engine of each worker is built with (`app/database/database_config.py`).
    # Each worker's overflow equals its pool size.
    total = workers * 2 * E.BRUTOR_CONTROL_PLANE_DB_POOL_SIZE.integer()

    server.log.info(f"Starting Brutor Agent Control Plane (Gunicorn)")
    server.log.info(f"  {workers} workers on {bind}, timeout {timeout}s, {total} max DB connections")


def when_ready(server):
    """Called just after the server is started.

    Intentionally does NOT call init_database() here. Calling DB init in
    the master opens psycopg2 sockets that children then inherit
    verbatim via fork(), so both workers end up writing to the same TCP
    connection — postgres logs "already in progress" / "no transaction
    in progress" and SQLAlchemy sees corrupted cursor state
    (IndexError / ResourceClosedError during row population).

    Each worker's FastAPI lifespan runs init_database() in its own
    process after fork; the PostgreSQL advisory lock serialises the
    body of init_database across workers so seed_initial_data still
    runs exactly once to completion before other workers proceed.
    """
    server.log.info("Brutor Agent Control Plane is ready")


def worker_int(worker):
    """Called when a worker receives SIGINT/SIGQUIT"""
    worker.log.info(f"Worker {worker.pid} received SIGINT/SIGQUIT")


def worker_abort(worker):
    """Called when a worker receives SIGABRT (timeout exceeded)"""
    worker.log.warning(f"Worker {worker.pid} received SIGABRT (timeout exceeded)")


def post_fork(server, worker):
    """Called just after a worker has been forked.

    Dispose the SQLAlchemy engine's pool immediately after fork. Any
    connection that existed in the parent (e.g. opened by DATABASE_URL
    introspection or by early `create_engine` eagerness) is a shared
    OS file descriptor across parent and all children — using it from
    multiple processes corrupts cursor state. `engine.dispose()`
    abandons the inherited pool; the next `engine.connect()` in this
    worker opens a fresh connection that this worker alone owns.

    Safe no-op on a cold engine that never opened connections — dispose
    just empties an already-empty pool.
    """
    try:
        from app.database import engine
        engine.dispose()
    except Exception as e:
        # Don't block worker startup on a dispose glitch — the worst
        # case is the first query re-hits the bad pool and errors out,
        # which is the status quo today. Log and move on.
        server.log.warning(
            f"Worker {worker.pid}: engine.dispose() after fork failed: {e}"
        )
    server.log.info(f"Worker {worker.pid} spawned")


def child_exit(server, worker):
    """Called just after a worker has been exited"""
    server.log.info(f"Worker {worker.pid} exited")


def on_exit(server):
    """Called just before exiting Gunicorn"""
    server.log.info("Brutor Agent Control Plane shutting down")
