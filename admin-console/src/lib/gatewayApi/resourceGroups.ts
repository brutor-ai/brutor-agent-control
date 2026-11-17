/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Resource groups and AI Systems: hierarchy, runs, drift, incident windows, health, the Assurance Report and members.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type {
    AISystemFleetFilters,
    AISystemFleetResponse,
    RunListFilters,
    RunListResponse,
    ServedCallListResponse,
    RunStatsResponse,
    RunTimeseriesResponse,
    RunDetail,
    RunIntervention,
    LiveRunsResponse,
    AbortRunResponse,
    TrajectoryResponse,
    ConsequenceResponse,
    ActionSearchResponse,
    UnattributedRunsResponse,
    LivenessStatus,
    LivenessExpectation,
    CadenceProposal,
    DriftEvent,
    DriftListResponse,
    BaselineListResponse,
    SystemHealth,
    AssuranceHistory,
} from '../aiSystemTypes';
import type {
    BaselineEpochListResponse,
    CloseDriftRequest,
    DriftResolution,
    IncidentWindowCreate,
    IncidentWindowDeleteResponse,
    IncidentWindowListResponse,
    IncidentWindowWriteResponse,
} from '../driftResolutionTypes';
import { MissionControlCardsParams, MissionControlCardsResponse, MissionControlExplainersResponse } from '../missionControlCardTypes';
import {
    ResourceGroup,
    GroupHierarchyResponse,
    CreateResourceGroupRequest,
    GroupMemberListResponse,
} from '../groupTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly, ApiRequestBody } from '../api/controlPlaneTypes';

export class ResourceGroupsApi extends GatewayApiBase {

    // ============================================================================
    // RESOURCE GROUP MANAGEMENT METHODS
    // ============================================================================

    /**
     * Fetch the hierarchical tree of all groups
     */
    async fetchGroupHierarchyTree(): Promise<GroupHierarchyResponse> {
        return this.request<GroupHierarchyResponse>('/v1/admin/resource-groups/hierarchy-tree');
    }

    /**
     * Fetch the AI System fleet board.
     *
     * AI Systems are Resource Groups with group_type='ai_system'; this is a
     * read-only projection over them with governance attributes,
     * composition counts and governance gaps.
     */
    async fetchAISystems(filters?: AISystemFleetFilters): Promise<AISystemFleetResponse> {
        const params = new URLSearchParams();
        if (filters?.system_kind) params.append('system_kind', filters.system_kind);
        if (filters?.lifecycle_stage) params.append('lifecycle_stage', filters.lifecycle_stage);
        if (filters?.risk_tier) params.append('risk_tier', filters.risk_tier);
        if (filters?.owner) params.append('owner', filters.owner);
        if (filters?.undocumented_only) params.append('undocumented_only', 'true');
        if (filters?.role) params.append('role', filters.role);
        if (filters?.include_inactive) params.append('include_inactive', 'true');
        if (filters?.include_health) params.append('include_health', 'true');
        if (filters?.include_card) params.append('include_card', 'true');
        if (filters?.limit !== undefined) params.append('limit', String(filters.limit));
        if (filters?.offset !== undefined) params.append('offset', String(filters.offset));

        const qs = params.toString();
        return this.request<AISystemFleetResponse>(
            `/v1/admin/ai-systems${qs ? `?${qs}` : ''}`
        );
    }

    /**
     * Runs for one AI System.
     */
    async fetchAISystemRuns(
        aiSystemId: string,
        filters?: RunListFilters
    ): Promise<RunListResponse> {
        const params = new URLSearchParams();
        if (filters?.terminal_state) params.append('terminal_state', filters.terminal_state);
        if (filters?.run_class) params.append('run_class', filters.run_class);
        if (filters?.period_hours !== undefined) params.append('period_hours', String(filters.period_hours));
        if (filters?.include_replays) params.append('include_replays', 'true');
        if (filters?.limit !== undefined) params.append('limit', String(filters.limit));
        if (filters?.offset !== undefined) params.append('offset', String(filters.offset));
        if (filters?.q) params.append('q', filters.q);
        const qs = params.toString();
        return this.request<RunListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/runs${qs ? `?${qs}` : ''}`
        );
    }

    /**
     * Calls a shared service served inside other systems' runs — its segments, newest first.
     */
    async fetchAISystemServedCalls(
        aiSystemId: string,
        filters?: { period_hours?: number; limit?: number; offset?: number }
    ): Promise<ServedCallListResponse> {
        const params = new URLSearchParams();
        if (filters?.period_hours !== undefined) params.append('period_hours', String(filters.period_hours));
        if (filters?.limit !== undefined) params.append('limit', String(filters.limit));
        if (filters?.offset !== undefined) params.append('offset', String(filters.offset));
        const qs = params.toString();
        return this.request<ServedCallListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/served-calls${qs ? `?${qs}` : ''}`
        );
    }

    /**
     * The four run signals for one AI System.
     */
    async fetchAISystemRunStats(
        aiSystemId: string,
        periodHours = 24,
        range?: { start?: string; end?: string }
    ): Promise<RunStatsResponse> {
        const sp = new URLSearchParams({ period_hours: String(periodHours) });
        if (range?.start) sp.append('start', range.start);
        if (range?.end) sp.append('end', range.end);
        return this.request<RunStatsResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/runs/stats?${sp}`
        );
    }

    /**
     * Bucketed run signals for charts — hourly at 48h and under, daily
     * above. Sparse: quiet buckets are omitted, the chart zero-fills.
     */
    async fetchAISystemRunTimeseries(
        aiSystemId: string,
        periodHours = 24
    ): Promise<RunTimeseriesResponse> {
        const sp = new URLSearchParams({ period_hours: String(periodHours) });
        return this.request<RunTimeseriesResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/runs/timeseries?${sp}`
        );
    }

    /**
     * One run, in full.
     */
    async fetchRun(runId: string): Promise<RunDetail> {
        return this.request<RunDetail>(`/v1/admin/runs/${runId}`);
    }

    // ── Run interventions ──────────────────────────

    /**
     * Runs currently in flight.
     *
     * Derived on read from `proxy_logs` — there is no live-run table,
     * because that would put a write on the hot path for every run and
     * almost no run is ever intervened on.
     */
    async fetchLiveRuns(windowSeconds?: number): Promise<LiveRunsResponse> {
        const q = windowSeconds ? `?window_seconds=${windowSeconds}` : '';
        return this.request<LiveRunsResponse>(`/v1/admin/ai-systems/runs/live${q}`);
    }

    /**
     * Stop one in-flight run. `reason` is required — an intervention with
     * no stated reason is not evidence.
     *
     * Abort stops the NEXT governed action; a call already in flight
     * completes. That limit is stated in the response and must be stated in
     * the confirm dialog too.
     */
    async abortRun(rootTaskId: string, reason: string): Promise<AbortRunResponse> {
        return this.request<AbortRunResponse>(
            `/v1/admin/ai-systems/runs/${encodeURIComponent(rootTaskId)}/abort`,
            { method: 'POST', body: JSON.stringify({ reason }) },
        );
    }

    /** The intervention on one run, if any. */
    async fetchRunIntervention(rootTaskId: string): Promise<{ intervention: RunIntervention | null }> {
        return this.request<{ intervention: RunIntervention | null }>(
            `/v1/admin/ai-systems/runs/${encodeURIComponent(rootTaskId)}/intervention`,
        );
    }

    /** Step → Turn → Action rollup for one run. */
    async fetchRunTrajectory(runId: string, stepId?: string): Promise<TrajectoryResponse> {
        const q = stepId ? `?step_id=${encodeURIComponent(stepId)}` : '';
        return this.request<TrajectoryResponse>(`/v1/admin/runs/${runId}/trajectory${q}`);
    }

    /** Tool actions that ran within `within` actions of a guardrail/policy trip.
     *  Co-occurrence in execution order — not a causal claim. */
    async fetchRunConsequences(runId: string, within = 10): Promise<ConsequenceResponse> {
        return this.request<ConsequenceResponse>(
            `/v1/admin/runs/${runId}/consequences?within=${within}`,
        );
    }

    /** Fleet-wide action search — the entry point into a trajectory. */
    async searchActions(params: {
        ai_system_id?: string;
        capability?: string;
        proxy_type?: string;
        step_name?: string;
        only_blocked?: boolean;
        period_hours?: number;
        limit?: number;
    } = {}): Promise<ActionSearchResponse> {
        const q = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== '' && v !== false) q.append(k, String(v));
        });
        const qs = q.toString();
        return this.request<ActionSearchResponse>(`/v1/admin/actions${qs ? `?${qs}` : ''}`);
    }

    /** Drift findings for one AI System. */
    async fetchAISystemDrift(aiSystemId: string, periodHours = 720): Promise<DriftListResponse> {
        return this.request<DriftListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/drift?period_hours=${periodHours}`
        );
    }

    /** Tenant-wide drift feed — the alerts drawer reads this. */
    async fetchTenantDriftFeed(params?: {
        status?: string;
        drift_class?: string;
        period_hours?: number;
        limit?: number;
    }): Promise<DriftListResponse> {
        const q = new URLSearchParams();
        if (params?.status) q.append('status', params.status);
        if (params?.drift_class) q.append('drift_class', params.drift_class);
        if (params?.period_hours !== undefined) q.append('period_hours', String(params.period_hours));
        if (params?.limit !== undefined) q.append('limit', String(params.limit));
        const qs = q.toString();
        return this.request<DriftListResponse>(`/v1/admin/drift${qs ? `?${qs}` : ''}`);
    }

    /**
     * Mission Control card dashboard: the cards for
     * one hierarchy level — root when no parent_id, flat when searching.
     *
     * The window scales measurements only; health/verdict/T-class are
     * pinned judgments from the materialized health row.
     */
    async fetchMissionControlCards(
        params?: MissionControlCardsParams
    ): Promise<MissionControlCardsResponse> {
        const q = new URLSearchParams();
        if (params?.parent_id) q.append('parent_id', params.parent_id);
        if (params?.window) q.append('window', params.window);
        if (params?.q) q.append('q', params.q);
        if (params?.group_type) q.append('group_type', params.group_type);
        if (params?.sort) q.append('sort', params.sort);
        if (params?.limit !== undefined) q.append('limit', String(params.limit));
        if (params?.offset !== undefined) q.append('offset', String(params.offset));
        const qs = q.toString();
        return this.request<MissionControlCardsResponse>(
            `/v1/admin/mission-control/cards${qs ? `?${qs}` : ''}`
        );
    }

    /** Operator-facing tokenomics concept explainers for the info boxes. */
    async fetchMissionControlExplainers(): Promise<MissionControlExplainersResponse> {
        return this.request<MissionControlExplainersResponse>(
            '/v1/admin/mission-control/explainers'
        );
    }

    /** What normal looks like for one AI System. */
    async fetchAISystemBaselines(aiSystemId: string): Promise<BaselineListResponse> {
        return this.request<BaselineListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/baselines`
        );
    }

    /** Acknowledge a finding ("investigating"). Never closes it. */
    async acknowledgeDriftEvent(eventId: string, note?: string): Promise<DriftEvent> {
        return this.request<DriftEvent>(`/v1/admin/drift/${encodeURIComponent(eventId)}/ack`, {
            method: 'POST',
            body: JSON.stringify({ note }),
        });
    }

    /**
     * Close a finding with a kind. Each kind has one effect the
     * detector honours — an epoch or an incident window — and the response
     * carries it with its seal. `false_positive` goes to its own route (it
     * feeds detector tuning); the other kinds to `/resolve`.
     */
    async closeDriftEvent(eventId: string, request: CloseDriftRequest): Promise<DriftResolution> {
        const action = request.kind === 'false_positive' ? 'false-positive' : 'resolve';
        return this.request<DriftResolution>(
            `/v1/admin/drift/${encodeURIComponent(eventId)}/${action}`,
            { method: 'POST', body: JSON.stringify(request) }
        );
    }

    /** Baseline epochs declared for one AI System, newest first. */
    async fetchAISystemBaselineEpochs(aiSystemId: string): Promise<BaselineEpochListResponse> {
        return this.request<BaselineEpochListResponse>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/baseline-epochs`
        );
    }

    /** Incident windows that apply to one AI System (its own and
     *  tenant-wide), optionally only those overlapping [since, until). */
    async fetchAISystemIncidentWindows(
        aiSystemId: string,
        params?: { since?: string; until?: string }
    ): Promise<IncidentWindowListResponse> {
        const q = new URLSearchParams();
        if (params?.since) q.append('since', params.since);
        if (params?.until) q.append('until', params.until);
        const qs = q.toString();
        return this.request<IncidentWindowListResponse>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/incident-windows${qs ? `?${qs}` : ''}`
        );
    }

    /** Every incident window in the tenant. */
    async fetchIncidentWindows(params?: {
        ai_system_id?: string;
        since?: string;
        until?: string;
    }): Promise<IncidentWindowListResponse> {
        const q = new URLSearchParams();
        if (params?.ai_system_id) q.append('ai_system_id', params.ai_system_id);
        if (params?.since) q.append('since', params.since);
        if (params?.until) q.append('until', params.until);
        const qs = q.toString();
        return this.request<IncidentWindowListResponse>(`/v1/admin/incident-windows${qs ? `?${qs}` : ''}`);
    }

    /** Declare a window: for `aiSystemId`, else as the body says (no
     *  `ai_system_id` = every system in the tenant). Sealed. */
    async createIncidentWindow(
        body: IncidentWindowCreate,
        aiSystemId?: string
    ): Promise<IncidentWindowWriteResponse> {
        const path = aiSystemId
            ? `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/incident-windows`
            : '/v1/admin/incident-windows';
        return this.request<IncidentWindowWriteResponse>(path, {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /** Edit a window — a new sealed declaration. The scope is not editable. */
    async updateIncidentWindow<Sent>(
        windowId: string,
        body: ApiBodyExactly<'/v1/admin/incident-windows/{window_id}', 'patch', Sent>
    ): Promise<IncidentWindowWriteResponse> {
        return this.request<IncidentWindowWriteResponse>(
            `/v1/admin/incident-windows/${encodeURIComponent(windowId)}`,
            { method: 'PATCH', body: JSON.stringify(body) }
        );
    }

    /** Withdraw a window (sealed): its runs count again. */
    async deleteIncidentWindow(windowId: string): Promise<IncidentWindowDeleteResponse> {
        return this.request<IncidentWindowDeleteResponse>(
            `/v1/admin/incident-windows/${encodeURIComponent(windowId)}`,
            { method: 'DELETE' }
        );
    }

    // -----------------------------------------------------------------
    // Health + the Assurance Report
    // -----------------------------------------------------------------

    async fetchSystemHealth(aiSystemId: string, refresh = false): Promise<SystemHealth> {
        return this.request<SystemHealth>(
            `/v1/admin/ai-systems/${aiSystemId}/health?refresh=${refresh}`
        );
    }

    async fetchAssuranceReport(
        aiSystemId: string,
        periodHours = 720
    ): Promise<Record<string, unknown>> {
        return this.request<Record<string, unknown>>(
            `/v1/admin/ai-systems/${aiSystemId}/assurance?period_hours=${periodHours}`
        );
    }

    async captureAssuranceReport(
        aiSystemId: string,
        periodHours = 720
    ): Promise<unknown> {
        return this.request<unknown>(
            `/v1/admin/ai-systems/${aiSystemId}/assurance`,
            { method: 'POST', body: JSON.stringify({ period_hours: periodHours }) }
        );
    }

    async fetchAssuranceHistory(aiSystemId: string): Promise<AssuranceHistory> {
        return this.request<AssuranceHistory>(
            `/v1/admin/ai-systems/${aiSystemId}/assurance/history`
        );
    }

    /**
     * Liveness expectation + current status.
     */
    async fetchAISystemLiveness(aiSystemId: string): Promise<LivenessStatus> {
        return this.request<LivenessStatus>(
            `/v1/admin/ai-systems/${aiSystemId}/liveness`
        );
    }

    async updateAISystemLiveness<Sent>(
        aiSystemId: string,
        payload: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/liveness', 'put', Sent>
    ): Promise<LivenessExpectation> {
        return this.request<LivenessExpectation>(
            `/v1/admin/ai-systems/${aiSystemId}/liveness`,
            { method: 'PUT', body: JSON.stringify(payload) }
        );
    }

    /** Propose a cadence from observed runs. Writes nothing. */
    async learnAISystemLiveness(aiSystemId: string): Promise<CadenceProposal> {
        return this.request<CadenceProposal>(
            `/v1/admin/ai-systems/${aiSystemId}/liveness/learn`,
            { method: 'POST' }
        );
    }

    /** Take the learned cadence as the expectation. */
    async acceptAISystemLiveness(aiSystemId: string): Promise<LivenessExpectation> {
        return this.request<LivenessExpectation>(
            `/v1/admin/ai-systems/${aiSystemId}/liveness/accept`,
            { method: 'POST' }
        );
    }

    /**
     * Traffic not yet organised into an AI System — the adoption funnel.
     */
    async fetchUnattributedRuns(periodHours = 168): Promise<UnattributedRunsResponse> {
        return this.request<UnattributedRunsResponse>(
            `/v1/admin/runs/unattributed?period_hours=${periodHours}`
        );
    }

    /**
     * Fetch a single group with optional includes
     */
    async fetchResourceGroup(
        groupId: string,
        includes?: {
            stats?: boolean;
            ancestors?: boolean;
            children?: boolean;
        }
    ): Promise<ResourceGroup> {
        const params = new URLSearchParams();
        if (includes?.stats) params.append('include_stats', 'true');
        if (includes?.ancestors) params.append('include_ancestors', 'true');
        if (includes?.children) params.append('include_children', 'true');

        const queryString = params.toString();
        const endpoint = `/v1/admin/resource-groups/${groupId}${queryString ? `?${queryString}` : ''}`;

        return this.request<ResourceGroup>(endpoint);
    }

    /**
     * Create a new resource group
     */
    async createResourceGroup(data: CreateResourceGroupRequest): Promise<ResourceGroup> {
        return this.request<ResourceGroup>('/v1/admin/resource-groups', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /**
     * Update a resource group
     */
    async updateResourceGroup<Sent>(
        groupId: string,
        data: ApiBodyExactly<'/v1/admin/resource-groups/{group_id}', 'patch', Sent>
    ): Promise<ResourceGroup> {
        return this.request<ResourceGroup>(`/v1/admin/resource-groups/${groupId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    }

    /**
     * Delete a resource group
     */
    async deleteResourceGroup(groupId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/${groupId}`, {
            method: 'DELETE',
        });
    }

    // ============================================
    // Group Members Endpoints
    // ============================================

    /**
     * Fetch members of a group
     */
    async fetchGroupMembers(
        groupId: string,
        options?: {
            include_descendants?: boolean;
        }
    ): Promise<GroupMemberListResponse> {
        const params = new URLSearchParams();
        if (options?.include_descendants) {
            params.append('include_descendants', 'true');
        }

        const queryString = params.toString();
        const endpoint = `/v1/admin/resource-groups/${groupId}/members${queryString ? `?${queryString}` : ''}`;

        return this.request<GroupMemberListResponse>(endpoint);
    }

    /**
     * Add members to a group (batch operation).
     *
     * `makePrimary`: when true, set this group as each added user's primary
     * (active runtime) group. Ignored for the user's very first group —
     * that is always auto-set as primary regardless of this flag.
     */
    async addGroupMembers(
        groupId: string,
        userIds: string[],
        role?: string,
        makePrimary: boolean = false
    ): Promise<{
        added: { user_id: string; username: string; primary?: boolean }[];
        skipped: { user_id: string; username: string; reason: string }[];
    }> {
        return this.request(`/v1/admin/resource-groups/${groupId}/members`, {
            method: 'POST',
            body: JSON.stringify({ user_ids: userIds, role, make_primary: makePrimary }),
        });
    }

    /**
     * Remove a member from a group
     */
    async removeGroupMember(groupId: string, userId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/${groupId}/members/${userId}`, {
            method: 'DELETE',
        });
    }

    /**
     * Explicitly set a portal user's primary (active) resource group.
     * The user must already be a member of the target group.
     */
    async setUserPrimaryGroup(
        userId: string | number,
        groupId: string
    ): Promise<{ user_id: number; primary_group_id: string; previous_primary_group_id: string | null }> {
        return this.request(`/v1/admin/end-users/${userId}/primary-group`, {
            method: 'PATCH',
            body: JSON.stringify({ group_id: groupId } satisfies ApiRequestBody<'/v1/admin/end-users/{user_id}/primary-group', 'patch'>),
        });
    }
}
