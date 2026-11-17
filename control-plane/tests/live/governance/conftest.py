# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Fixtures for the live GOVERNANCE suite. Inherits the parent live conftest
(control_client / proxy_client / admin_headers / reachability + provider-key
guards) and adds:

  * demo provisioning (the setup scripts = the "first half"), session-scoped so
    the whole demo is stood up once per run;
  * a `log_probe` that fetches a call's audit row by its correlation id.
"""

from __future__ import annotations

import pytest

from .helpers import demo_keys, fetch_all_logs, fetch_log, run_setup


@pytest.fixture(scope="session")
def base_scaffolding() -> dict:
    """Provision the shared demo scaffolding — ACME org, Sales/Ops resource
    groups, SDK keys, and LLM models. Every governance surface builds on this,
    and it is the authoritative source of the SALES_KEY / OPS_KEY the calls use.
    """
    run_setup("setup-llm-demo.py")
    return demo_keys()


@pytest.fixture
def log_probe(control_client, admin_headers):
    """`log_probe(corr, **kw)` → the proxy-log row for that correlation id.

    Wraps `helpers.fetch_log` with the session's control client + admin token so
    a test just says `log = log_probe(call.corr)`.
    """
    def probe(corr: str, **kw):
        return fetch_log(control_client, admin_headers, corr, **kw)

    return probe


@pytest.fixture
def logs_probe(control_client, admin_headers):
    """`logs_probe(corr)` → ALL proxy-log rows for a correlation id (after a
    settle window). Backs the anti-double-logging regression assertions."""
    def probe(corr: str, **kw):
        return fetch_all_logs(control_client, admin_headers, corr, **kw)

    return probe
