/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrail configurations, providers, break-glass and tests.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { EffectiveGuardrailsResponse } from '../groupTypes';
import type {
    GuardrailConfigDetailResponse,
    GuardrailConfigListResponse,
    GuardrailConfigCreateRequest,
    GuardrailTestRequest,
    GuardrailTestResponse,
    GuardrailProviderListResponse,
} from '../guardrailTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly } from '../api/controlPlaneTypes';

export class GuardrailsApi extends GatewayApiBase {

    // ============================================================================
    // Guardrails Management
    // ============================================================================

    async fetchEffectiveGuardrails(
        groupId: string
    ): Promise<EffectiveGuardrailsResponse> {
        return this.request<EffectiveGuardrailsResponse>(
            `/v1/admin/resource-groups/${groupId}/effective-guardrails`
        );
    }

    /**
     * Get the guardrail configuration for the current tenant.
     * Returns defaults if no configuration has been saved yet.
     */
    async getGuardrailConfig(): Promise<GuardrailConfigDetailResponse> {
        return this.request<GuardrailConfigDetailResponse>('/v1/admin/guardrails/config', {
            method: 'GET'
        });
    }

    /**
     * Create or update the guardrail configuration for the current tenant.
     * This is an upsert — if no config exists, one is created.
     * @param data - Full guardrail configuration update
     * @returns Updated configuration
     */
    async updateGuardrailConfig<Sent>(data: ApiBodyExactly<'/v1/admin/guardrails/config', 'patch', Sent>): Promise<GuardrailConfigDetailResponse> {
        return this.request<GuardrailConfigDetailResponse>('/v1/admin/guardrails/config', {
            method: 'PATCH',
            body: JSON.stringify(data)
        });
    }

    /**
     * Test content against the current guardrail configuration.
     * @param data - Content and surface/direction to test
     * @returns Test results including violations and final action
     */
    async testGuardrails(data: GuardrailTestRequest): Promise<GuardrailTestResponse> {
        return this.request<GuardrailTestResponse>('/v1/admin/guardrails/test', {
            method: 'POST',
            body: JSON.stringify(data)
        });
    }

    /**
     * Probe an external guardrail provider for reachability + auth
     * (distinct from testGuardrails, which tests detection *behaviour*).
     * When `api_key` is omitted the server falls back to the saved key
     * for (configId, provider) — the key is never returned to the client,
     * so an unchanged provider has none to send.
     */
    async testGuardrailProviderConnection(data: {
        provider: string;
        endpoint?: string | null;
        api_key?: string | null;
        config?: Record<string, unknown> | null;
        config_id?: string | null;
    }): Promise<{
        provider: string;
        status: 'healthy' | 'unhealthy' | 'timeout';
        ok: boolean;
        response_time_ms: number;
        detail?: string | null;
        error_message?: string | null;
    }> {
        return this.request('/v1/admin/guardrails/providers/test', {
            method: 'POST',
            body: JSON.stringify(data),
            // Providers should answer in a few seconds; give the probe a
            // little more headroom than the server's 8s ceiling so a slow
            // edge resolves to the server's verdict rather than a fetch abort.
            timeout: 15_000,
        });
    }

    /**
     * List all available guardrail providers and their capabilities.
     * @returns List of provider info objects
     */
    async listGuardrailProviders(): Promise<GuardrailProviderListResponse> {
        return this.request<GuardrailProviderListResponse>('/v1/admin/guardrails/providers', {
            method: 'GET'
        });
    }

    /**
     * List all guardrail configurations for the tenant.
     */
    async listGuardrailConfigs(): Promise<GuardrailConfigListResponse> {
        return this.request<GuardrailConfigListResponse>('/v1/admin/guardrails/configs', {
            method: 'GET'
        });
    }

    /**
     * Guardrail break-glass — current state + recent history.
     *
     * Break-glass degrades checks to log-and-allow during a detector outage.
     * It always expires; `remaining_seconds` counts down.
     */
    async fetchGuardrailBreakGlass(): Promise<import('../guardrailTypes').BreakGlassState> {
        return await this.request('/v1/admin/guardrails/break-glass');
    }

    /** Declare a guardrail outage. Reason is mandatory; duration is clamped. */
    async activateGuardrailBreakGlass(
        reason: string,
        durationMinutes?: number,
    ): Promise<import('../guardrailTypes').BreakGlassWindow> {
        return await this.request('/v1/admin/guardrails/break-glass', {
            method: 'POST',
            body: JSON.stringify({ reason, duration_minutes: durationMinutes }),
        });
    }

    /** End the window early and restore normal enforcement. */
    async deactivateGuardrailBreakGlass(): Promise<import('../guardrailTypes').BreakGlassState> {
        return await this.request('/v1/admin/guardrails/break-glass', { method: 'DELETE' });
    }

    /**
     * Create a new guardrail configuration.
     */
    async createGuardrailConfig(data: GuardrailConfigCreateRequest): Promise<GuardrailConfigDetailResponse> {
        return this.request<GuardrailConfigDetailResponse>('/v1/admin/guardrails/configs', {
            method: 'POST',
            body: JSON.stringify(data)
        });
    }

    /**
     * Get a specific guardrail configuration by ID.
     */
    async getGuardrailConfigById(configId: string): Promise<GuardrailConfigDetailResponse> {
        return this.request<GuardrailConfigDetailResponse>(`/v1/admin/guardrails/configs/${configId}`, {
            method: 'GET'
        });
    }

    /**
     * Update a specific guardrail configuration.
     */
    async updateGuardrailConfigById<Sent>(configId: string, data: ApiBodyExactly<'/v1/admin/guardrails/configs/{config_id}', 'patch', Sent>): Promise<GuardrailConfigDetailResponse> {
        return this.request<GuardrailConfigDetailResponse>(`/v1/admin/guardrails/configs/${configId}`, {
            method: 'PATCH',
            body: JSON.stringify(data)
        });
    }

    /**
     * Delete a guardrail configuration.
     */
    async deleteGuardrailConfig(configId: string): Promise<{ message: string; id: string }> {
        return this.request<{ message: string; id: string }>(`/v1/admin/guardrails/configs/${configId}`, {
            method: 'DELETE'
        });
    }

    /**
     * Set resource group associations for a guardrail config.
     */
    async setGuardrailGroups<Sent>(configId: string, data: ApiBodyExactly<'/v1/admin/guardrails/configs/{config_id}/groups', 'patch', Sent>): Promise<GuardrailConfigDetailResponse> {
        return this.request<GuardrailConfigDetailResponse>(`/v1/admin/guardrails/configs/${configId}/groups`, {
            method: 'PATCH',
            body: JSON.stringify(data)
        });
    }

    /**
     * Apply audit-recommended per-mode surface defaults.
     * Narrows jailbreak / prompt-injection / toxic-content to chat-
     * style surfaces (drops embeddings, batch, moderation_input) and
     * widens PII / secrets. Pass `dryRun: true` to preview the delta
     * without persisting, so the UI can render a confirmation modal.
     */
    async applyRecommendedModeDefaults(
        configId: string,
        dryRun: boolean = false,
    ): Promise<{
        config_id: string;
        applied: boolean;
        changed: string[];
        delta: Record<string, { before: string[]; after: string[]; added: string[]; removed: string[] }>;
        message?: string;
    }> {
        const qs = dryRun ? '?dry_run=true' : '';
        // No `/configs` segment here: the Control Plane registers this one as
        // `/v1/admin/guardrails/{config_id}/apply-recommended-mode-defaults`,
        // unlike its sibling config routes. Matching the convention instead of
        // the deployed route 404s.
        return this.request(`/v1/admin/guardrails/${configId}/apply-recommended-mode-defaults${qs}`, {
            method: 'POST',
        });
    }
}
