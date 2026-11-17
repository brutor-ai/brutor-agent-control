/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Types for the Agent Identity admin surface — agents as governable
 * principals, their per-action grants (consumed by the Rust runtime authz
 * engine), and their resource-group memberships. Brutor is the decision/
 * enforce/evidence plane; issuance stays with the IdP.
 */

import type { components } from './generated/control-plane-api';

export type AgentSource = 'manual' | 'idp' | 'discovered';
export type AgentStatus = 'active' | 'suspended';
export type GrantActionType = 'llm_call' | 'mcp_tool' | 'a2a_call' | 'skill_exec';
export type GrantEffect = 'allow' | 'deny' | 'approval_required';

/**
 * Aliased to the generated schema instead of re-declared.
 *
 * Several fields are optional on the wire, and a call site that dereferences
 * one without a guard throws on a response that legitimately omits the key.
 * A hand-written response interface is an assertion about what the server
 * sends and nothing verifies it; the generated type is derived from the
 * Control Plane's own response model, so a server-side rename or optionality
 * change breaks the build instead of the page.
 */
export type AgentIdentity = components['schemas']['AgentIdentityResponse'];

// ── Agent implementation identity ────────────────────────────
//
// The credential decides WHO acts; these describe WHAT software acted and
// WHICH release of it. They never decide access.

/** `verified` = a claim inside a verified workload token; `declared` =
 *  self-declared by the agent (MCP clientInfo, OpenTelemetry baggage or the
 *  X-Brutor-Agent-* headers). */
export type AgentReleaseTrust = 'verified' | 'declared';

/** The implementation declaration an approver makes per identity.
 *  Part of the contract closure of every AI System the identity is a
 *  member of. */
export interface AgentImplementationDeclaration {
    /** The name this identity's software must declare; null = not declared. */
    implementation_name: string | null;
    /** Exact versions or a prefix ending in `.*`; empty = observe only. */
    approved_versions: string[];
    /** A run with no name/version is a finding. */
    require_release: boolean;
}

/** One observed release (GET /agent-identities/{id}/releases). */
export interface AgentRelease {
    id: string;
    agent_id: string;
    name: string;
    version: string;
    /** git SHA or image digest; null = none declared. */
    build: string | null;
    /** `name@version`, plus `#build` when there is one. */
    label: string;
    trust: AgentReleaseTrust;
    first_seen_at: string | null;
    last_seen_at: string | null;
    run_count: number;
    /** Distinct implementation fingerprints seen for this release (≤ 16). */
    fingerprints: string[];
    fingerprint_count: number;
    /** Derived from approved_versions; null = observe only. */
    approved: boolean | null;
    /** Whether the name is `implementation_name`; null = none declared. */
    name_matches: boolean | null;
    /** The release seen last. */
    current: boolean;
}

export interface AgentReleaseListResponse {
    agent_id: string;
    implementation_name: string | null;
    approved_versions: string[];
    require_release: boolean;
    current_release_id: string | null;
    releases: AgentRelease[];
    total: number;
}

/** A member agent's CURRENT release as the composition view and the
 *  assurance report carry it. */
export interface AgentReleaseRef {
    name: string;
    version: string;
    build: string | null;
    trust: AgentReleaseTrust;
    /** null = the identity approves nothing (observe only). */
    approved: boolean | null;
    last_seen_at: string | null;
}

export interface AgentIdentityCreate {
    name: string;
    description?: string;
    owner_user_id?: number;
    owner_label?: string;
    idp_server_id?: string;
    idp_subject?: string;
    source?: AgentSource;
    /** ISO 8601 with its zone; left out, the identity does not expire. */
    expires_at?: string;
    default_deny?: boolean;
}

/**
 * Aliased to the generated schema instead of re-declared.
 *
 * Several fields are optional on the wire (and some are plain `string`, not a
 * union), so a call site dereferencing them without a guard throws on a
 * response that legitimately omits the key. A hand-written response interface
 * is an assertion nothing verifies; the generated type is derived from the
 * Control Plane's own response model, so a server-side change breaks the
 * build rather than the page.
 */
export type AgentGrant = components['schemas']['AgentGrantResponse'];

export interface AgentGrantCreate {
    action_type: GrantActionType;
    target: string;
    effect: GrantEffect;
    constraints?: Record<string, unknown>;
    expires_at?: string;
}

export interface AgentMembership {
    id: number;
    tenant_id: string;
    agent_id: string;
    group_id: string;
    role?: string | null;
    added_by?: string | null;
    joined_at?: string | null;
}

export const GRANT_ACTION_TYPES: GrantActionType[] = [
    'llm_call', 'mcp_tool', 'a2a_call', 'skill_exec',
];
export const GRANT_EFFECTS: GrantEffect[] = ['allow', 'deny', 'approval_required'];
