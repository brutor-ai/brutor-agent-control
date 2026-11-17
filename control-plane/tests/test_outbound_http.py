# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The fetch-time half of the outbound policy: a client from
`app/services/outbound_http.py` connects only where the policy permits,
judging the addresses a name resolves to and every redirect hop, against a
real local server.
"""

from __future__ import annotations

import http.server
import ipaddress
import threading

import httpx
import pytest

from app.services import outbound_policy as op
from app.services.outbound_http import (
    OutboundRefused,
    is_refusal,
    outbound_async_client,
    outbound_client,
)


@pytest.fixture()
def server():
    """A local server: `/` answers 200, `/hop` redirects to the IP literal."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reached")

        def log_message(self, *_a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_port
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _no_allow_list(monkeypatch):
    monkeypatch.delenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", raising=False)


def test_a_loopback_destination_is_refused_by_default(server):
    with outbound_client() as client:
        with pytest.raises(OutboundRefused) as refused:
            client.get(f"http://127.0.0.1:{server}/")
    assert "BRUTOR_OUTBOUND_ALLOW_HOSTS" in str(refused.value)
    assert isinstance(refused.value, httpx.ConnectError)


def test_a_name_resolving_to_loopback_is_refused(server):
    with outbound_client() as client:
        with pytest.raises(OutboundRefused, match="localhost resolves to a loopback address"):
            client.get(f"http://localhost:{server}/")


def test_the_allow_list_lets_it_through(server, monkeypatch):
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "127.0.0.1")
    with outbound_client() as client:
        assert client.get(f"http://127.0.0.1:{server}/").text == "reached"


async def test_the_async_client_too(server):
    async with outbound_async_client(timeout=5) as client:
        with pytest.raises(OutboundRefused):
            await client.get(f"http://127.0.0.1:{server}/")


def test_every_redirect_hop_is_judged(server, monkeypatch):
    # The name is allowed; the address it redirects to is not.
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "localhost")
    with outbound_client(follow_redirects=True) as client:
        with pytest.raises(OutboundRefused, match="loopback"):
            client.get(f"http://localhost:{server}/hop")


def test_the_metadata_address_is_never_reached(monkeypatch):
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "metadata.internal,169.254.0.0/16")
    monkeypatch.setattr(op, "resolve", lambda host: [ipaddress.ip_address("169.254.169.254")])
    with outbound_client() as client:
        with pytest.raises(OutboundRefused, match="never allowed"):
            client.get("http://metadata.internal/latest/meta-data/")


def test_is_refusal_sees_through_wrapping():
    try:
        try:
            raise OutboundRefused("x", "loopback address")
        except OutboundRefused as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_refusal(outer)
    assert not is_refusal(RuntimeError("other"))
