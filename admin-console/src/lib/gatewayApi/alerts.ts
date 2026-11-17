/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Usage alerts, system alerts, notification channels and the Assurance Inbox, Checks and Digests.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type {
    AssuranceInboxCounts,
    AssuranceInboxItem,
    AssuranceInboxItemDetail,
    AssuranceInboxListResponse,
} from '../assuranceInboxTypes';
import type {
    NotificationChannel,
    NotificationChannelConfigInput,
    NotificationChannelKind,
    NotificationDelivery,
    NotificationDeliveryListResponse,
    NotificationEventClass,
    NotificationSubscription,
    NotificationTestSendResponse,
} from '../notificationTypes';
import type {
    AssuranceCheck,
    AssuranceCheckCreate,
    AssuranceCheckVersion,
    CheckBacktestResponse,
    CheckCompileAssistResponse,
    CheckCompileResponse,
    CheckResultListResponse,
    DigestContent,
    DigestListResponse,
} from '../assuranceCheckTypes';
import {
    TenantAlertsResponse,
    GroupAlertResponse,
    AlertAcknowledgeResponse,
    UsageAlertQueryParams,
    SystemAlertsListResponse,
    SystemAlertAcknowledgeResponse,
    SystemAlertResolveResponse,
} from '../groupTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly } from '../api/controlPlaneTypes';

export class AlertsApi extends GatewayApiBase {

    // =============================================================================
    // Usage Alert Operations
    // =============================================================================

    /**
     * Fetch all usage alerts across the tenant
     * GET /v1/admin/resource-groups/usage-alerts
     */
    async fetchAllUsageAlerts(
        params?: UsageAlertQueryParams
    ): Promise<TenantAlertsResponse> {
        const searchParams = new URLSearchParams();

        if (params?.status) searchParams.append('status', params.status);
        if (params?.resource_type) searchParams.append('resource_type', params.resource_type);
        if (params?.alert_type) searchParams.append('alert_type', params.alert_type);
        if (params?.group_id) searchParams.append('group_id', params.group_id);
        if (params?.limit) searchParams.append('limit', String(params.limit));

        const queryString = searchParams.toString();
        const endpoint = `/v1/admin/resource-groups/usage-alerts${queryString ? `?${queryString}` : ''}`;

        return this.request<TenantAlertsResponse>(endpoint);
    }

    /**
     * Fetch usage alerts for a specific resource group
     * GET /v1/admin/resource-groups/{groupId}/usage-alerts
     */
    async fetchGroupAlerts(
        groupId: string,
        params?: UsageAlertQueryParams
    ): Promise<GroupAlertResponse> {
        const searchParams = new URLSearchParams();

        if (params?.status) searchParams.append('status', params.status);
        if (params?.resource_type) searchParams.append('resource_type', params.resource_type);
        if (params?.alert_type) searchParams.append('alert_type', params.alert_type);
        if (params?.limit) searchParams.append('limit', String(params.limit));

        const queryString = searchParams.toString();
        const endpoint = `/v1/admin/resource-groups/${groupId}/usage-alerts${queryString ? `?${queryString}` : ''}`;

        return this.request<GroupAlertResponse>(endpoint);
    }

    /**
     * Acknowledge a usage alert
     * POST /v1/admin/resource-groups/usage-alerts/{alertId}/acknowledge
     */
    async acknowledgeUsageAlert(
        alertId: string
    ): Promise<AlertAcknowledgeResponse> {
        return this.request<AlertAcknowledgeResponse>(
            `/v1/admin/resource-groups/usage-alerts/${alertId}/acknowledge`,
            { method: 'POST' }
        );
    }

    // =============================================================================
    // System Alerts (downstream failures, health transitions, infra thresholds,
    // internal errors). Distinct from usage_alerts which is quota-scoped.
    // =============================================================================

    /**
     * List notification channels. Configs come back
     * masked — the API never returns webhook URLs or secrets.
     *
     * GET /v1/admin/notifications/channels
     */
    async listNotificationChannels(): Promise<NotificationChannel[]> {
        return this.request<NotificationChannel[]>('/v1/admin/notifications/channels');
    }

    /**
     * POST /v1/admin/notifications/channels
     */
    async createNotificationChannel(body: {
        name: string;
        kind: NotificationChannelKind;
        config: NotificationChannelConfigInput;
        enabled?: boolean;
    }): Promise<NotificationChannel> {
        return this.request<NotificationChannel>('/v1/admin/notifications/channels', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /**
     * PATCH /v1/admin/notifications/channels/{channel_id} — omitting
     * `config` keeps the stored (encrypted) one.
     */
    async updateNotificationChannel<Sent>(
        channelId: string,
        body: ApiBodyExactly<'/v1/admin/notifications/channels/{channel_id}', 'patch', Sent>,
    ): Promise<NotificationChannel> {
        return this.request<NotificationChannel>(
            `/v1/admin/notifications/channels/${channelId}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    /**
     * DELETE /v1/admin/notifications/channels/{channel_id}
     */
    async deleteNotificationChannel(channelId: string): Promise<{ deleted: boolean }> {
        return this.request<{ deleted: boolean }>(
            `/v1/admin/notifications/channels/${channelId}`,
            { method: 'DELETE' },
        );
    }

    /**
     * POST /v1/admin/notifications/channels/{channel_id}/test
     */
    async testNotificationChannel(
        channelId: string,
    ): Promise<NotificationTestSendResponse> {
        return this.request<NotificationTestSendResponse>(
            `/v1/admin/notifications/channels/${channelId}/test`,
            { method: 'POST' },
        );
    }

    /**
     * GET /v1/admin/notifications/subscriptions
     */
    async listNotificationSubscriptions(): Promise<NotificationSubscription[]> {
        return this.request<NotificationSubscription[]>(
            '/v1/admin/notifications/subscriptions',
        );
    }

    /**
     * POST /v1/admin/notifications/subscriptions
     */
    async createNotificationSubscription(body: {
        channel_id: string;
        event_class?: NotificationEventClass;
        severity_floor?: 'info' | 'warning' | 'critical';
        categories?: string[] | null;
        ai_system_id?: string | null;
        coalesce_seconds?: number;
        enabled?: boolean;
    }): Promise<NotificationSubscription> {
        return this.request<NotificationSubscription>(
            '/v1/admin/notifications/subscriptions',
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    /**
     * PATCH /v1/admin/notifications/subscriptions/{subscription_id}
     */
    async updateNotificationSubscription<Sent>(
        subscriptionId: string,
        body: ApiBodyExactly<'/v1/admin/notifications/subscriptions/{subscription_id}', 'patch', Sent>,
    ): Promise<NotificationSubscription> {
        return this.request<NotificationSubscription>(
            `/v1/admin/notifications/subscriptions/${subscriptionId}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    /**
     * DELETE /v1/admin/notifications/subscriptions/{subscription_id}
     */
    async deleteNotificationSubscription(
        subscriptionId: string,
    ): Promise<{ deleted: boolean }> {
        return this.request<{ deleted: boolean }>(
            `/v1/admin/notifications/subscriptions/${subscriptionId}`,
            { method: 'DELETE' },
        );
    }

    /**
     * GET /v1/admin/notifications/deliveries — the audit trail of what
     * was sent (or coalesced / failed), newest first.
     */
    async listNotificationDeliveries(params?: {
        channel_id?: string;
        status?: NotificationDelivery['status'];
        limit?: number;
        offset?: number;
    }): Promise<NotificationDeliveryListResponse> {
        const qs = new URLSearchParams();
        if (params?.channel_id) qs.append('channel_id', params.channel_id);
        if (params?.status) qs.append('status', params.status);
        if (params?.limit) qs.append('limit', String(params.limit));
        if (params?.offset) qs.append('offset', String(params.offset));
        const q = qs.toString();
        return this.request<NotificationDeliveryListResponse>(
            `/v1/admin/notifications/deliveries${q ? `?${q}` : ''}`,
        );
    }

    /**
     * List assurance-inbox items — the per-tenant triage queue
     * of findings awaiting human review. All filters optional.
     *
     * GET /v1/admin/assurance-inbox
     */
    async listAssuranceInbox(params?: {
        state?: 'open' | 'acknowledged' | 'resolved';
        severity?: 'info' | 'warning' | 'critical';
        category?: string;
        ai_system_id?: string;
        limit?: number;
        offset?: number;
    }): Promise<AssuranceInboxListResponse> {
        const qs = new URLSearchParams();
        if (params?.state) qs.append('state', params.state);
        if (params?.severity) qs.append('severity', params.severity);
        if (params?.category) qs.append('category', params.category);
        if (params?.ai_system_id) qs.append('ai_system_id', params.ai_system_id);
        if (params?.limit) qs.append('limit', String(params.limit));
        if (params?.offset) qs.append('offset', String(params.offset));
        const q = qs.toString();
        return this.request<AssuranceInboxListResponse>(
            `/v1/admin/assurance-inbox${q ? `?${q}` : ''}`,
        );
    }

    /**
     * Badge counts for Mission Control.
     *
     * GET /v1/admin/assurance-inbox/counts
     */
    async getAssuranceInboxCounts(): Promise<AssuranceInboxCounts> {
        return this.request<AssuranceInboxCounts>('/v1/admin/assurance-inbox/counts');
    }

    /**
     * One item with its append-only event trail.
     *
     * GET /v1/admin/assurance-inbox/{item_id}
     */
    async getAssuranceInboxItem(itemId: string): Promise<AssuranceInboxItemDetail> {
        return this.request<AssuranceInboxItemDetail>(
            `/v1/admin/assurance-inbox/${itemId}`,
        );
    }

    /**
     * POST /v1/admin/assurance-inbox/{item_id}/acknowledge
     */
    async acknowledgeAssuranceInboxItem(itemId: string): Promise<AssuranceInboxItem> {
        return this.request<AssuranceInboxItem>(
            `/v1/admin/assurance-inbox/${itemId}/acknowledge`,
            { method: 'POST' },
        );
    }

    /**
     * POST /v1/admin/assurance-inbox/{item_id}/assign
     */
    async assignAssuranceInboxItem(
        itemId: string,
        assignee: string,
    ): Promise<AssuranceInboxItem> {
        return this.request<AssuranceInboxItem>(
            `/v1/admin/assurance-inbox/${itemId}/assign`,
            { method: 'POST', body: JSON.stringify({ assignee }) },
        );
    }

    /**
     * Resolve with a disposition + note — dispositions are report
     * evidence, so the note is REQUIRED server-side.
     *
     * POST /v1/admin/assurance-inbox/{item_id}/resolve
     */
    async resolveAssuranceInboxItem(
        itemId: string,
        disposition: 'true_positive' | 'false_positive' | 'expected' | 'fixed',
        note: string,
    ): Promise<AssuranceInboxItem> {
        return this.request<AssuranceInboxItem>(
            `/v1/admin/assurance-inbox/${itemId}/resolve`,
            { method: 'POST', body: JSON.stringify({ disposition, note }) },
        );
    }

    /**
     * POST /v1/admin/assurance-inbox/{item_id}/reopen
     */
    async reopenAssuranceInboxItem(
        itemId: string,
        reason: string,
    ): Promise<AssuranceInboxItem> {
        return this.request<AssuranceInboxItem>(
            `/v1/admin/assurance-inbox/${itemId}/reopen`,
            { method: 'POST', body: JSON.stringify({ reason }) },
        );
    }

    /**
     * POST /v1/admin/assurance-inbox/bulk-acknowledge
     */
    async bulkAcknowledgeAssuranceInbox(
        itemIds: string[],
    ): Promise<AssuranceInboxCounts> {
        return this.request<AssuranceInboxCounts>(
            '/v1/admin/assurance-inbox/bulk-acknowledge',
            { method: 'POST', body: JSON.stringify({ item_ids: itemIds }) },
        );
    }

    // ── Continuous Assurance Checks ───────────────

    /** GET /v1/admin/assurance-checks */
    async listAssuranceChecks(): Promise<AssuranceCheck[]> {
        return this.request<AssuranceCheck[]>('/v1/admin/assurance-checks');
    }

    /** POST /v1/admin/assurance-checks — creates version 1, disabled. */
    async createAssuranceCheck(body: AssuranceCheckCreate): Promise<AssuranceCheck> {
        return this.request<AssuranceCheck>('/v1/admin/assurance-checks', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /**
     * PATCH /v1/admin/assurance-checks/{id} — spec-field changes bump the
     * version and drop enablement until re-approved; enabling records the
     * approver.
     */
    async updateAssuranceCheck<Sent>(
        checkId: string,
        body: ApiBodyExactly<'/v1/admin/assurance-checks/{check_id}', 'patch', Sent>,
    ): Promise<AssuranceCheck> {
        return this.request<AssuranceCheck>(
            `/v1/admin/assurance-checks/${checkId}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    /** DELETE /v1/admin/assurance-checks/{id} */
    async deleteAssuranceCheck(checkId: string): Promise<void> {
        await this.request<void>(`/v1/admin/assurance-checks/${checkId}`, {
            method: 'DELETE',
        });
    }

    /** GET /v1/admin/assurance-checks/{id}/versions — immutable snapshots. */
    async listAssuranceCheckVersions(
        checkId: string,
    ): Promise<AssuranceCheckVersion[]> {
        return this.request<AssuranceCheckVersion[]>(
            `/v1/admin/assurance-checks/${checkId}/versions`,
        );
    }

    /**
     * POST /v1/admin/assurance-checks/{id}/backtest — evaluate the current
     * version against recorded runs. Dispatches nothing, raises nothing.
     */
    async backtestAssuranceCheck(
        checkId: string,
        windowDays: number,
    ): Promise<CheckBacktestResponse> {
        return this.request<CheckBacktestResponse>(
            `/v1/admin/assurance-checks/${checkId}/backtest`,
            { method: 'POST', body: JSON.stringify({ window_days: windowDays }) },
        );
    }

    /** GET /v1/admin/assurance-checks/{id}/results */
    async listAssuranceCheckResults(
        checkId: string,
        params?: { backtest?: boolean; limit?: number },
    ): Promise<CheckResultListResponse> {
        const qs = new URLSearchParams();
        if (params?.backtest !== undefined) {
            qs.append('backtest', String(params.backtest));
        }
        if (params?.limit) qs.append('limit', String(params.limit));
        const q = qs.toString();
        return this.request<CheckResultListResponse>(
            `/v1/admin/assurance-checks/${checkId}/results${q ? `?${q}` : ''}`,
        );
    }

    /**
     * POST /v1/admin/assurance-checks/compile — parse + profile-check +
     * sample-evaluate an expression against the Rust core's evaluator.
     */
    async compileAssuranceCheck(
        expression: string,
        output?: string,
    ): Promise<CheckCompileResponse> {
        return this.request<CheckCompileResponse>(
            '/v1/admin/assurance-checks/compile',
            { method: 'POST', body: JSON.stringify({ expression, output }) },
        );
    }

    /**
     * POST /v1/admin/assurance-checks/compile-assist — NL question →
     * proposed spec. Nothing persists; the operator reviews and creates.
     */
    async compileAssistAssuranceCheck(
        question: string,
        scopeHint?: string,
    ): Promise<CheckCompileAssistResponse> {
        return this.request<CheckCompileAssistResponse>(
            '/v1/admin/assurance-checks/compile-assist',
            {
                method: 'POST',
                body: JSON.stringify({ question, scope_hint: scopeHint }),
            },
        );
    }

    // ── Weekly assurance digest ──────────────────────

    /** GET /v1/admin/assurance-digest — stored digests, newest first. */
    async listAssuranceDigests(limit?: number): Promise<DigestListResponse> {
        const q = limit ? `?limit=${limit}` : '';
        return this.request<DigestListResponse>(`/v1/admin/assurance-digest${q}`);
    }

    /** POST /v1/admin/assurance-digest/preview — current week, live. */
    async previewAssuranceDigest(): Promise<DigestContent> {
        return this.request<DigestContent>('/v1/admin/assurance-digest/preview', {
            method: 'POST',
        });
    }

    /**
     * List system alerts for the current tenant.
     *
     * The server also takes `tenant_scope=all` from a system administrator
     * (every tenant's alerts); nothing here sends it.
     *
     * GET /v1/admin/system-alerts
     */
    async fetchSystemAlerts(params?: {
        status?: 'active' | 'acknowledged' | 'resolved';
        severity?: 'critical' | 'high' | 'medium' | 'low';
        category?:
            | 'downstream_provider'
            | 'downstream_idp'
            | 'downstream_cache'
            | 'downstream_vector_db'
            | 'resource_health'
            | 'infrastructure_threshold'
            | 'internal_error';
        limit?: number;
    }): Promise<SystemAlertsListResponse> {
        const qs = new URLSearchParams();
        if (params?.status) qs.append('status', params.status);
        if (params?.severity) qs.append('severity', params.severity);
        if (params?.category) qs.append('category', params.category);
        if (params?.limit) qs.append('limit', String(params.limit));
        const q = qs.toString();
        return this.request<SystemAlertsListResponse>(
            `/v1/admin/system-alerts${q ? `?${q}` : ''}`,
        );
    }

    /**
     * Acknowledge a system alert.
     *
     * Idempotent: acknowledging an already-acknowledged alert returns the
     * existing state instead of erroring. Acknowledged alerts are never
     * auto-resolved — the operator owns them until explicit resolution.
     *
     * POST /v1/admin/system-alerts/{alertId}/acknowledge
     */
    async acknowledgeSystemAlert(
        alertId: string,
    ): Promise<SystemAlertAcknowledgeResponse> {
        return this.request<SystemAlertAcknowledgeResponse>(
            `/v1/admin/system-alerts/${alertId}/acknowledge`,
            { method: 'POST' },
        );
    }

    /**
     * Resolve a system alert with an optional note.
     *
     * After resolution the dedup index no longer matches the row, so a
     * new occurrence of the same failure produces a fresh active alert
     * rather than bumping the closed one.
     *
     * POST /v1/admin/system-alerts/{alertId}/resolve
     */
    async resolveSystemAlert(
        alertId: string,
        resolutionNote?: string,
    ): Promise<SystemAlertResolveResponse> {
        return this.request<SystemAlertResolveResponse>(
            `/v1/admin/system-alerts/${alertId}/resolve`,
            {
                method: 'POST',
                body: JSON.stringify({
                    resolution_note: resolutionNote ?? null,
                }),
            },
        );
    }
}
