# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The execution limits of an MCP call are refused at save when the gateway
could not read them.

The gateway (`brutor-gateway-core`, `src/services/mcp_limits.rs`) reads four
fields of a group's `mcp_global_limits`, of a server binding's `limits` and of
an MCP server's `tenant_limits`: `execution.timeout_seconds` and
`data.max_payload_size_mb` (positive numbers), `execution.
max_concurrency_per_user` and `execution.max_concurrency_per_group` (positive
whole numbers). A stated value that is anything else (0, a negative, a
string, a boolean, a fractional count) makes it refuse EVERY call of the
group to the server. So the Control Plane refuses such a value wherever the
documents are written, with a sentence that names the field and the rule;
`null` is unset.

`mcp_servers.max_concurrent_requests` is the server's in-flight ceiling on
one gateway instance (NULL: 10): a positive whole number when set.

The Policy-as-Code half is in `enterprise/tests/test_mcp_execution_limits_bundle.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.models.admin.mcp_server import ServerCreateRequest, ServerUpdateRequest
from app.core.logging_system import BrutorLogger
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import ResourceGroup
from app.database.models.server_config import ServerConfig
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness

validate = ResourceGroup.validate_mcp_limits_structure

NUMBERS = [("execution", "timeout_seconds"), ("data", "max_payload_size_mb")]
COUNTS = [("execution", "max_concurrency_per_user"), ("execution", "max_concurrency_per_group")]
FIELDS = NUMBERS + COUNTS

# What the gateway cannot read in any of the four fields.
UNREADABLE = [0, -1, -0.5, "5", "", True, False, [], {}, float("inf"), float("nan")]
# What it cannot read in a count only.
UNREADABLE_COUNT = [2.5, 0.1]


def _doc(section, key, value):
    return {section: {key: value}}


def _refusals():
    for section, key in FIELDS:
        for value in UNREADABLE:
            yield section, key, value
    for section, key in COUNTS:
        for value in UNREADABLE_COUNT:
            yield section, key, value


REFUSED = list(_refusals())
ACCEPTED = [
    ("execution", "timeout_seconds", 30),
    ("execution", "timeout_seconds", 0.5),
    ("execution", "max_concurrency_per_user", 2),
    ("execution", "max_concurrency_per_user", 2.0),
    ("execution", "max_concurrency_per_group", 20),
    ("data", "max_payload_size_mb", 5),
    ("data", "max_payload_size_mb", 0.25),
]


def _names_the_field_and_rule(why: str, section: str, key: str) -> None:
    assert f"{section}.{key}" in why, why
    rule = "positive whole number" if (section, key) in COUNTS else "positive number"
    assert rule in why, why


# --- The rule -----------------------------------------------------------------------------------


@pytest.mark.parametrize("section,key,value", REFUSED)
def test_a_value_the_gateway_cannot_read_is_refused(section, key, value):
    ok, why = validate(_doc(section, key, value))
    assert not ok
    _names_the_field_and_rule(why, section, key)


@pytest.mark.parametrize("section,key,value", ACCEPTED)
def test_a_positive_value_is_accepted(section, key, value):
    assert validate(_doc(section, key, value)) == (True, None)


@pytest.mark.parametrize("section,key", FIELDS)
def test_null_is_unset(section, key):
    assert validate(_doc(section, key, None)) == (True, None)
    assert validate({section: None}) == (True, None)


@pytest.mark.parametrize("section", ["execution", "data"])
def test_a_section_that_is_not_an_object_is_refused(section):
    ok, why = validate({section: 5})
    assert not ok and section in why


# --- An MCP server's tenant_limits --------------------------------------------------------------


@pytest.mark.parametrize("section,key,value", REFUSED)
def test_a_server_cannot_store_unreadable_tenant_limits(section, key, value):
    for model in (ServerCreateRequest, ServerUpdateRequest):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({"name": "s", "base_url": "https://mcp.example", "tenant_limits": _doc(section, key, value)})
        _names_the_field_and_rule(str(caught.value), section, key)


@pytest.mark.parametrize("value", [0, -1, 2.5, "3", True])
def test_max_concurrent_requests_is_a_positive_whole_number(value):
    for model in (ServerCreateRequest, ServerUpdateRequest):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({"name": "s", "base_url": "https://mcp.example", "max_concurrent_requests": value})
        why = str(caught.value)
        assert "max_concurrent_requests" in why and "positive whole number" in why, why


def test_max_concurrent_requests_may_be_set_or_left_unset():
    for model in (ServerCreateRequest, ServerUpdateRequest):
        assert model.model_validate({"name": "s", "base_url": "https://x", "max_concurrent_requests": 3}).max_concurrent_requests == 3
        assert model.model_validate({"name": "s", "base_url": "https://x", "max_concurrent_requests": None}).max_concurrent_requests is None


def test_max_concurrent_requests_is_null_by_default_and_null_reads_as_ten():
    column = MCPServer.__table__.c.max_concurrent_requests
    assert column.nullable
    assert column.default is None and column.server_default is None
    assert "10" in column.comment and "instance" in column.comment


# --- The admin API ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("test-mcp-exec-limits")
    harness = build_admin_harness(
        [
            create_resource_groups_router(UsageLimitEnforcer(logger=log), log),
            create_mcp_server_router(MCPServerManager(log), log),
        ],
        logger_name="test-mcp-exec-limits",
    )

    def seed(s):
        s.add(MCPServer(id="srv-x", tenant_id="t1", name="srv-x", base_url="https://mcp.example"))
        s.flush()
        s.add(ServerConfig(id="cfg-x", tenant_id="t1", name="cfg-x", mcp_server_id="srv-x", enabled=True))

    harness.seed(seed)
    yield harness
    harness.close()


_n = {"i": 0}
BASE = "/v1/admin/resource-groups"
BAD = {"execution": {"max_concurrency_per_user": 0}}
GOOD = {"execution": {"timeout_seconds": 1.5, "max_concurrency_per_user": 2}, "data": {"max_payload_size_mb": 1}}


def _group(h, **body) -> str:
    _n["i"] += 1
    r = h.client.post(BASE, json={"name": f"g-{_n['i']}", **body}, headers=h.headers("t1"))
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("group", payload)["id"]


def _stored(h, gid):
    s = h.Factory()
    try:
        return s.get(ResourceGroup, gid).mcp_global_limits
    finally:
        s.close()


def _refused(r):
    assert r.status_code in (400, 422), r.text
    assert "execution.max_concurrency_per_user" in r.text and "positive whole number" in r.text, r.text


def test_a_group_is_not_created_with_unreadable_limits(h):
    _n["i"] += 1
    name = f"g-bad-{_n['i']}"
    _refused(h.client.post(BASE, json={"name": name, "mcp_global_limits": BAD}, headers=h.headers("t1")))
    s = h.Factory()
    try:
        assert s.query(ResourceGroup).filter_by(name=name).first() is None
    finally:
        s.close()


def test_a_group_is_created_with_readable_limits(h):
    assert _stored(h, _group(h, mcp_global_limits=GOOD)) == GOOD


def test_a_group_update_cannot_store_unreadable_limits(h):
    gid = _group(h, mcp_global_limits=GOOD)
    _refused(h.client.patch(f"{BASE}/{gid}", json={"mcp_global_limits": BAD}, headers=h.headers("t1")))
    assert _stored(h, gid) == GOOD


def test_the_group_limits_endpoint_cannot_store_unreadable_limits(h):
    gid = _group(h, mcp_global_limits=GOOD)
    _refused(h.client.patch(f"{BASE}/{gid}/mcp-global-limits", json={"mcp_global_limits": BAD}, headers=h.headers("t1")))
    assert _stored(h, gid) == GOOD


def test_a_server_binding_cannot_store_unreadable_limits(h):
    gid = _group(h)
    r = h.client.post(f"{BASE}/{gid}/server-configs", json={"server_config_id": "cfg-x"}, headers=h.headers("t1"))
    assert r.status_code in (200, 201), r.text
    path = f"{BASE}/{gid}/mcp-servers/cfg-x/limits"
    _refused(h.client.patch(path, json={"limits": BAD}, headers=h.headers("t1")))
    r = h.client.patch(path, json={"limits": GOOD}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text


def _server(h, **over):
    body = {"name": f"s-{_n['i']}", "base_url": f"http://tools-{_n['i']}.internal:3000", "mcp_endpoint_path": "/mcp", "auth_type": "none", **over}
    _n["i"] += 1
    return h.client.post("/v1/admin/mcp-servers?discover_capabilities=false", json=body, headers=h.headers("t1"))


def test_a_server_is_not_created_or_updated_with_unreadable_tenant_limits(h):
    _refused(_server(h, tenant_limits=BAD))
    r = _server(h)
    assert r.status_code in (200, 201), r.text
    sid = r.json()["server"]["id"]
    _refused(h.client.patch(f"/v1/admin/mcp-servers/{sid}", json={"tenant_limits": BAD}, headers=h.headers("t1")))


def test_max_concurrent_requests_round_trips(h):
    r = _server(h)
    assert r.status_code in (200, 201), r.text
    server = r.json()["server"]
    assert server["max_concurrent_requests"] is None
    url = f"/v1/admin/mcp-servers/{server['id']}"
    r = h.client.patch(url, json={"max_concurrent_requests": 4}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    assert r.json()["server"]["max_concurrent_requests"] == 4
    r = h.client.patch(url, json={"max_concurrent_requests": 0}, headers=h.headers("t1"))
    assert r.status_code == 422 and "positive whole number" in r.text, r.text
    r = h.client.patch(url, json={"max_concurrent_requests": None}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    assert r.json()["server"]["max_concurrent_requests"] is None
    r = _server(h, max_concurrent_requests=7)
    assert r.status_code in (200, 201), r.text
    assert r.json()["server"]["max_concurrent_requests"] == 7


# --- What the API description says --------------------------------------------------------------


def _schemas():
    from tests.app_factory import create_app

    return create_app().openapi()["components"]["schemas"]


@pytest.fixture(scope="module")
def schemas():
    return _schemas()


def _field(schemas, model, field):
    prop = schemas[model]["properties"][field]
    return prop.get("description", "")


def test_the_four_fields_say_what_the_gateway_does(schemas):
    timeout = _field(schemas, "McpExecutionLimitsDocument", "timeout_seconds")
    assert "streamed" in timeout and "start" in timeout and "positive number" in timeout
    per_user = _field(schemas, "McpExecutionLimitsDocument", "max_concurrency_per_user")
    for word in ("tools/call", "listener", "agent", "user", "identity provider", "key", "each descendant group separately"):
        assert word in per_user, (word, per_user)
    per_group = _field(schemas, "McpExecutionLimitsDocument", "max_concurrency_per_group")
    for word in ("tools/call", "listener", "each descendant group separately", "positive whole number"):
        assert word in per_group, (word, per_group)
    payload = _field(schemas, "McpDataLimitsDocument", "max_payload_size_mb")
    for word in ("request", "answer", "10 MiB", "unset", "positive number"):
        assert word in payload, (word, payload)


@pytest.mark.parametrize(
    "model,field",
    [
        ("ResourceGroupCreate", "mcp_global_limits"),
        ("ResourceGroupUpdate", "mcp_global_limits"),
        ("MCPGlobalLimitsUpdate", "mcp_global_limits"),
        ("ServerCreateRequest", "tenant_limits"),
        ("ServerUpdateRequest", "tenant_limits"),
    ],
)
def test_every_mcp_limits_document_is_described(schemas, model, field):
    prop = schemas[model]["properties"][field]
    refs = [prop.get("$ref")] + [a.get("$ref") for a in prop.get("anyOf", [])]
    assert "#/components/schemas/McpLimitsDocument" in refs, prop


def test_max_concurrent_requests_says_it_is_per_instance(schemas):
    for model in ("ServerCreateRequest", "ServerUpdateRequest", "ServerResponse"):
        text = _field(schemas, model, "max_concurrent_requests")
        assert "gateway instance" in text and "10" in text, (model, text)


# --- Keys the gateway does not read -------------------------------------------------------------
# The document schema (`McpLimitsDocument`) says `additionalProperties: false`,
# but the validator stored any other key, never read. Refused at every writer.
# The five keys this release does not enforce keep their own refusal
# (`release_scope.MCP_UNAVAILABLE`).

UNKNOWN = [
    ({"retries": 3}, "retries"),
    ({"execution": {"timeout": 30}}, "execution.timeout"),
    ({"execution": {"timeout_seconds": 30, "max_concurrency": 2}}, "execution.max_concurrency"),
    ({"data": {"max_response_size_mb": 2}}, "data.max_response_size_mb"),
    ({"frequency": {"max_calls_per_second": 2}}, "frequency.max_calls_per_second"),
    ({"per_tool": {"search": {"max_calls_per_minute": 5, "burst": 2}}}, "per_tool 'search'.burst"),
    ({"execution": {"timeout": None}}, "execution.timeout"),
]


def _names_the_unknown_key(why: str, where: str) -> None:
    assert where in why and "not a key of an MCP limits document" in why, why


@pytest.mark.parametrize("limits,where", UNKNOWN)
def test_a_key_the_gateway_does_not_read_is_refused(limits, where):
    ok, why = validate(limits)
    assert not ok
    _names_the_unknown_key(why, where)


@pytest.mark.parametrize(
    "section,key",
    [("execution", "memory_limit_mb"), ("execution", "cpu_limit_percent"), ("data", "max_context_window_percent"),
     ("data", "max_rows_per_query"), ("data", "max_items_per_response")],
)
def test_the_keys_this_release_does_not_enforce_keep_their_refusal(section, key):
    ok, why = validate({section: {key: 5}})
    assert not ok and "not available in this release" in why and f"{section}.{key}" in why, why
    assert validate({section: {key: None}}) == (True, None)


def test_every_key_the_gateway_reads_is_accepted():
    every = {
        "execution": {"timeout_seconds": 30, "max_concurrency_per_user": 2, "max_concurrency_per_group": 4},
        "data": {"max_payload_size_mb": 1},
        "frequency": {"max_calls_per_minute": 5, "max_calls_per_hour": 50, "max_calls_per_day": 500,
                      "burst_limit": 5, "cooldown_seconds": 10, "daily_warning_percent": 80},
        "per_tool": {"search": {"max_calls_per_minute": 1}},
    }
    assert validate(every) == (True, None)


def test_the_keys_accepted_are_the_documents():
    """The validator and the document the API description gives name the same keys."""
    from app.api.v1.models.admin.mcp_limits_document import (
        McpDataLimitsDocument, McpExecutionLimitsDocument, McpLimitsDocument,
    )
    from app.services.release_scope import MCP_UNAVAILABLE

    unavailable = {(s, k) for s, k, _ in MCP_UNAVAILABLE}
    assert set(ResourceGroup.MCP_LIMITS_KEYS) == set(McpLimitsDocument.model_fields)
    assert set(ResourceGroup.MCP_LIMITS_KEYS["execution"]) == set(McpExecutionLimitsDocument.model_fields) | {
        k for s, k in unavailable if s == "execution"}
    assert set(ResourceGroup.MCP_LIMITS_KEYS["data"]) == set(McpDataLimitsDocument.model_fields) | {
        k for s, k in unavailable if s == "data"}


def test_a_server_cannot_store_an_unknown_key_in_tenant_limits():
    for model in (ServerCreateRequest, ServerUpdateRequest):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({"name": "s", "base_url": "https://mcp.example", "tenant_limits": {"execution": {"timeout": 30}}})
        _names_the_unknown_key(str(caught.value), "execution.timeout")


UNKNOWN_DOC = {"execution": {"timeout_seconds": 5, "timeout": 30}}


def _refused_unknown(r):
    assert r.status_code in (400, 422), r.text
    _names_the_unknown_key(r.text, "execution.timeout")


def test_no_admin_api_writer_stores_an_unknown_key(h):
    _n["i"] += 1
    name = f"g-unknown-{_n['i']}"
    _refused_unknown(h.client.post(BASE, json={"name": name, "mcp_global_limits": UNKNOWN_DOC}, headers=h.headers("t1")))
    gid = _group(h, mcp_global_limits=GOOD)
    _refused_unknown(h.client.patch(f"{BASE}/{gid}", json={"mcp_global_limits": UNKNOWN_DOC}, headers=h.headers("t1")))
    _refused_unknown(h.client.patch(f"{BASE}/{gid}/mcp-global-limits", json={"mcp_global_limits": UNKNOWN_DOC}, headers=h.headers("t1")))
    assert _stored(h, gid) == GOOD
    r = h.client.post(f"{BASE}/{gid}/server-configs", json={"server_config_id": "cfg-x"}, headers=h.headers("t1"))
    assert r.status_code in (200, 201), r.text
    _refused_unknown(h.client.patch(f"{BASE}/{gid}/mcp-servers/cfg-x/limits", json={"limits": UNKNOWN_DOC}, headers=h.headers("t1")))
    _refused_unknown(_server(h, tenant_limits=UNKNOWN_DOC))
    r = _server(h)
    assert r.status_code in (200, 201), r.text
    sid = r.json()["server"]["id"]
    _refused_unknown(h.client.patch(f"/v1/admin/mcp-servers/{sid}", json={"tenant_limits": UNKNOWN_DOC}, headers=h.headers("t1")))
