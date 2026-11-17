/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Semantic Governance Policy types — mirrors the control-plane shape served by
 * `/v1/admin/semantic-policies`. A semantic policy is a natural-language
 * constraint evaluated at runtime by an LLM judge (shadow → enforce), scoped to
 * agent-action surfaces and optionally bound to resource groups.
 */

export type PolicyMode = 'shadow' | 'enforce';
export type PolicyAction = 'block' | 'warn';

/** Agent-action ingress/egress surfaces where the SGP runtime hook is wired. */
export type Surface =
    | 'chat_input'
    | 'mcp_input'
    | 'a2a_inbound'
    | 'a2a_outbound'
    | 'skill_input';

export const SURFACES: Surface[] = [
    'chat_input',
    'mcp_input',
    'a2a_inbound',
    'a2a_outbound',
    'skill_input',
];

export const SURFACE_LABEL: Record<Surface, string> = {
    chat_input: 'Chat input',
    mcp_input: 'MCP input',
    a2a_inbound: 'A2A inbound',
    a2a_outbound: 'A2A outbound',
    skill_input: 'Skill input',
};

export const MODE_LABEL: Record<PolicyMode, string> = {
    shadow: 'Shadow (log-only)',
    enforce: 'Enforce',
};

export const ACTION_LABEL: Record<PolicyAction, string> = {
    block: 'Block (403)',
    warn: 'Warn (log)',
};

export interface SemanticPolicy {
    id: string;
    tenant_id?: string;
    name: string;
    description?: string | null;
    constraint_text: string;
    action: PolicyAction;
    mode: PolicyMode;
    surfaces: string[];
    judge_model_id?: string | null;
    enabled: boolean;
    resource_group_ids: string[];
    created_by?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
}
