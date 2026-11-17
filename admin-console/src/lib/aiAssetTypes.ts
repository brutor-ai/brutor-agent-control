/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * AI Assets governance types — mirrors the control-plane shape served
 * by `/v1/admin/assets`. The AI Asset Registry is the auto-discovered
 * inventory of every governed AI asset (models, providers, routing
 * groups, MCP servers, skills, agent cards) with a governance overlay
 * (owner, lifecycle stage, risk tier, enforcement mode, tags).
 */

export type AssetKind =
    | 'llm_model'
    | 'llm_provider'
    | 'routing_group'
    | 'mcp_server'
    | 'skill'
    | 'agent_card'
    | 'ai_system'
    | 'agent_identity';

export type LifecycleStage =
    | 'proposed'
    | 'approved'
    | 'active'
    | 'deprecated'
    | 'retired';

/** Nullable on the wire — an asset may have no assigned risk tier yet. */
export type RiskTier = 'minimal' | 'limited' | 'high' | 'unacceptable';

export type EnforcementMode = 'off' | 'warn' | 'enforce';

export type AssetStatus = 'ok' | 'review_due' | 'flagged';

export type AssetSource = 'discovered' | 'manual';

export interface AIAsset {
    id: string;
    tenant_id: string;
    asset_kind: AssetKind;
    ref_id: string;
    display_name?: string | null;
    owner?: string | null;
    lifecycle_stage: LifecycleStage;
    risk_tier?: RiskTier | null;
    intended_use?: string | null;
    /** GPAI track (llm_model assets): Art 53/55 obligations
     *  attach to the MODEL, on its own track. */
    gpai_classification?: 'gpai' | 'gpai_systemic_risk' | null;
    /** Who operates this constituent. third_party marks the
     *  supply-chain relationships you have obligations over but no
     *  operational control of. */
    operated_by?: 'self' | 'third_party' | null;
    /** Derived edge view: the AI Systems whose declared boundary
     *  (direct bindings) includes this component asset. */
    owning_systems?: { id: string; name: string; eu_ai_act_risk_tier?: RiskTier | null }[];
    /** Worst tier among owning systems — components derive risk, never
     *  store it (Annex III classifies by system purpose). */
    derived_risk_tier?: RiskTier | null;
    enforcement_mode: EnforcementMode;
    tags: string[];
    status: AssetStatus;
    source: AssetSource;
    /**
     * Per-asset icon. Three shapes from the backend:
     *   - a normal URL (provider/model/agent_card brand mark, MCP server
     *     icon endpoint) → rendered as <img>;
     *   - a `lucide:{name}` sentinel (skills) → resolved to a Lucide
     *     component via `lucideIconMap`;
     *   - null/empty → neutral kind-based fallback.
     */
    icon_url?: string | null;
    /** Whether the backing source/integration is currently enabled. */
    source_enabled?: boolean | null;
    /**
     * Observability-only: known for cost/usage visibility but NOT governed
     * inline by the gateway (an LLM model discovered via Traffic Data Import
     * that hasn't been promoted/enabled). These are "Observed", not "Governed".
     */
    observed?: boolean | null;
    created_at?: string | null;
    updated_at?: string | null;
}

export interface AIAssetCreateRequest {
    asset_kind: AssetKind;
    ref_id: string;
    display_name?: string | null;
    owner?: string | null;
    lifecycle_stage?: LifecycleStage;
    risk_tier?: RiskTier | null;
    gpai_classification?: 'none' | 'gpai' | 'gpai_systemic_risk' | null;
    operated_by?: 'self' | 'third_party' | null;
    intended_use?: string | null;
    enforcement_mode?: EnforcementMode;
    tags?: string[];
}

export interface AIAssetSyncResponse {
    created: number;
    updated: number;
    orphaned: number;
    total: number;
}

export const ASSET_KIND_LABEL: Record<AssetKind, string> = {
    llm_model: 'LLM Model',
    llm_provider: 'LLM Provider',
    routing_group: 'Routing Group',
    mcp_server: 'MCP Server',
    skill: 'Skill',
    agent_card: 'Agent Card',
    ai_system: 'AI System',
    agent_identity: 'Agent Identity',
};

export const LIFECYCLE_STAGE_LABEL: Record<LifecycleStage, string> = {
    proposed: 'Proposed',
    approved: 'Approved',
    active: 'Active',
    deprecated: 'Deprecated',
    retired: 'Retired',
};

export const RISK_TIER_LABEL: Record<RiskTier, string> = {
    minimal: 'Minimal',
    limited: 'Limited',
    high: 'High',
    unacceptable: 'Unacceptable',
};

export const ENFORCEMENT_MODE_LABEL: Record<EnforcementMode, string> = {
    off: 'Off',
    warn: 'Warn',
    enforce: 'Enforce',
};

export const STATUS_LABEL: Record<AssetStatus, string> = {
    ok: 'OK',
    review_due: 'Review due',
    flagged: 'Flagged',
};
