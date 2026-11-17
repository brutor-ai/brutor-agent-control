# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# MCP Capability Snapshot model

Append-only history of every successful capability discovery. The
diff service compares the newest snapshot to the previous one to
detect tool-set drift (new tools, removed tools, description
changes) and raises a system alert when drift is found.

See migration `a048_capability_snapshots` for the schema rationale. A row is appended
only when the declared contract changed (`content_hash` differs from the
latest row's), and `is_change` says whether the row is a semantic change
against its predecessor — the core's drift cause attribution counts only
those (a first-ever snapshot is a baseline, not a change).
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, JSON, String, text,
)
from sqlalchemy.sql import func

from app.database import Base


# Trigger values stored in the `trigger` column. String literals so
# operators reading proxy_logs see the exact reason without needing
# a code lookup.
TRIGGER_DISCOVERY = "discovery"           # routine periodic / admin-triggered
TRIGGER_MANUAL_REFRESH = "manual_refresh" # operator clicked re-discover
TRIGGER_STARTUP = "startup"               # gateway boot capability sync


class MCPCapabilitySnapshot(Base):
    """One immutable row per successful capability discovery."""

    __tablename__ = "mcp_capability_snapshots"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    mcp_server_id = Column(
        String(255),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    capabilities = Column(JSON, nullable=False)
    trigger = Column(String(32), nullable=True)
    # sha256 of tool_snapshot.canonical_contract(capabilities).
    content_hash = Column(String(64), nullable=True)
    # True iff the semantic diff against the predecessor is non-empty.
    is_change = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    __table_args__ = (
        Index(
            "ix_mcp_capability_snapshots_server_time",
            "mcp_server_id", "snapshot_at",
        ),
        Index(
            "ix_mcp_capability_snapshots_tenant_time",
            "tenant_id", "snapshot_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "mcp_server_id": self.mcp_server_id,
            "snapshot_at": self.snapshot_at.isoformat() if self.snapshot_at else None,
            "capabilities": self.capabilities or {},
            "trigger": self.trigger,
            "content_hash": self.content_hash,
            "is_change": bool(self.is_change),
        }

    def __repr__(self) -> str:
        return (
            f"<MCPCapabilitySnapshot(id={self.id!r}, server={self.mcp_server_id!r}, "
            f"at={self.snapshot_at!r}, trigger={self.trigger!r})>"
        )
