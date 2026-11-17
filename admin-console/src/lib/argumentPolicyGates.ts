/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Which argument policies also gate a given MCP capability.
 *
 * Capability filters are the unconditional switch; argument policies are the
 * conditional one. They meet at one decision, so the filter screen has to be
 * able to name the bands that also apply. Kept free of React so the matching
 * rules can be pinned without rendering.
 */

import type { ArgumentPolicy } from './argumentPolicyTypes';

/** Does this policy target the named tool? `"*"` matches anything. */
export function policyTargets(policy: ArgumentPolicy, capabilityName: string): boolean {
    if (!policy.enabled) return false;
    if (policy.surface !== 'mcp_input') return false;
    return policy.target === '*' || policy.target === capabilityName;
}

/** Policies bound to this group, or to no group at all (tenant-wide). */
export function policyAppliesToGroup(policy: ArgumentPolicy, groupId: string): boolean {
    const ids = policy.group_ids ?? [];
    return ids.length === 0 || ids.includes(groupId);
}
