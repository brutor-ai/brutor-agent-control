# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Observed agent events — local tool-call audit from Brutor Scout.

A coding agent on a developer's laptop does real work against real data and
never touches the gateway. The gateway can only see what routes through it;
this is the record of what did not.

## The wall this table is built to keep

Scout is **observe-only**, and its stated boundary is that it reads config
files rather than intercepting traffic or reading payloads. Tool-call auditing
gets closer to that line than anything else Scout does, so the schema enforces
the boundary rather than trusting the collector to respect it: there is
**no column for tool input or output**. What lands here is metadata — which
tool, on which device, in which session, allowed or denied, how long it took.
A hook that wanted to ship prompt text would have nowhere to put it.

## Why a separate table rather than a flag on proxy_logs

Local activity must never merge into governed lineage. A `source`
column on the shared table would make that a convention one careless JOIN
could break. A separate table makes it structural: a query against
`proxy_logs` cannot accidentally include local activity, and the Assurance
Report's coverage claims stay bounded to traffic the gateway actually saw.

Mission Control reads both and shows them side by side. That is the point —
"here is what we govern, and here is what we can see but do not" is a more
honest estate picture than either half alone.
"""

from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.database import Base

# --- vocabulary -------------------------------------------------------------

#: Which hook produced the record. `pre` can carry a decision; `post` carries
#: the outcome. They are separate rows rather than one joined row because a
#: denied call has no post phase, and an agent killed mid-tool never sends one.
PHASE_PRE = "pre"
PHASE_POST = "post"
PHASES = frozenset({PHASE_PRE, PHASE_POST})

#: What the client's own hook decided. Recorded, never enforced by us — this
#: is the agent's local decision, observed after the fact.
DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_ASK = "ask"
DECISIONS = frozenset({DECISION_ALLOW, DECISION_DENY, DECISION_ASK})

#: Builtin tools (Bash, Read, Edit…) vs MCP tools (`mcp__<server>__<tool>`).
#: The split matters: an MCP tool call names a server that may itself be an
#: ungoverned asset already in the discovered estate.
KIND_BUILTIN = "builtin"
KIND_MCP = "mcp"
KINDS = frozenset({KIND_BUILTIN, KIND_MCP})

#: Provenance. Distinct from proxy_metrics' 'observed' (vendor-API import) so
#: the two kinds of not-governed telemetry stay tellable apart.
SOURCE_ENDPOINT = "endpoint"


def classify_tool(tool_name: str) -> tuple:
    """Split a tool name into (kind, mcp_server).

    Claude Code names MCP tools `mcp__<server>__<tool>`. Recognising the
    prefix is what lets a local tool call be joined back to a discovered
    `local_mcp_server` asset — the whole reason the endpoint plane is worth
    collecting rather than merely counting.
    """
    if tool_name and tool_name.startswith("mcp__"):
        rest = tool_name[len("mcp__") :]
        server = rest.split("__", 1)[0] if "__" in rest else rest
        return KIND_MCP, (server or None)
    return KIND_BUILTIN, None


class ObservedAgentEvent(Base):
    """One local tool-call record from a device-resident AI client."""

    __tablename__ = "observed_agent_events"

    id = Column(String(255), primary_key=True)  # oae-<ULID>
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Collector-assigned, deterministic. Idempotency anchor for at-least-once
    #: delivery from the device spool — a laptop that reconnects and re-sends
    #: its backlog must not double-count.
    event_id = Column(String(255), nullable=False)

    collector_id = Column(String(255), nullable=True)
    device_id = Column(String(128), nullable=True)
    device_name = Column(String(255), nullable=True)

    #: The agent client (`claude_code`, `cursor`, …).
    client = Column(String(64), nullable=False)
    #: The client's own session id. The correlation handle for multi-turn work
    #: — coding agents emit one conventionally, and it is the only way to tell
    #: "forty tool calls in one task" from "forty separate tasks".
    session_id = Column(String(255), nullable=True)

    tool_name = Column(String(255), nullable=False)
    tool_kind = Column(String(16), nullable=False, server_default=KIND_BUILTIN)
    #: Populated for `mcp__<server>__<tool>` — joinable to discovered assets.
    mcp_server = Column(String(255), nullable=True)

    phase = Column(String(8), nullable=False)
    decision = Column(String(16), nullable=True)

    occurred_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=True)
    #: Process exit status for `post` records. NULL on `pre`.
    exit_status = Column(Integer, nullable=True)
    #: Coarse workspace label (a directory basename), never a full path — a
    #: path is user data and adds nothing the estate picture needs.
    project = Column(String(255), nullable=True)

    source = Column(
        String(32), nullable=False, server_default=SOURCE_ENDPOINT,
        comment="Provenance: 'endpoint' (device-observed). Never governed.",
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # At-least-once delivery makes this the load-bearing constraint.
        UniqueConstraint(
            "tenant_id", "event_id", name="uix_observed_agent_events_tenant_event"
        ),
        Index("ix_observed_agent_events_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_observed_agent_events_tenant_device", "tenant_id", "device_id"),
        Index("ix_observed_agent_events_tenant_session", "tenant_id", "session_id"),
        # "Which ungoverned MCP servers are actually being called?" — the
        # question that turns an inventory row into a priority.
        Index("ix_observed_agent_events_tenant_mcp", "tenant_id", "mcp_server"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "event_id": self.event_id,
            "collector_id": self.collector_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "client": self.client,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_kind": self.tool_kind,
            "mcp_server": self.mcp_server,
            "phase": self.phase,
            "decision": self.decision,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "duration_ms": self.duration_ms,
            "exit_status": self.exit_status,
            "project": self.project,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
