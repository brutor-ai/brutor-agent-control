/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The system operations console: overview, counters, caches, audit chains, connections, tasks, sessions, log levels and maintenance.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { AuditChainReport } from '../types';
import {
    CacheFlushRequest,
    CacheStatsResponse,
    ConnectionListResponse,
    CounterFilter,
    CounterListResponse,
    CounterReleaseCooldownRequest,
    CounterResetRequest,
    DbVacuumRequest,
    DbVacuumResponse,
    LogLevelListResponse,
    MaintenanceStateResponse,
    PoolRecycleRequest,
    PoolRecycleResponse,
    RestartRollingRequest,
    RestartRollingResponse,
    SchedulerActionRequest,
    SchedulerListResponse,
    SetLogLevelRequest,
    SetLogLevelResponse,
    SystemAuditFacetsResponse,
    SystemAuditFilter,
    SystemAuditListResponse,
    SystemOverviewResponse,
} from '../systemOpsTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly, ApiResponse } from '../api/controlPlaneTypes';

export class SystemOpsApi extends GatewayApiBase {

    // ─── System Operations Console ───────────────────────────
    //
    // Tenant-scoped operator surface. Distinct from `getPlatformOverview`
    // above (platform admin only, cross-tenant aggregation). Reads need
    // `system:read`; the action endpoints require `system:operate` or
    // `system:maintenance:toggle` as appropriate — the backend gates,
    // the UI deeply hints disabled state.

    async getSystemOverview(): Promise<SystemOverviewResponse> {
        return this.request<SystemOverviewResponse>('/v1/admin/system/overview');
    }

    // --- Audit hash chains ---------------------------------
    //
    // Both proxy to the Rust core, which owns the canonical row format and is
    // therefore the only thing that can recompute a row hash. A 503 means no
    // check ran — it is never a statement about the records.

    async listAuditChains(limit = 100): Promise<{ chains: string[] }> {
        return this.request<{ chains: string[] }>(
            `/v1/admin/audit/chains?limit=${limit}`,
        );
    }

    async verifyAuditChain(
        chainId: string,
        fromSeq = 0,
        limit = 10000,
    ): Promise<AuditChainReport> {
        return this.request<AuditChainReport>(
            `/v1/admin/audit/chains/${encodeURIComponent(chainId)}/verify` +
                `?from_seq=${fromSeq}&limit=${limit}`,
            { method: 'POST' },
        );
    }
    // ─── counters-rust-consumer-and-ui ───
    /**
     * Counter list for the Counters page.
     *
     * The backend treats every filter field as optional. We forward
     * them verbatim as query-string params so the request signature
     * matches the FastAPI Query(default=None) shape; an empty value
     * is omitted to avoid sending `?group_id=` (FastAPI would coerce
     * that to empty-string and miss the "unset" intent).
     */
    async listSystemCounters(
        filter: CounterFilter,
    ): Promise<CounterListResponse> {
        const qs = new URLSearchParams();
        if (filter.group_id) qs.set('group_id', filter.group_id);
        if (filter.kind) qs.set('kind', filter.kind);
        if (filter.subject_id) qs.set('subject_id', filter.subject_id);
        if (filter.scope) qs.set('scope', filter.scope);
        if (filter.only_cooling_down) qs.set('only_cooling_down', 'true');
        const suffix = qs.toString();
        return this.request<CounterListResponse>(
            `/v1/admin/system/counters${suffix ? `?${suffix}` : ''}`,
        );
    }

    /**
     * Reset a counter bucket. The backend writes the audit
     * row before publishing NOTIFY, so on success the action is
     * durable even if NOTIFY delivery never lands at a replica.
     * Requires the caller's session to hold `system:operate`.
     */
    async resetSystemCounter(
        body: CounterResetRequest,
    ): Promise<{ status: string; channel: string; payload: string }> {
        return this.request('/v1/admin/system/counters/reset', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /**
     * Release any cooldown for the given scope. Same audit +
     * NOTIFY shape as reset; the proxy distinguishes by payload.
     * Requires `system:operate`.
     */
    async releaseSystemCooldown(
        body: CounterReleaseCooldownRequest,
    ): Promise<{ status: string; channel: string; payload: string }> {
        return this.request('/v1/admin/system/counters/release-cooldown', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }


    // ─── caches ───
    /**
     * Per-layer cache stats for the System → Caches page. Semantic
     * hit/miss is aggregated from proxy_logs over the last 60 min;
     * other layers surface zeros with a `detail` hint until the
     * gateway-core fan-out reply round-trip ships.
     */
    async listSystemCaches(): Promise<CacheStatsResponse> {
        return this.request<CacheStatsResponse>('/v1/admin/system/caches');
    }

    /**
     * Publish a cache flush via NOTIFY. The audit row is written
     * before NOTIFY so the action is durable even if a replica is
     * briefly offline. The backend validates (layer, scope) pairs
     * via the channel registry — invalid combinations return 4xx
     * with the underlying Pydantic error.
     */
    async flushSystemCache(body: CacheFlushRequest): Promise<{ status: string; channel: string; payload: string }> {
        return this.request('/v1/admin/system/caches/flush', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    }


    // ─── audit ───

    /**
     * List audit rows from the System Console.
     *
     * Multi-value filters are sent as repeated query params (the
     * FastAPI side reads them as `Query(default_factory=list)`).
     * Pagination + sort piggy-back on the same URL — no body
     * needed because the envelope is small and the endpoint is
     * cache-friendly when intermediaries see GETs.
     */
    async listSystemAudit(
        filter: SystemAuditFilter & { limit?: number; offset?: number; sort_order?: 'asc' | 'desc' },
    ): Promise<SystemAuditListResponse> {
        const q = new URLSearchParams();
        for (const v of filter.actions ?? []) q.append('actions', v);
        for (const v of filter.scope_kinds ?? []) q.append('scope_kinds', v);
        for (const v of filter.actor_admin_ids ?? []) q.append('actor_admin_ids', v);
        for (const v of filter.actor_types ?? []) q.append('actor_types', v);
        if (filter.start) q.append('start', filter.start);
        if (filter.end) q.append('end', filter.end);
        if (filter.q) q.append('q', filter.q);
        if (filter.limit !== undefined) q.append('limit', String(filter.limit));
        if (filter.offset !== undefined) q.append('offset', String(filter.offset));
        if (filter.sort_order) q.append('sort_order', filter.sort_order);
        const qs = q.toString();
        return this.request<SystemAuditListResponse>(
            `/v1/admin/system/audit${qs ? `?${qs}` : ''}`,
        );
    }

    /**
     * Faceted counts for the Audit sidebar. Same envelope as
     * listSystemAudit so counts always match the table.
     */
    async getSystemAuditFacets(
        filter: SystemAuditFilter & { per_field_limit?: number },
    ): Promise<SystemAuditFacetsResponse> {
        const q = new URLSearchParams();
        for (const v of filter.actions ?? []) q.append('actions', v);
        for (const v of filter.scope_kinds ?? []) q.append('scope_kinds', v);
        for (const v of filter.actor_admin_ids ?? []) q.append('actor_admin_ids', v);
        for (const v of filter.actor_types ?? []) q.append('actor_types', v);
        if (filter.start) q.append('start', filter.start);
        if (filter.end) q.append('end', filter.end);
        if (filter.q) q.append('q', filter.q);
        if (filter.per_field_limit !== undefined) q.append('per_field_limit', String(filter.per_field_limit));
        const qs = q.toString();
        return this.request<SystemAuditFacetsResponse>(
            `/v1/admin/system/audit/facets${qs ? `?${qs}` : ''}`,
        );
    }

    /**
     * Stream filtered audit rows as CSV. Mirrors the filter shape
     * of listSystemAudit. Resolves to a {blob, filename} so the
     * caller can drive a browser download via createObjectURL.
     */
    async downloadSystemAuditCsv(
        filter: SystemAuditFilter,
    ): Promise<{ blob: Blob; filename?: string }> {
        const q = new URLSearchParams();
        for (const v of filter.actions ?? []) q.append('actions', v);
        for (const v of filter.scope_kinds ?? []) q.append('scope_kinds', v);
        for (const v of filter.actor_admin_ids ?? []) q.append('actor_admin_ids', v);
        for (const v of filter.actor_types ?? []) q.append('actor_types', v);
        if (filter.start) q.append('start', filter.start);
        if (filter.end) q.append('end', filter.end);
        if (filter.q) q.append('q', filter.q);
        const qs = q.toString();
        return this.streamExport(
            `/v1/admin/system/audit/export${qs ? `?${qs}` : ''}`,
            'export_system_audit',
        );
    }


    // ─── connections ───

    /**
     * List outbound connection pools.
     *
     * Backend builds one entry per known pool: Postgres (real SQLAlchemy
     * stats), Redis (honest 'unknown' until a persistent client lands),
     * Qdrant (one client per request, latency from system_alerts), and
     * one entry per configured LLM provider. Requires `system:read`.
     */
    async listSystemConnections(): Promise<ConnectionListResponse> {
        return this.request<ConnectionListResponse>(
            '/v1/admin/system/connections',
        );
    }

    /**
     * Recycle a control-plane pool.
     *
     * Only `postgres` and `redis` are accepted by the backend. Postgres
     * calls `engine.dispose()`; Redis returns an explicit `{status:
     * 'noop', detail}` until the persistent client lands. Requires
     * `system:operate`.
     */
    async recycleSystemPool(
        body: PoolRecycleRequest,
    ): Promise<PoolRecycleResponse> {
        return this.request<PoolRecycleResponse>(
            '/v1/admin/system/connections/recycle',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            },
        );
    }


    // ─── tasks ───

    /**
     * GET /v1/admin/system/tasks — list background scheduler status.
     * Requires `system:read`. Returns one entry per KNOWN_SCHEDULERS
     * constant; rows that have never been paused appear with default
     * state synthesised by the backend.
     */
    async listSystemTasks(): Promise<SchedulerListResponse> {
        return this.request<SchedulerListResponse>('/v1/admin/system/tasks');
    }

    /**
     * POST /v1/admin/system/tasks/trigger — fire one out-of-band tick.
     * Requires `system:operate`. No mutation on scheduler_runtime_state.
     */
    async triggerSystemTask(
        body: SchedulerActionRequest,
    ): Promise<{ status: string; channel: string; payload: string }> {
        return this.request('/v1/admin/system/tasks/trigger', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /**
     * POST /v1/admin/system/tasks/pause — flip paused=TRUE + NOTIFY.
     * Requires `system:operate`. Idempotent.
     */
    async pauseSystemTask(
        body: SchedulerActionRequest,
    ): Promise<{ status: string; channel: string; payload: string }> {
        return this.request('/v1/admin/system/tasks/pause', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /**
     * POST /v1/admin/system/tasks/resume — flip paused=FALSE + NOTIFY.
     * Requires `system:operate`. Idempotent.
     */
    async resumeSystemTask(
        body: SchedulerActionRequest,
    ): Promise<{ status: string; channel: string; payload: string }> {
        return this.request('/v1/admin/system/tasks/resume', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }


    // ─── sessions ───

    // ─── System Operations — Sessions ──────────────────
    //
    // Tenant-scoped session enumeration + revoke. The list path takes a
    // single `kind` query param so the UI can render one table component
    // across the three credential families. Revoke writes an audit row
    // server-side and publishes `brutor:session:revoke` NOTIFY before
    // returning 202.

    async listSystemSessions(
        kind: 'admin' | 'portal' | 'api_key',
        limit: number = 50,
    ): Promise<import('../systemOpsTypes').SessionListResponse> {
        return this.request<import('../systemOpsTypes').SessionListResponse>(
            `/v1/admin/system/sessions?kind=${encodeURIComponent(kind)}&limit=${limit}`,
        );
    }

    async revokeSystemSession(
        body: import('../systemOpsTypes').SessionRevokeRequest,
    ): Promise<import('../systemOpsTypes').SessionRevokeResponse> {
        return this.request<import('../systemOpsTypes').SessionRevokeResponse>(
            '/v1/admin/system/sessions/revoke',
            {
                method: 'POST',
                body: JSON.stringify(body),
            },
        );
    }


    // ─── logs ───

    /**
     * GET /v1/admin/system/logs/levels — curated module list + any
     * ad-hoc overrides. Both LIVE effective levels and persisted
     * override metadata so the UI can highlight drift.
     */
    async getLogLevels(): Promise<LogLevelListResponse> {
        return this.request<LogLevelListResponse>('/v1/admin/system/logs/levels');
    }

    /**
     * POST /v1/admin/system/logs/levels — mutate a module's runtime
     * level. Default scope='all' fans the change out across replicas
     * via Postgres NOTIFY. Requires system:operate; the backend gates.
     */
    async setLogLevel(body: SetLogLevelRequest): Promise<SetLogLevelResponse> {
        return this.request<SetLogLevelResponse>('/v1/admin/system/logs/levels', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /**
     * Open a WebSocket to the runtime log tail.
     *
     * A browser cannot set the Authorization header of a WebSocket upgrade,
     * and the session token is never put in an address. The tail opens with
     * a ticket: `POST /v1/admin/system/logs/tail/ticket` (authenticated like
     * any request, `system:read`) answers a random, single-use ticket good
     * for 30 seconds, presented as `?ticket=…`. The socket is `wss://`
     * whenever the base URL is `https://`.
     *
     * Resolves to null without a session or where the browser has no
     * WebSocket; a refused ticket request rejects.
     */
    async openLogsWebSocket(level?: string, module?: string): Promise<WebSocket | null> {
        if (!this.authToken) return null;
        if (typeof window === 'undefined' || typeof window.WebSocket === 'undefined') {
            return null;
        }
        const { ticket } = await this.request<ApiResponse<'/v1/admin/system/logs/tail/ticket', 'post'>>(
            '/v1/admin/system/logs/tail/ticket',
            { method: 'POST' },
        );
        const wsBase = this.baseUrl.replace(/^http/, 'ws');
        const params = new URLSearchParams({ ticket });
        if (level) params.set('level', level);
        if (module) params.set('module', module);
        return new WebSocket(`${wsBase}/v1/admin/system/logs/tail?${params.toString()}`);
    }


        // ─── maintenance ───
    /**
     * Read the per-tenant maintenance state. Used by the System →
     * Maintenance page and by the portal banner to colour the
     * "tenant is in read-only mode" pill.
     */
    async getMaintenance(): Promise<MaintenanceStateResponse> {
        return this.request<MaintenanceStateResponse>(
            '/v1/admin/system/maintenance',
        );
    }

    /**
     * Toggle maintenance mode. Server enforces system:maintenance:toggle;
     * UI also gates the buttons client-side for the disabled affordance.
     */
    async patchMaintenance<Sent>(
        body: ApiBodyExactly<'/v1/admin/system/maintenance', 'patch', Sent>,
    ): Promise<MaintenanceStateResponse> {
        return this.request<MaintenanceStateResponse>(
            '/v1/admin/system/maintenance',
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    /**
     * Record an operator-initiated rolling restart in the Audit log.
     * Does NOT actually restart any process — the real rollout is
     * operator-driven via kubectl/ECS tooling.
     */
    async restartRolling(
        body: RestartRollingRequest,
    ): Promise<RestartRollingResponse> {
        return this.request<RestartRollingResponse>(
            '/v1/admin/system/maintenance/restart-rolling',
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    /**
     * Execute VACUUM ANALYZE on a whitelisted table.
     * Endpoint enforces the whitelist; a non-whitelisted table 400s.
     */
    async dbVacuum(body: DbVacuumRequest): Promise<DbVacuumResponse> {
        return this.request<DbVacuumResponse>(
            '/v1/admin/system/maintenance/db-vacuum',
            { method: 'POST', body: JSON.stringify(body) },
        );
    }
}
