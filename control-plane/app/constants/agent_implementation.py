# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent implementation identity — the wire contract

WHO is acting is decided by the credential (API key bound to an agent
identity, or a verified workload token). WHAT software is acting and WHICH
release of it are accepted by the core from a fixed list of carriers, in
order of trust, and recorded on `proxy_logs`,
`ai_system_runs`, `ai_system_run_segments` and `agent_implementation_releases`.

WIRE CONTRACT: every string here is written by the Rust core
(`brutor-gateway-core/src/constants.rs`) and read by the control plane, or is
what an agent must send. Pinned in BOTH repos (Python:
`tests/test_wire_contract.py`). A rename is a deliberate two-repo edit.

Nothing an agent declares ever decides access. Only descriptive
surfaces — conformance, checks, drift, the report — read these values.
"""

from __future__ import annotations

import enum


class AgentImplHeader(str, enum.Enum):
    """Brutor fallback headers (the fourth carrier in order of trust) and the
    explicit client label. They mirror the OpenTelemetry names."""

    NAME = "X-Brutor-Agent-Name"
    VERSION = "X-Brutor-Agent-Version"
    BUILD = "X-Brutor-Agent-Build"
    CLIENT = "X-Brutor-Client"


class AgentImplBaggageKey(str, enum.Enum):
    """W3C `baggage` members (the third carrier). OpenTelemetry `gen_ai.*` is in
    Development status; this one table is where a rename lands."""

    NAME = "gen_ai.agent.name"
    VERSION = "gen_ai.agent.version"
    BUILD = "gen_ai.agent.build"  # Brutor extension


class AgentImplTokenClaim(str, enum.Enum):
    """Verified workload-token claims (the first carrier; RFC 7591 names)."""

    NAME = "software_id"
    VERSION = "software_version"
    BUILD = "software_build"


#: MCP `_meta` key (MCP 2026-07-28) — the per-request clientInfo carrier;
#: the session's `initialize` `params.clientInfo` is the 2025-11-25 fallback.
MCP_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"


class AgentImplSource(str, enum.Enum):
    """`proxy_logs.agent_impl_source` — which carrier the release came from."""

    TOKEN = "token"
    MCP_CLIENT_INFO = "mcp_client_info"
    BAGGAGE = "baggage"
    HEADER = "header"


class AgentImplTrust(str, enum.Enum):
    """`agent_impl_trust` / `agent_implementation_releases.trust`.
    `token` → verified; every other carrier → declared."""

    VERIFIED = "verified"
    DECLARED = "declared"


AGENT_IMPL_SOURCES = frozenset(s.value for s in AgentImplSource)
AGENT_IMPL_TRUST_LEVELS = frozenset(t.value for t in AgentImplTrust)

#: The drift cause-ladder rung (core `drift/cause.rs`), after
#: `contract_change`, before `model_change`.
DRIFT_CAUSE_AGENT_RELEASE = "agent_release"

#: Lifecycle event raised once per new release row, on every AI System the
#: identity is a member of.
LIFECYCLE_AGENT_NEW_RELEASE = "agent.new_release"

#: `agent_implementation_releases.build` sentinel: no build declared.
NO_BUILD = ""

#: Distinct implementation fingerprints kept per release (core-enforced cap).
RELEASE_FINGERPRINT_CAP = 16

__all__ = [
    "AgentImplHeader",
    "AgentImplBaggageKey",
    "AgentImplTokenClaim",
    "MCP_CLIENT_INFO_META_KEY",
    "AgentImplSource",
    "AgentImplTrust",
    "AGENT_IMPL_SOURCES",
    "AGENT_IMPL_TRUST_LEVELS",
    "DRIFT_CAUSE_AGENT_RELEASE",
    "LIFECYCLE_AGENT_NEW_RELEASE",
    "NO_BUILD",
    "RELEASE_FINGERPRINT_CAP",
]
