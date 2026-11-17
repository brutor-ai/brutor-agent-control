# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Agent Registry — one governance namespace over every AI resource.

`ai_assets` is already the canonical governed inventory (spans llm_model /
llm_provider / routing_group / mcp_server / skill / agent_card via
`(asset_kind, ref_id)`, with risk/lifecycle/enforcement/tags). This service
joins that governed inventory with the **discovered-but-ungoverned** inventory
(`discovered_assets`) into a single coverage view — the honest "N AI assets
seen, M governed" namespace Google's Agent Registry surfaces — and enriches the
governed agent/MCP rows with version + capability metadata.

Read-model only; reuses `AssetRegistryService` + the discovery tables.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill
from app.database.models.discovery import DiscoveredAsset
from app.database.models.mcp_server import MCPServer
from app.services.asset_registry_service import AssetRegistryService


class RegistryService:
    """Unified governed + discovered coverage view."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("registry-service")
        self.assets = AssetRegistryService(self.logger)

    def coverage(self, db: Session, tenant_id: str) -> Dict[str, Any]:
        all_rows, total = self.assets.list_assets(db, tenant_id, limit=1000)
        all_dicts = [r.to_dict() for r in all_rows]
        self._enrich(db, tenant_id, all_rows, all_dicts)

        # Three planes, not two:
        #   * governed   — configured AND enforced inline by the gateway.
        #   * observed   — known for cost/usage visibility but NOT enforced
        #                  (e.g. an LLM model imported from vendor traffic that
        #                  hasn't been promoted). Observability, not governance.
        #   * discovered — out-of-band shadow AI (the discovery plane).
        governed = [d for d in all_dicts if not d.get("observed")]
        observed = [d for d in all_dicts if d.get("observed")]

        # Discovered, not-yet-governed assets (the out-of-band coverage gap).
        discovered_rows = db.execute(
            select(DiscoveredAsset).where(
                DiscoveredAsset.tenant_id == tenant_id,
                DiscoveredAsset.governance_status != "governed",
                DiscoveredAsset.status == "active",
            ).order_by(DiscoveredAsset.last_seen.desc())
        ).scalars().all()
        discovered = [r.to_dict() for r in discovered_rows]

        return {
            "governed": governed,
            "observed": observed,
            "discovered_ungoverned": discovered,
            "coverage": {
                "governed": len(governed),
                "observed": len(observed),
                "ungoverned": len(discovered),
                "total_seen": total + len(discovered),
            },
        }

    def _enrich(
        self, db: Session, tenant_id: str, rows: List[Any], dicts: List[Dict[str, Any]]
    ) -> None:
        """Add `version` + `capability_count` to governed agent_card / mcp_server
        / skill rows via batched per-kind lookups (no N+1)."""
        by_kind: Dict[str, List[str]] = {}
        for r in rows:
            by_kind.setdefault(r.asset_kind, []).append(r.ref_id)

        meta: Dict[str, Dict[str, Any]] = {}  # ref_id → {version, capability_count}

        card_ids = by_kind.get("agent_card", [])
        if card_ids:
            for c in db.execute(
                select(AgentCard).where(
                    AgentCard.tenant_id == tenant_id, AgentCard.id.in_(card_ids)
                )
            ).scalars().all():
                caps = c.card_json.get("skills") if isinstance(c.card_json, dict) else None
                meta[c.id] = {
                    "version": c.version,
                    "protocol_version": c.protocol_version,
                    "capability_count": len(caps) if isinstance(caps, list) else None,
                }

        mcp_ids = by_kind.get("mcp_server", [])
        if mcp_ids:
            for s in db.execute(
                select(MCPServer).where(
                    MCPServer.tenant_id == tenant_id, MCPServer.id.in_(mcp_ids)
                )
            ).scalars().all():
                tools = None
                if isinstance(s.capabilities, dict):
                    t = s.capabilities.get("tools")
                    tools = len(t) if isinstance(t, list) else None
                meta[s.id] = {"version": None, "capability_count": tools}

        skill_ids = by_kind.get("skill", [])
        if skill_ids:
            for sk in db.execute(
                select(AgentSkill).where(
                    AgentSkill.tenant_id == tenant_id, AgentSkill.id.in_(skill_ids)
                )
            ).scalars().all():
                meta[sk.id] = {"version": getattr(sk, "version", None), "capability_count": None}

        for d in dicts:
            m = meta.get(d.get("ref_id"))
            d["version"] = m.get("version") if m else None
            d["capability_count"] = m.get("capability_count") if m else None
            if m and m.get("protocol_version"):
                d["protocol_version"] = m["protocol_version"]
