# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# BRUTOR_TRUSTED_PROXIES: whose forwarded headers the Control Plane believes

A request's client IP is the TCP peer's address unless the peer is a
trusted proxy; then it is the right-most `X-Forwarded-For` address that is
not itself a trusted proxy. `X-Forwarded-Proto` and `X-Forwarded-Host` are
used only from a trusted proxy. Unset: no forwarded header is believed.
The Core Gateway reads the same variable with the same rules
(`.scratch/envaudit/trusted-proxies.md` while both are written).

One middleware applies the rules and one helper (`client_ip`) is what every
audit row, DCR's source IP and session record reads; Uvicorn's and
Gunicorn's own proxy-header handling is switched off so there is no second
implementation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import env_vars
from app.middleware import trusted_proxies as tp

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def trust(monkeypatch):
    def _set(value):
        if value is None:
            monkeypatch.delenv("BRUTOR_TRUSTED_PROXIES", raising=False)
        else:
            monkeypatch.setenv("BRUTOR_TRUSTED_PROXIES", value)

    return _set


def _resolve(peer, headers, trusted):
    return tp.resolve(peer, [(k.lower().encode(), v.encode()) for k, v in headers], tp.parse_trusted(trusted))


# --- the variable ----------------------------------------------------------------


def test_the_variable_is_registered_and_empty_trusts_nobody():
    var = env_vars.BRUTOR_TRUSTED_PROXIES
    assert var.default is None
    assert var.mismatch("10.0.0.0/8, 192.168.1.10 ,fd00::/8") is None
    assert var.mismatch("") is None or var.empty_unsets


@pytest.mark.parametrize("bad", ["*", "proxy.internal", "10.0.0.0/33", "0.0.0.0/0x", "1.2.3.4:80"])
def test_the_variable_refuses_what_is_not_an_address_or_range(bad):
    assert env_vars.BRUTOR_TRUSTED_PROXIES.mismatch(bad)


# --- the client IP ------------------------------------------------------------


def test_untrusted_peer_ignores_forwarded_for():
    r = _resolve("203.0.113.9", [("X-Forwarded-For", "1.1.1.1")], "")
    assert r.client == "203.0.113.9"


def test_unset_list_trusts_even_loopback_nothing():
    r = _resolve("127.0.0.1", [("X-Forwarded-For", "1.1.1.1"), ("X-Forwarded-Proto", "https")], "")
    assert r.client == "127.0.0.1"
    assert r.scheme is None and r.host is None


def test_trusted_peer_takes_right_most_untrusted_entry():
    r = _resolve(
        "10.0.0.2",
        [("X-Forwarded-For", "6.6.6.6, 198.51.100.7, 10.0.0.3")],
        "10.0.0.0/8",
    )
    # 6.6.6.6 is what the client claimed; 198.51.100.7 is what the first
    # trusted proxy saw.
    assert r.client == "198.51.100.7"


def test_multiple_forwarded_for_headers_join_in_order():
    r = _resolve(
        "10.0.0.2",
        [("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "198.51.100.7, 10.0.0.3")],
        "10.0.0.0/8",
    )
    assert r.client == "198.51.100.7"


def test_all_trusted_entries_give_the_left_most():
    r = _resolve("10.0.0.2", [("X-Forwarded-For", "10.0.0.5, 10.0.0.3")], "10.0.0.0/8")
    assert r.client == "10.0.0.5"


def test_invalid_entry_stops_the_walk_at_the_peer():
    r = _resolve("10.0.0.2", [("X-Forwarded-For", "1.2.3.4, unknown, 10.0.0.3")], "10.0.0.0/8")
    assert r.client == "10.0.0.2"


def test_no_forwarded_for_from_trusted_peer_is_the_peer():
    assert _resolve("10.0.0.2", [], "10.0.0.0/8").client == "10.0.0.2"
    assert _resolve("10.0.0.2", [("X-Forwarded-For", " , ")], "10.0.0.0/8").client == "10.0.0.2"


def test_ports_are_removed_and_ipv6_is_handled():
    r = _resolve("10.0.0.2", [("X-Forwarded-For", "[2001:db8::1]:4711, 198.51.100.7:80")], "10.0.0.2")
    assert r.client == "198.51.100.7"
    r = _resolve("fd00::2", [("X-Forwarded-For", "[2001:db8::1]:4711")], "fd00::/8")
    assert r.client == "2001:db8::1"


def test_ipv4_mapped_peer_is_compared_as_ipv4():
    r = _resolve("::ffff:10.0.0.2", [("X-Forwarded-For", "198.51.100.7")], "10.0.0.0/8")
    assert r.client == "198.51.100.7"
    assert _resolve("::ffff:203.0.113.9", [], "").client == "203.0.113.9"


def test_peer_that_is_not_an_address_is_never_trusted():
    r = _resolve("testclient", [("X-Forwarded-For", "1.1.1.1")], "10.0.0.0/8")
    assert r.client == "testclient"


# --- scheme and host ----------------------------------------------------------------


def test_proto_and_host_only_from_a_trusted_peer():
    headers = [("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", "admin.example.com")]
    r = _resolve("203.0.113.9", headers, "10.0.0.0/8")
    assert (r.scheme, r.host) == (None, None)
    r = _resolve("10.0.0.2", headers, "10.0.0.0/8")
    assert (r.scheme, r.host) == ("https", "admin.example.com")


def test_proto_and_host_take_the_right_most_valid_value():
    r = _resolve(
        "10.0.0.2",
        [("X-Forwarded-Proto", "http, HTTPS"), ("X-Forwarded-Host", "evil.example, cp.example.com:8443")],
        "10.0.0.0/8",
    )
    assert (r.scheme, r.host) == ("https", "cp.example.com:8443")


@pytest.mark.parametrize("host", ["a b", "x/y", "u@h", "h:99999", "", "h?q", "h#f"])
def test_implausible_forwarded_host_is_ignored(host):
    r = _resolve("10.0.0.2", [("X-Forwarded-Host", host)], "10.0.0.0/8")
    assert r.host is None


def test_unknown_proto_is_ignored():
    r = _resolve("10.0.0.2", [("X-Forwarded-Proto", "gopher")], "10.0.0.0/8")
    assert r.scheme is None


# --- the middleware and the helper ---------------------------------------------------


def _probe_app():
    async def probe(request: Request):
        return JSONResponse(
            {
                "client_ip": tp.client_ip(request),
                "scheme": request.url.scheme,
                "host": request.url.netloc,
            }
        )

    return tp.TrustedProxyMiddleware(Starlette(routes=[Route("/p", probe)]))


async def _call(app, peer, headers):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/p",
        "raw_path": b"/p",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"cp:5050")] + [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": (peer, 1234),
        "server": ("cp", 5050),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    import json

    return json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


@pytest.mark.asyncio
async def test_middleware_rewrites_client_scheme_and_host_from_trusted_peer(trust):
    trust("10.0.0.0/8")
    body = await _call(
        _probe_app(),
        "10.0.0.2",
        [("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", "cp.example.com")],
    )
    assert body == {"client_ip": "198.51.100.7", "scheme": "https", "host": "cp.example.com"}


@pytest.mark.asyncio
async def test_middleware_ignores_forwarded_headers_by_default(trust):
    trust(None)
    body = await _call(
        _probe_app(),
        "127.0.0.1",
        [("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", "evil")],
    )
    assert body == {"client_ip": "127.0.0.1", "scheme": "http", "host": "cp:5050"}


def test_helper_resolves_without_the_middleware(trust):
    trust("10.0.0.0/8")
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"198.51.100.7")],
        "client": ("10.0.0.2", 1),
    }
    assert tp.client_ip(Request(scope)) == "198.51.100.7"
    trust(None)
    assert tp.client_ip(Request(scope)) == "10.0.0.2"
    assert tp.client_ip(Request({"type": "http", "headers": []})) is None
    assert tp.client_ip(None) is None


def test_create_app_puts_the_middleware_outside_every_reader_of_the_request():
    from app.middleware.cache_invalidation import CacheInvalidationWarningMiddleware
    from tests.app_factory import create_app

    stack = [m.cls for m in create_app().user_middleware]  # outermost first
    # Only the response-warning middleware (which reads nothing of the
    # request) is outside it.
    assert stack[:2] == [CacheInvalidationWarningMiddleware, tp.TrustedProxyMiddleware]


# --- one implementation ------------------------------------------------------------


def _python_files():
    for root in ("app", "enterprise/brutor_enterprise"):
        for path in sorted((REPO / root).rglob("*.py")):
            yield path


def test_no_other_code_reads_forwarded_headers_or_the_raw_peer():
    # The helper, and the variable's description.
    allowed = {REPO / "app" / "middleware" / "trusted_proxies.py", REPO / "app" / "env_vars.py"}
    offenders = []
    for path in _python_files():
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"x-forwarded|\.client\.host|\.client\s+else|scope\s*\[\s*[\"']client[\"']\s*\]|scope\.get\(\s*[\"']client", line, re.I):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert offenders == []


def test_uvicorn_and_gunicorn_trust_no_forwarded_header_themselves():
    tree = ast.parse((REPO / "gunicorn.conf.py").read_text())
    assigned = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "forwarded_allow_ips" in assigned
    assert isinstance(assigned["forwarded_allow_ips"], ast.Constant)
    assert assigned["forwarded_allow_ips"].value == ""

    launcher = (REPO / "app" / "launcher.py").read_text()
    assert '"proxy_headers": False' in launcher
    assert '"forwarded_allow_ips": ""' in launcher


def test_no_saas_wording_in_code():
    offenders = []
    for path in _python_files():
        if "catalog_data" in path.parts:
            continue  # MCP catalog category names ("SaaS & API Integrations") are data
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"saas", line, re.I):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert offenders == []
