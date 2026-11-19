/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Identity policy: a group's policy (effective, inherited floor, sources),
 * its write, the credentials an AI System has seen, and the tenant identity
 * default.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class. Every answer is read by
 * `lib/identityPolicy.ts`.
 */

import {
    identityPolicyDefaultOf,
    parseIdentityCredentials,
    parseIdentityPolicyResponse,
    type IdentityCredentialList,
    type IdentityPolicy,
    type IdentityPolicyDefault,
    type IdentityPolicyResponse,
} from '../identityPolicy';
import type { ApiRequestBody } from '../api/controlPlaneTypes';
import { GatewayApiBase } from './base';

export class IdentityApi extends GatewayApiBase {

    /** The group's stored policy, its effective policy, the floor it inherits and where each field comes from. */
    async fetchIdentityPolicy(groupId: string): Promise<IdentityPolicyResponse> {
        return parseIdentityPolicyResponse(
            await this.request<unknown>(`/v1/admin/resource-groups/${encodeURIComponent(groupId)}/identity-policy`),
        );
    }

    /**
     * Store the group's own policy; null removes it. Refused (422,
     * `identity_policy_below_floor`) when it is looser than what the group
     * inherits, with every such field named.
     */
    async putIdentityPolicy(groupId: string, policy: IdentityPolicy | null): Promise<IdentityPolicyResponse> {
        return parseIdentityPolicyResponse(
            await this.request<unknown>(
                `/v1/admin/resource-groups/${encodeURIComponent(groupId)}/identity-policy`,
                {
                    method: 'PUT',
                    body: JSON.stringify({ policy } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/identity-policy', 'put'>),
                },
            ),
        );
    }

    /** The credentials that reached an AI System (its whole subtree) in the last `days`, newest first. */
    async fetchIdentityCredentials(
        aiSystemId: string,
        { days = 30, limit = 100, offset = 0 }: { days?: number; limit?: number; offset?: number } = {},
    ): Promise<IdentityCredentialList> {
        return parseIdentityCredentials(
            await this.request<unknown>(
                `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/identity/credentials?days=${days}&limit=${limit}&offset=${offset}`,
            ),
        );
    }

    /**
     * Set the tenant identity default alone (null removes it), and answer
     * the default the tenant now holds. Needs `identity_policy:manage` on
     * top of the permission the tenant update itself needs.
     */
    async updateTenantIdentityPolicyDefault(value: IdentityPolicyDefault | null): Promise<IdentityPolicyDefault | null> {
        return identityPolicyDefaultOf(
            await this.request<unknown>('/v1/admin/tenants/current', {
                method: 'PATCH',
                body: JSON.stringify({ identity_policy_default: value } satisfies ApiRequestBody<'/v1/admin/tenants/current', 'patch'>),
            }),
        );
    }
}
