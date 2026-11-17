# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
XREPO-9 — one default for `BRUTOR_GATEWAY_INTERNAL_URL`.

Ten call sites reached the Core Proxy and nine defaulted to loopback while
`knowledge_base_service` defaulted to `http://core:8100`. The two are
mutually exclusive — the Docker service name does not resolve on a native
`python main.py` run, loopback does not leave the control-plane container —
so whenever the variable was unset exactly one group broke, and the odd one
out was KB ingestion. This pins the single source and asserts nobody has
re-inlined a default.
"""

import ast
import os
import pathlib

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.constants.proxy import DEFAULT_PROXY_INTERNAL_URL, proxy_internal_url
from tests.source_trees import python_sources

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def test_default_is_loopback():
    assert DEFAULT_PROXY_INTERNAL_URL == "http://127.0.0.1:8100"


def test_helper_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", "http://core:8100/")
    assert proxy_internal_url() == "http://core:8100"


def test_helper_falls_back_and_strips_the_trailing_slash(monkeypatch):
    monkeypatch.delenv("BRUTOR_GATEWAY_INTERNAL_URL", raising=False)
    assert proxy_internal_url() == DEFAULT_PROXY_INTERNAL_URL
    assert not proxy_internal_url().endswith("/")


def test_no_module_inlines_its_own_default():
    """`os.environ.get("BRUTOR_GATEWAY_INTERNAL_URL", <anything>)` outside
    app/constants/proxy.py is a second source of truth."""
    offenders = []
    for path in python_sources(APP):
        if path.name == "proxy.py" and path.parent.name == "constants":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in ("get", "getenv")):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "BRUTOR_GATEWAY_INTERNAL_URL":
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "these sites default BRUTOR_GATEWAY_INTERNAL_URL themselves instead of calling "
        "app.constants.proxy.proxy_internal_url():\n  " + "\n  ".join(offenders)
    )
