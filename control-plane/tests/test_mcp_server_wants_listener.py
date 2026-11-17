# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression suite for the v0.7.95 bench-report findings around the
`wants_listener` flag on MCP servers. Two distinct bugs lived in two
distinct layers; this file locks both fixes in.

1. `MCPServerManager.to_response` — the dict that feeds the
   `ServerResponse` Pydantic model — was missing `wants_listener`.
   Pydantic doesn't 500 on missing keys; it falls back to the field
   default. Operators who PATCH'ed `wants_listener=false` then read the
   server back and saw `null` (when the response model's default
   evaluated to None at serialisation time) — they concluded the PATCH
   silently dropped the field. The DB had the right value all along;
   the API just wasn't returning it.

2. `MCPServerManager._derive_wants_listener` — the function that turns
   an MCP `initialize` capabilities object into the boolean we store.
   The derivation is the source of truth used by both the admin
   re-discovery path and (mirrored) the runtime Rust proxy. A drift
   between the two paths would put an operator in a state where the
   admin UI says one thing and the gateway behaves the other way.

Both tests are pure unit tests — no DB, no network, no SDK round-trip.
The fakes here mirror the shape the real `mcp` Python SDK returns from
`session.initialize()`, but only the attributes the derivation reads.
"""

from types import SimpleNamespace
from typing import Optional

import pytest

from app.services.mcp.server.mcp_server_manager import MCPServerManager


# ---------------------------------------------------------------------------
# `to_response` surfaces `wants_listener` from the DB column
# ---------------------------------------------------------------------------

class _FakeMCPServer:
    """Minimal shim covering the attributes `to_response` touches.

    Keeps the test independent of SQLAlchemy and the mountains of
    behaviour on the real `MCPServer` model — we exercise the dict
    construction only, not any ORM machinery.
    """

    def __init__(self, *, wants_listener: Optional[bool]):
        self.id = "mcp-test-id"
        self.name = "test-server"
        self.description = None
        self.base_url = "https://example.com"
        self.mcp_endpoint_path = "/mcp"
        self.mcp_url = "https://example.com/mcp"
        self.auth_type = "none"
        self.auth_token = None
        self.idp_id = None
        self.timeout = 30.0
        self.max_concurrent_requests = None
        self.enabled = True
        self.capabilities = None
        self.protocol_version = None
        self.dialect = None
        self.discovered_at = None
        self.server_info = None
        self.wants_listener = wants_listener
        self.mcp_apps_enabled = None
        self.tenant_limits = None
        self.tenant_governance = None
        self.region = None
        # AI Resource version + authorship block. Present on the
        # real model; the shim has to carry them too or `to_response`
        # raises before it reaches the field under test.
        self.version = "1.0"
        self.created_by = None
        self.updated_by = None
        self.created_at = None
        self.updated_at = None

    # The real methods used by `to_response`; trivial stubs are fine.
    def get_icon_info(self):
        return None

    def get_health_status_dict(self):
        return None


@pytest.mark.parametrize(
    "db_value, expected",
    [
        # Operator explicitly set False via PATCH → must read back False.
        # This is the exact bug scenario from the bench report.
        (False, False),
        # Default state: True is the safe assumption for a non-probed server.
        (True, True),
        # Legacy row from before the column existed (NULL in DB → None in ORM).
        # Defaults to True so we don't silently flip behaviour for any
        # server the gateway hasn't yet had a chance to re-probe.
        (None, True),
    ],
    ids=["operator_false", "default_true", "legacy_null_defaults_true"],
)
def test_to_response_surfaces_wants_listener(db_value, expected):
    server = _FakeMCPServer(wants_listener=db_value)
    # The manager only needs a logger to construct; `to_response`
    # itself doesn't touch it. A logging.NullHandler-style stub
    # would also work; the real BrutorLogger is fine and avoids
    # mocking complications.
    from app.core.logging_system import BrutorLogger
    manager = MCPServerManager(logger=BrutorLogger("test"))
    response = manager.to_response(server)
    # The presence of the key is what matters first — its absence is the
    # bug — and then the value must reflect the DB column with the
    # legacy-NULL → True defaulting consistent with `MCPServer.to_dict`.
    assert "wants_listener" in response, (
        "to_response must include `wants_listener` so callers PATCHing the "
        "field can read it back"
    )
    assert response["wants_listener"] is expected


# ---------------------------------------------------------------------------
# `_derive_wants_listener` — capability-aware derivation
# ---------------------------------------------------------------------------

def _caps(**kwargs):
    """Build a fake `init_result.capabilities` whose attribute shape
    mirrors the `mcp` Python SDK (snake_case attributes on a Pydantic
    model). `SimpleNamespace` is enough — the production code uses
    `getattr` with a default for everything.
    """
    return SimpleNamespace(**kwargs)


def test_derive_returns_true_when_capabilities_is_none():
    # Defensive default: an empty/None capabilities object means we
    # couldn't read it, not "server has no notifications". Keep the
    # listener so the gateway can probe and downgrade on the first
    # 405-or-otherwise signal.
    assert MCPServerManager._derive_wants_listener(None) is True


def test_derive_returns_false_for_empty_capabilities_object():
    # An MCP server that returned `capabilities: {}` (no tools,
    # resources, prompts, or logging fields) is provably pure
    # request/response — the listener will never fire. Downgrade
    # wants_listener so the gateway short-circuits listener GETs.
    assert MCPServerManager._derive_wants_listener(_caps()) is False


@pytest.mark.parametrize(
    "section_name",
    ["tools", "resources", "prompts"],
)
def test_derive_returns_true_when_section_has_list_changed(section_name):
    # Any of tools/resources/prompts.listChanged means the server
    # promises change notifications → keep the listener.
    caps = _caps(**{section_name: SimpleNamespace(list_changed=True)})
    assert MCPServerManager._derive_wants_listener(caps) is True


def test_derive_returns_true_when_resources_subscribe_is_set():
    # `resources.subscribe` is a separate subscription channel —
    # also requires the listener.
    caps = _caps(resources=SimpleNamespace(subscribe=True))
    assert MCPServerManager._derive_wants_listener(caps) is True


def test_derive_returns_true_when_logging_is_advertised():
    # `logging` is a marker capability — presence (even as an
    # empty object) signals the server emits log notifications.
    caps = _caps(logging=SimpleNamespace())
    assert MCPServerManager._derive_wants_listener(caps) is True


def test_derive_returns_false_when_listChanged_false_and_no_subscribe():
    # A server that declares the sub-capability objects but with
    # all flags False is still pure request/response.
    caps = _caps(
        tools=SimpleNamespace(list_changed=False),
        resources=SimpleNamespace(list_changed=False, subscribe=False),
        prompts=SimpleNamespace(list_changed=False),
    )
    assert MCPServerManager._derive_wants_listener(caps) is False


def test_derive_tolerates_missing_attrs_on_sub_objects():
    # Real servers return Pydantic models where the SDK has
    # already filled in defaults, but a tampered/older wire format
    # might omit fields entirely. The derivation uses `getattr`
    # with a default, so missing attrs must not raise.
    caps = _caps(tools=SimpleNamespace(), resources=SimpleNamespace())
    # No `list_changed` anywhere, no `subscribe`, no `logging` →
    # listener is useless.
    assert MCPServerManager._derive_wants_listener(caps) is False
