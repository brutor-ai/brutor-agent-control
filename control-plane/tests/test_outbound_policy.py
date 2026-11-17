# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Control Plane's half of the SSRF guard: URLs an administrator saves are judged against the rules the Core
Gateway and KB Connector Sync apply (`outbound_policy.rs`), under the same
setting, `BRUTOR_OUTBOUND_ALLOW_HOSTS`. Private addresses are refused by
default; link-local and cloud metadata always.
"""

import ipaddress

import pytest
from fastapi import HTTPException

from app.services import outbound_policy as op


@pytest.fixture(autouse=True)
def _no_allow_list(monkeypatch):
    monkeypatch.delenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", raising=False)


@pytest.fixture()
def dns(monkeypatch):
    """Resolve names from a table instead of the network."""
    table: dict = {}
    monkeypatch.setattr(op, "resolve", lambda host: [ipaddress.ip_address(a) for a in table.get(host, [])])
    return table


def test_metadata_and_link_local_are_always_refused(monkeypatch):
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "169.254.0.0/16,fe80::/10,0.0.0.0/0")
    assert op.check_url("http://169.254.169.254/latest/meta-data/")
    assert op.check_url("http://[fe80::1]/")
    assert op.check_url("http://[fd00:ec2::254]/")
    assert op.check_url("http://0.0.0.0/")
    assert op.check_url("http://0.1.2.3/")
    assert op.check_url("ftp://example.com/")


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:11434/v1",
        "http://172.16.1.1/",
        "http://192.168.1.10/",
        "http://127.0.0.1:3000/mcp",
        "http://100.64.3.3/",
        "http://[::1]/",
        "http://[fd12::1]/",
        "http://[fec0::1]/",
        "http://[::ffff:10.0.0.5]/",
        "http://[64:ff9b::a00:5]/",
        "http://[2002:a00:5::1]/",
    ],
)
def test_private_addresses_are_refused_by_default(url):
    assert op.check_url(url)


def test_public_addresses_pass():
    assert op.check_url("http://8.8.8.8/") is None
    assert op.check_url("https://[2606:4700::1]/") is None


def test_a_host_name_is_judged_where_it_is_fetched(dns):
    """As in the Core Gateway: saving does not resolve (the fetch does, on
    every address: `tests/test_outbound_http.py`)."""
    dns["postgres"] = ["172.18.0.4"]
    assert op.check_url("http://postgres:5432/") is None


def test_the_allow_list_takes_hosts_domains_addresses_and_ranges(monkeypatch):
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "ollama, *.svc.cluster.local, 10.20.0.0/16, 192.168.1.10")
    assert op.host_allowed("ollama") and op.host_allowed("OLLAMA.")
    assert op.host_allowed("qdrant.svc.cluster.local") and op.host_allowed("svc.cluster.local")
    assert not op.host_allowed("evil-svc.cluster.local.attacker.com")
    assert op.check_url("http://10.20.3.4/") is None
    assert op.check_url("http://192.168.1.10/") is None
    assert op.check_url("http://192.168.1.11/")
    assert op.check_url("http://10.0.0.5/")


def test_an_unreadable_allow_list_entry_refuses_the_start():
    from app.env_vars import BRUTOR_OUTBOUND_ALLOW_HOSTS

    assert BRUTOR_OUTBOUND_ALLOW_HOSTS.mismatch("ok.example,10.0.0.0/8") is None
    for bad in ("http://x.example", "x.example:8080", "10.0.0.0/33", "a*b"):
        assert BRUTOR_OUTBOUND_ALLOW_HOSTS.mismatch(bad), bad


def test_the_refusal_names_the_url_and_the_setting():
    with pytest.raises(HTTPException) as exc:
        op.assert_allowed_url("http://user:secret@10.0.0.5/mcp", "MCP server base URL")
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert "'http://10.0.0.5/mcp'" in detail and "secret" not in detail
    assert "BRUTOR_OUTBOUND_ALLOW_HOSTS" in detail
    with pytest.raises(HTTPException) as exc:
        op.assert_allowed_url("http://169.254.169.254/", "MCP server base URL")
    assert "never allowed" in exc.value.detail


def test_assert_helpers_walk_a_config():
    with pytest.raises(HTTPException):
        op.assert_allowed_urls_in({"base_url": "https://8.8.8.8", "seeds": ["http://169.254.169.254/"]})
    op.assert_allowed_urls_in({"base_url": "https://8.8.8.8", "depth": 3, "seeds": ["https://1.1.1.1"]})


def test_llm_model_request_refuses_a_private_api_url():
    from pydantic import ValidationError
    from app.api.v1.models.admin.llm_model import LLModelCreate

    base = dict(
        model_name="gpt-test",
        display_name="gpt-test",
        provider="openai",
        api_url_auto_discovered=False,
        api_url="http://10.1.2.3/v1",
    )
    with pytest.raises(ValidationError) as exc:
        LLModelCreate(**base)
    assert "outbound destination policy" in str(exc.value)
    assert "BRUTOR_OUTBOUND_ALLOW_HOSTS" in str(exc.value)


def test_the_retired_switch_is_gone():
    from app import env_vars

    assert not hasattr(env_vars, "BRUTOR_OUTBOUND_DENY_PRIVATE_ENABLED")
