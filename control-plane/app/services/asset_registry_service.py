# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Asset Registry service

Business logic for the AI Asset inventory. All methods are sync, take an
explicit `db: Session` (the endpoint hands over its `Depends(get_db)`
session — this manager never opens or commits its own transaction; the
commit is owned by `get_db`), and scope every query by tenant.

The headline operation is `sync_tenant` (auto-discovery): it walks the
six source config tables and upserts one `ai_assets` row per discovered
resource. The contract operators rely on:

  * A newly-discovered resource → a fresh row with source="discovered",
    display_name seeded from the source, lifecycle_stage="active",
    status="ok".
  * An already-tracked resource → only `display_name` is refreshed; the
    operator's governance fields (owner / lifecycle_stage / risk_tier /
    intended_use / enforcement_mode / tags / status) are left untouched.
  * A discovered asset whose underlying config row has vanished → flagged
    (status="flagged"), never auto-deleted. Orphans stay visible for
    review.

Domain errors raise `AssetRegistryError(code, message)`; the endpoint
layer maps `code` to an HTTP status (404 / 409 / 400).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_identity import AgentIdentity
from app.database.models.resource_group import ResourceGroup
from app.database.models.agent_identity import ResourceGroupAgentMember
from app.database.models.agent_skill import resource_group_agent_skills
from app.database.models.resource_group import (
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.ai_asset import (
    ASSET_KIND_AGENT_CARD,
    ASSET_KIND_AGENT_IDENTITY,
    ASSET_KIND_AI_SYSTEM,
    ASSET_KIND_AGENT_IDENTITY,
    ASSET_KIND_SKILL,
    GPAI_CLASSIFICATIONS,
    GPAI_NONE,
    OPERATED_BY,
    ASSET_KIND_LLM_MODEL,
    ASSET_KIND_LLM_PROVIDER,
    ASSET_KIND_MCP_SERVER,
    ASSET_KIND_ROUTING_GROUP,
    ASSET_KIND_SKILL,
    ASSET_KINDS,
    ENFORCEMENT_MODES,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_STAGES,
    RISK_TIERS,
    SOURCE_DISCOVERED,
    SOURCE_MANUAL,
    STATUS_FLAGGED,
    STATUS_OK,
    STATUSES,
    AiAsset,
)
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer


# Discovery map: (asset_kind, source model, display column attribute,
# enabled column attribute or None, opts). The sync iterates this so adding a
# kind is a one-line change. `enabled_attr` is None for kinds whose source
# row has no enabled flag (routing_group / skill). `opts` (optional dict):
#   filter    — extra SQLAlchemy predicate on the source query (e.g. only
#               resource groups of type ai_system).
#   risk_attr — source attribute to SEED risk_tier from on first discovery
#               (governance fields stay operator-owned after that).
#   owner_attr— source attribute to seed owner from on first discovery.
_DISCOVERY_SOURCES = [
    (ASSET_KIND_LLM_MODEL, LLModel, "name", "enabled"),
    (ASSET_KIND_LLM_PROVIDER, LlmProvider, "display_name", "enabled"),
    (ASSET_KIND_ROUTING_GROUP, LLMRoutingGroup, "name", None),
    (ASSET_KIND_MCP_SERVER, MCPServer, "name", "enabled"),
    (ASSET_KIND_SKILL, AgentSkill, "name", None),
    (ASSET_KIND_AGENT_CARD, AgentCard, "name", "enabled"),
    # The AI System itself — a resource group of type ai_system — is the
    # auditor-legible inventory unit (Design Note 001). Seed its risk tier from the
    # RG's EU AI Act tier.
    (ASSET_KIND_AI_SYSTEM, ResourceGroup, "display_name", "is_active",
     {"filter": ResourceGroup.group_type == "ai_system",
      "risk_attr": "eu_ai_act_risk_tier"}),
    # The acting agent principal.
    (ASSET_KIND_AGENT_IDENTITY, AgentIdentity, "name", "enabled",
     {"owner_attr": "owner_label"}),
]


class AssetRegistryError(Exception):
    """Service-level error. Caught by the endpoint layer and translated
    into an HTTP 4xx response with a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class AssetRegistryService:
    """CRUD + auto-discovery sync for the AI Asset Registry."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("asset-registry-service")

    # =====================================================================
    # Read
    # =====================================================================

    def list_assets(
        self,
        db: Session,
        tenant_id: str,
        *,
        asset_kind: Optional[str] = None,
        lifecycle_stage: Optional[str] = None,
        risk_tier: Optional[str] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AiAsset], int]:
        """Paginated, filtered list. Returns (rows, total).

        `q` is a case-insensitive substring match on display_name OR
        ref_id. Filter values are validated against their vocabularies so
        a typo returns a tidy 400 rather than silently matching nothing.
        """
        where = [AiAsset.tenant_id == tenant_id]

        if asset_kind is not None:
            self._require_in(asset_kind, ASSET_KINDS, "asset_kind")
            where.append(AiAsset.asset_kind == asset_kind)
        if lifecycle_stage is not None:
            self._require_in(lifecycle_stage, LIFECYCLE_STAGES, "lifecycle_stage")
            where.append(AiAsset.lifecycle_stage == lifecycle_stage)
        if risk_tier is not None:
            self._require_in(risk_tier, RISK_TIERS, "risk_tier")
            where.append(AiAsset.risk_tier == risk_tier)
        if status is not None:
            self._require_in(status, STATUSES, "status")
            where.append(AiAsset.status == status)
        if q:
            needle = f"%{q.lower()}%"
            where.append(
                func.lower(func.coalesce(AiAsset.display_name, "")).like(needle)
                | func.lower(AiAsset.ref_id).like(needle)
            )

        total = db.execute(
            select(func.count()).select_from(AiAsset).where(and_(*where))
        ).scalar_one()

        rows = db.execute(
            select(AiAsset)
            .where(and_(*where))
            .order_by(AiAsset.asset_kind.asc(), AiAsset.created_at.asc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()
        return list(rows), int(total or 0)

    def get_asset(self, db: Session, tenant_id: str, asset_id: str) -> AiAsset:
        """Tenant-scoped fetch. Raises 404 when missing or cross-tenant."""
        asset = db.execute(
            select(AiAsset).where(
                AiAsset.id == asset_id,
                AiAsset.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if asset is None:
            raise AssetRegistryError(
                "not_found",
                f"AI asset {asset_id!r} not found in tenant",
            )
        return asset

    # =====================================================================
    # Write
    # =====================================================================

    def create_asset(
        self,
        db: Session,
        tenant_id: str,
        *,
        asset_kind: str,
        ref_id: str,
        display_name: Optional[str] = None,
        owner: Optional[str] = None,
        lifecycle_stage: Optional[str] = None,
        risk_tier: Optional[str] = None,
        intended_use: Optional[str] = None,
        gpai_classification: Optional[str] = None,
        operated_by: Optional[str] = None,
        enforcement_mode: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source: str = SOURCE_MANUAL,
    ) -> AiAsset:
        """Create an asset. Validates vocabularies, 409 on a duplicate
        (tenant, asset_kind, ref_id)."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._require_in(asset_kind, ASSET_KINDS, "asset_kind")
        ref_id = (ref_id or "").strip()
        if not ref_id:
            raise AssetRegistryError("invalid_ref_id", "ref_id must not be empty")
        if lifecycle_stage is not None:
            self._require_in(lifecycle_stage, LIFECYCLE_STAGES, "lifecycle_stage")
        if risk_tier is not None:
            if asset_kind != ASSET_KIND_AI_SYSTEM:
                raise AssetRegistryError(
                    "risk_tier_system_only",
                    "risk_tier is a property of the AI System (Annex III "
                    "classifies by purpose). Component assets derive their "
                    "exposure from the systems that bind them.",
                )
            self._require_in(risk_tier, RISK_TIERS, "risk_tier")
        if gpai_classification is not None:
            if asset_kind != ASSET_KIND_LLM_MODEL:
                raise AssetRegistryError(
                    "gpai_llm_model_only",
                    "gpai_classification applies to llm_model assets — the "
                    "Art 53–55 track attaches to the model, not the system.",
                )
            self._require_in(
                gpai_classification, GPAI_CLASSIFICATIONS, "gpai_classification"
            )
            if gpai_classification == GPAI_NONE:
                gpai_classification = None
        if operated_by is not None:
            self._require_in(operated_by, OPERATED_BY, "operated_by")
        if enforcement_mode is not None:
            self._require_in(enforcement_mode, ENFORCEMENT_MODES, "enforcement_mode")

        existing = db.execute(
            select(AiAsset.id).where(
                AiAsset.tenant_id == tenant_id,
                AiAsset.asset_kind == asset_kind,
                AiAsset.ref_id == ref_id,
            )
        ).first()
        if existing:
            raise AssetRegistryError(
                "duplicate",
                f"AI asset for ({asset_kind}, {ref_id}) already exists in tenant",
            )

        asset = AiAsset(
            id=f"aiasset-{ULID()}",
            tenant_id=tenant_id,
            asset_kind=asset_kind,
            ref_id=ref_id,
            display_name=display_name,
            owner=owner,
            lifecycle_stage=lifecycle_stage or LIFECYCLE_ACTIVE,
            risk_tier=risk_tier,
            intended_use=intended_use,
            gpai_classification=gpai_classification,
            operated_by=operated_by,
            enforcement_mode=enforcement_mode if enforcement_mode is not None else "off",
            tags=tags,
            status=STATUS_OK,
            source=source,
        )
        db.add(asset)
        db.flush()
        self.logger.info(
            f"Created AI asset {asset.id} ({asset_kind}:{ref_id}) "
            f"in tenant {tenant_id} (source={source})"
        )
        return asset

    def update_asset(
        self,
        db: Session,
        tenant_id: str,
        asset_id: str,
        *,
        display_name: Optional[str] = None,
        owner: Optional[str] = None,
        lifecycle_stage: Optional[str] = None,
        risk_tier: Optional[str] = None,
        intended_use: Optional[str] = None,
        gpai_classification: Optional[str] = None,
        operated_by: Optional[str] = None,
        enforcement_mode: Optional[str] = None,
        tags: Optional[List[str]] = None,
        status: Optional[str] = None,
    ) -> AiAsset:
        """Partial update of governance fields. Only provided fields are
        set. 404 if the asset is missing."""
        asset = self.get_asset(db, tenant_id, asset_id)

        if display_name is not None:
            asset.display_name = display_name
        if owner is not None:
            asset.owner = owner
        if lifecycle_stage is not None:
            self._require_in(lifecycle_stage, LIFECYCLE_STAGES, "lifecycle_stage")
            asset.lifecycle_stage = lifecycle_stage
        if risk_tier is not None:
            # Annex III classifies by SYSTEM purpose. A risk class on a
            # model or MCP server in the abstract is meaningless — the
            # editable tier lives on ai_system rows only; component rows
            # DERIVE theirs from the systems that bind them.
            if asset.asset_kind != ASSET_KIND_AI_SYSTEM:
                raise AssetRegistryError(
                    "risk_tier_system_only",
                    "risk_tier is a property of the AI System (Annex III "
                    "classifies by purpose). Component assets derive their "
                    "exposure from the systems that bind them.",
                )
            self._require_in(risk_tier, RISK_TIERS, "risk_tier")
            asset.risk_tier = risk_tier
        if gpai_classification is not None:
            if asset.asset_kind != ASSET_KIND_LLM_MODEL:
                raise AssetRegistryError(
                    "gpai_llm_model_only",
                    "gpai_classification applies to llm_model assets — the "
                    "Art 53–55 track attaches to the model, not the system.",
                )
            self._require_in(
                gpai_classification, GPAI_CLASSIFICATIONS, "gpai_classification"
            )
            # 'none' is the explicit not-GPAI assertion; store NULL as the
            # unassessed state so the two stay distinguishable in filters.
            asset.gpai_classification = (
                None if gpai_classification == GPAI_NONE else gpai_classification
            )
        if operated_by is not None:
            self._require_in(operated_by, OPERATED_BY, "operated_by")
            asset.operated_by = operated_by
        if intended_use is not None:
            asset.intended_use = intended_use
        if enforcement_mode is not None:
            self._require_in(enforcement_mode, ENFORCEMENT_MODES, "enforcement_mode")
            asset.enforcement_mode = enforcement_mode
        if tags is not None:
            asset.tags = tags
        if status is not None:
            self._require_in(status, STATUSES, "status")
            asset.status = status

        db.flush()
        return asset

    def delete_asset(self, db: Session, tenant_id: str, asset_id: str) -> None:
        """Hard-delete. 404 if missing."""
        asset = self.get_asset(db, tenant_id, asset_id)
        db.delete(asset)
        db.flush()
        self.logger.info(
            f"Deleted AI asset {asset_id} in tenant {tenant_id}"
        )

    # =====================================================================
    # Auto-discovery sync
    # =====================================================================

    def sync_tenant(self, db: Session, tenant_id: str) -> Dict[str, int]:
        """Reconcile the registry against the source config tables.

        Returns {created, updated, orphaned, total} where `total` is the
        current count of ai_assets rows in the tenant after the sync.
        """
        created = 0
        updated = 0

        # Index the existing rows once, keyed by (asset_kind, ref_id), so
        # the per-source loop is O(rows) not O(rows × sources).
        existing_rows = db.execute(
            select(AiAsset).where(AiAsset.tenant_id == tenant_id)
        ).scalars().all()
        by_key: Dict[Tuple[str, str], AiAsset] = {
            (a.asset_kind, a.ref_id): a for a in existing_rows
        }

        # Track which (kind, ref_id) keys are still backed by a source row
        # so we can flag the orphans afterwards.
        live_keys: set[Tuple[str, str]] = set()

        # Per-source provider-icon cache keyed by provider_id so the
        # llm_model icon resolution doesn't fire an N+1 of provider reads.
        provider_icon_cache: Dict[str, Optional[str]] = {}

        for entry in _DISCOVERY_SOURCES:
            asset_kind, model, display_attr, enabled_attr = entry[:4]
            opts = entry[4] if len(entry) > 4 else {}
            stmt = select(model).where(model.tenant_id == tenant_id)
            if opts.get("filter") is not None:
                stmt = stmt.where(opts["filter"])
            source_rows = db.execute(stmt).scalars().all()
            for row in source_rows:
                ref_id = str(row.id)
                key = (asset_kind, ref_id)
                live_keys.add(key)
                display_name = getattr(row, display_attr, None)
                source_enabled = (
                    getattr(row, enabled_attr, None)
                    if enabled_attr else None
                )
                # Observability-only: an LLM model discovered via Traffic Data
                # Import (source='import') that hasn't been promoted/enabled is
                # tracked for cost/usage but NOT governed inline. Once enabled,
                # it's proxied → governed, so observed flips back to False.
                observed = bool(
                    asset_kind == ASSET_KIND_LLM_MODEL
                    and getattr(row, "source", None) == "import"
                    and source_enabled is False
                )
                icon_url = self._resolve_icon_url(
                    db, asset_kind, row, provider_icon_cache
                )

                existing = by_key.get(key)
                if existing is None:
                    # Seed governance fields from the source ONLY on first
                    # discovery (thereafter operator-owned, never stomped).
                    seed_risk = (
                        getattr(row, opts["risk_attr"], None)
                        if opts.get("risk_attr") else None
                    ) or None
                    seed_owner = (
                        getattr(row, opts["owner_attr"], None)
                        if opts.get("owner_attr") else None
                    ) or None
                    asset = AiAsset(
                        id=f"aiasset-{ULID()}",
                        tenant_id=tenant_id,
                        asset_kind=asset_kind,
                        ref_id=ref_id,
                        display_name=display_name,
                        icon_url=icon_url,
                        source_enabled=source_enabled,
                        observed=observed,
                        lifecycle_stage=LIFECYCLE_ACTIVE,
                        status=STATUS_OK,
                        source=SOURCE_DISCOVERED,
                        risk_tier=seed_risk,
                        owner=seed_owner,
                    )
                    db.add(asset)
                    by_key[key] = asset
                    created += 1
                else:
                    # Refresh sync-owned presentation fields only — never
                    # stomp operator-set governance fields. Count an update
                    # when any of them actually changed.
                    changed = False
                    if existing.display_name != display_name:
                        existing.display_name = display_name
                        changed = True
                    if existing.icon_url != icon_url:
                        existing.icon_url = icon_url
                        changed = True
                    if existing.source_enabled != source_enabled:
                        existing.source_enabled = source_enabled
                        changed = True
                    if bool(existing.observed) != observed:
                        existing.observed = observed
                        changed = True
                    if changed:
                        updated += 1

        # Flag orphans: discovered rows whose source no longer exists.
        # Manual rows are left alone — they have no source to vanish.
        orphaned = 0
        for (asset_kind, ref_id), asset in by_key.items():
            if (asset_kind, ref_id) in live_keys:
                continue
            if asset.source != SOURCE_DISCOVERED:
                continue
            if asset.status != STATUS_FLAGGED:
                asset.status = STATUS_FLAGGED
            orphaned += 1

        db.flush()

        total = db.execute(
            select(func.count()).select_from(AiAsset).where(
                AiAsset.tenant_id == tenant_id
            )
        ).scalar_one()

        self.logger.info(
            f"AI asset sync for tenant {tenant_id}: "
            f"+{created} ~{updated} orphaned={orphaned} total={total}"
        )
        return {
            "created": created,
            "updated": updated,
            "orphaned": orphaned,
            "total": int(total or 0),
        }

    # =====================================================================
    # Internals
    # =====================================================================

    def owning_systems_map(
        self, db: Session, tenant_id: str, assets: List[AiAsset]
    ) -> Dict[str, List[Dict[str, Optional[str]]]]:
        """{asset_id: [{id, name, eu_ai_act_risk_tier}]} — the AI Systems
        whose declared boundary (direct resource bindings) includes each
        component asset. Batched: one query per asset kind present.

        Annex III classifies by system purpose, so this is what a
        component's risk exposure derives from; a flat per-asset tier
        cannot represent one model serving a high-risk and a minimal-risk
        system at once, but this edge list can.
        """
        from app.database.models.resource_group import GroupType, ResourceGroup

        kind_edges = {
            ASSET_KIND_LLM_MODEL: (
                resource_group_llm_models.c.resource_group_id,
                resource_group_llm_models.c.llm_model_id,
            ),
            ASSET_KIND_MCP_SERVER: (
                resource_group_server_configs.c.resource_group_id,
                resource_group_server_configs.c.server_config_id,
            ),
            ASSET_KIND_SKILL: (
                resource_group_agent_skills.c.resource_group_id,
                resource_group_agent_skills.c.agent_skill_id,
            ),
            ASSET_KIND_AGENT_IDENTITY: (
                ResourceGroupAgentMember.group_id,
                ResourceGroupAgentMember.agent_id,
            ),
        }

        by_kind: Dict[str, List[AiAsset]] = {}
        for a in assets:
            if a.asset_kind in kind_edges:
                by_kind.setdefault(a.asset_kind, []).append(a)

        out: Dict[str, List[Dict[str, Optional[str]]]] = {}
        for kind, kind_assets in by_kind.items():
            group_col, ref_col = kind_edges[kind]
            ref_ids = [a.ref_id for a in kind_assets]
            rows = db.execute(
                select(
                    ref_col,
                    ResourceGroup.id,
                    ResourceGroup.display_name,
                    ResourceGroup.name,
                    ResourceGroup.eu_ai_act_risk_tier,
                )
                .join(ResourceGroup, ResourceGroup.id == group_col)
                .where(
                    ref_col.in_(ref_ids),
                    ResourceGroup.tenant_id == tenant_id,
                    ResourceGroup.group_type == GroupType.AI_SYSTEM,
                    ResourceGroup.is_active.is_(True),
                )
            ).all()
            by_ref: Dict[str, List[Dict[str, Optional[str]]]] = {}
            for ref_id, gid, disp, name, tier in rows:
                by_ref.setdefault(str(ref_id), []).append(
                    {"id": gid, "name": disp or name, "eu_ai_act_risk_tier": tier}
                )
            for a in kind_assets:
                out[a.id] = by_ref.get(a.ref_id, [])
        return out

    #: Worst-first over the Annex-aligned tiers, for the derived chip.
    _RISK_RANK = {"unacceptable": 3, "high": 2, "limited": 1, "minimal": 0}

    @classmethod
    def derived_risk_tier(
        cls, owning: List[Dict[str, Optional[str]]]
    ) -> Optional[str]:
        tiers = [
            o["eu_ai_act_risk_tier"]
            for o in owning
            if o.get("eu_ai_act_risk_tier") in cls._RISK_RANK
        ]
        if not tiers:
            return None
        return max(tiers, key=lambda t: cls._RISK_RANK[t])

    @staticmethod
    def _resolve_icon_url(
        db: Session,
        asset_kind: str,
        row: Any,
        provider_icon_cache: Dict[str, Optional[str]],
    ) -> Optional[str]:
        """Best-effort icon URL for a discovered asset, by kind:

          * llm_model    → its catalog_id → llm_catalog.icon_url, falling
                            back to the row's provider icon.
          * llm_provider → the provider row's icon_url.
          * agent_card   → the card's own icon_url.
          * mcp_server   → the served icon-blob URL
                            (`/v1/assets/mcp-servers/{id}/icon`) when the
                            server has an uploaded/seeded icon, else None.
          * skill        → a `lucide:{name}` sentinel from the skill's
                            `icon_name` metadata that the UI maps to a
                            Lucide glyph (skills have no icon blob).
          * routing_group → None.

        Provider icons are memoised per provider_id to avoid an N+1.
        """
        if asset_kind == ASSET_KIND_LLM_PROVIDER:
            return AssetRegistryService._provider_icon(row)

        if asset_kind == ASSET_KIND_AGENT_CARD:
            return getattr(row, "icon_url", None)

        if asset_kind == ASSET_KIND_MCP_SERVER:
            # An explicit external icon_url wins; otherwise advertise the
            # CONTROL-PLANE icon-blob endpoint when the server has bytes.
            # This MUST be the control-plane path
            # (`/v1/admin/mcp-servers/{id}/icon`, public for <img>), NOT the
            # Rust-core `/v1/assets/...` path — the Asset Registry UI loads
            # icons from the control plane, so a /v1/assets URL would not
            # resolve. The UI prefixes a leading-slash path with its
            # gateway base URL.
            explicit = getattr(row, "icon_url", None)
            if explicit:
                return explicit
            has_icon = getattr(row, "has_icon", None)
            server_id = getattr(row, "id", None)
            if callable(has_icon) and has_icon() and server_id:
                return f"/v1/admin/mcp-servers/{server_id}/icon"
            return None

        if asset_kind == ASSET_KIND_SKILL:
            # Skills carry no icon blob — they reference a Lucide icon by
            # name in their metadata. Emit a `lucide:{name}` sentinel the
            # UI maps to the corresponding glyph.
            get_meta = getattr(row, "get_metadata_value", None)
            icon_name = get_meta("icon_name") if callable(get_meta) else None
            if icon_name:
                return f"lucide:{icon_name}"
            return None

        if asset_kind == ASSET_KIND_LLM_MODEL:
            catalog_id = getattr(row, "catalog_id", None)
            if catalog_id:
                entry = db.get(LlmCatalog, catalog_id)
                if entry is not None and entry.icon_url:
                    return entry.icon_url
            provider_id = getattr(row, "provider_id", None)
            if provider_id is None:
                return None
            if provider_id not in provider_icon_cache:
                prov = db.get(LlmProvider, provider_id)
                provider_icon_cache[provider_id] = (
                    AssetRegistryService._provider_icon(prov)
                    if prov is not None
                    else None
                )
            return provider_icon_cache[provider_id]

        return None

    @staticmethod
    def _provider_icon(prov: Any) -> Optional[str]:
        """Reachable icon URL for an LLM provider: an explicit external
        `icon_url` if set, else the control-plane icon-blob endpoint
        (`/v1/admin/providers/{id}/icon`, public for <img>) when the
        provider has uploaded/seeded icon bytes, else None. Seeded
        providers carry blob icons (from the bundled SVGs) but often no
        `icon_url` string, so the blob endpoint is what actually renders."""
        if prov is None:
            return None
        explicit = getattr(prov, "icon_url", None)
        if explicit:
            return explicit
        has_icon = getattr(prov, "has_icon", None)
        prov_id = getattr(prov, "id", None)
        if callable(has_icon) and has_icon() and prov_id:
            return f"/v1/admin/providers/{prov_id}/icon"
        return None

    @staticmethod
    def _require_in(value: str, allowed: frozenset, field: str) -> None:
        if value not in allowed:
            raise AssetRegistryError(
                "invalid_value",
                f"Invalid {field} {value!r}; expected one of {sorted(allowed)}",
            )


__all__ = [
    "AssetRegistryService",
    "AssetRegistryError",
]
