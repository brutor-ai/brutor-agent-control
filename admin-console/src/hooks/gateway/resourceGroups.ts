/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Resource groups and AI Systems: hierarchy, runs, drift, lifecycle, incident windows, the Assurance Report and members.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type {
    AISystemFleetFilters,
    AISystemFleetResponse,
    BuildSuiteRequest,
    RunListFilters,
    RunListResponse,
    ServedCallListResponse,
    RunStatsResponse,
    RunTimeseriesResponse,
    RunDetail,
    UnattributedRunsResponse,
    LivenessStatus,
    LivenessExpectation,
    CadenceProposal,
    DriftEvent,
    DriftListResponse,
    BaselineListResponse,
    DependencyDraft,
} from '../../lib/aiSystemTypes';
import type {
    CloseDriftRequest,
    DriftResolution,
    IncidentWindowCreate,
} from '../../lib/driftResolutionTypes';
import {
    GroupHierarchyResponse,
    GroupMemberListResponse,
    CreateResourceGroupRequest,
    ResourceGroup,
} from '../../lib/groupTypes';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';


export function useResourceGroupsGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // ============================================
    // Resource Group Operations
    // ============================================

    const fetchGroupHierarchyTree = useCallback(async (): Promise<GroupHierarchyResponse> => {
        return gatewayClient.fetchGroupHierarchyTree();
    }, []);

    // AI System fleet board.
    const fetchAISystems = useCallback(async (
        filters?: AISystemFleetFilters
    ): Promise<AISystemFleetResponse> => {
        return gatewayClient.fetchAISystems(filters);
    }, []);

    // The run ledger.
    const fetchAISystemRuns = useCallback(async (
        aiSystemId: string,
        filters?: RunListFilters
    ): Promise<RunListResponse> => {
        return gatewayClient.fetchAISystemRuns(aiSystemId, filters);
    }, []);

    // A shared service's work: the calls it served.
    const fetchAISystemServedCalls = useCallback(async (
        aiSystemId: string,
        filters?: { period_hours?: number; limit?: number; offset?: number }
    ): Promise<ServedCallListResponse> => {
        return gatewayClient.fetchAISystemServedCalls(aiSystemId, filters);
    }, [gatewayClient]);

    const fetchAISystemRunStats = useCallback(async (
        aiSystemId: string,
        periodHours?: number
    ): Promise<RunStatsResponse> => {
        return gatewayClient.fetchAISystemRunStats(aiSystemId, periodHours);
    }, []);

    const fetchAISystemRunTimeseries = useCallback(async (
        aiSystemId: string,
        periodHours?: number
    ): Promise<RunTimeseriesResponse> => {
        return gatewayClient.fetchAISystemRunTimeseries(aiSystemId, periodHours);
    }, []);

    const fetchRunTrajectory = useCallback(async (runId: string, stepId?: string) => {
        return gatewayClient.fetchRunTrajectory(runId, stepId);
    }, [gatewayClient]);

    const fetchRunConsequences = useCallback(async (runId: string, within?: number) => {
        return gatewayClient.fetchRunConsequences(runId, within);
    }, [gatewayClient]);

    const searchActions = useCallback(async (params: Parameters<typeof gatewayClient.searchActions>[0]) => {
        return gatewayClient.searchActions(params);
    }, [gatewayClient]);

    // Operator abort of one in-flight run.
    const fetchLiveRuns = useCallback(async (windowSeconds?: number) => {
        return gatewayClient.fetchLiveRuns(windowSeconds);
    }, [gatewayClient]);

    const abortRun = useCallback(async (rootTaskId: string, reason: string) => {
        return gatewayClient.abortRun(rootTaskId, reason);
    }, [gatewayClient]);

    const fetchRunIntervention = useCallback(async (rootTaskId: string) => {
        return gatewayClient.fetchRunIntervention(rootTaskId);
    }, [gatewayClient]);

    const fetchRun = useCallback(async (runId: string): Promise<RunDetail> => {
        return gatewayClient.fetchRun(runId);
    }, []);

    const fetchUnattributedRuns = useCallback(async (
        periodHours?: number
    ): Promise<UnattributedRunsResponse> => {
        return gatewayClient.fetchUnattributedRuns(periodHours);
    }, []);

    // Liveness.
    const fetchAISystemLiveness = useCallback(async (
        aiSystemId: string
    ): Promise<LivenessStatus> => {
        return gatewayClient.fetchAISystemLiveness(aiSystemId);
    }, []);

    const updateAISystemLiveness = useCallback(async <Sent>(
        aiSystemId: string,
        payload: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/liveness', 'put', Sent>
    ): Promise<LivenessExpectation> => {
        return gatewayClient.updateAISystemLiveness(aiSystemId, payload);
    }, []);

    const learnAISystemLiveness = useCallback(async (
        aiSystemId: string
    ): Promise<CadenceProposal> => {
        return gatewayClient.learnAISystemLiveness(aiSystemId);
    }, []);

    const acceptAISystemLiveness = useCallback(async (
        aiSystemId: string
    ): Promise<LivenessExpectation> => {
        return gatewayClient.acceptAISystemLiveness(aiSystemId);
    }, []);

    // Drift.
    const fetchAISystemDrift = useCallback(async (
        aiSystemId: string, periodHours?: number
    ): Promise<DriftListResponse> => {
        return gatewayClient.fetchAISystemDrift(aiSystemId, periodHours);
    }, []);

    const fetchTenantDriftFeed = useCallback(async (
        params?: Parameters<typeof gatewayClient.fetchTenantDriftFeed>[0]
    ): Promise<DriftListResponse> => {
        return gatewayClient.fetchTenantDriftFeed(params);
    }, []);

    // Mission Control card dashboard.
    const fetchMissionControlCards = useCallback(async (
        params?: Parameters<typeof gatewayClient.fetchMissionControlCards>[0]
    ) => {
        return gatewayClient.fetchMissionControlCards(params);
    }, []);

    const fetchMissionControlExplainers = useCallback(async () => {
        return gatewayClient.fetchMissionControlExplainers();
    }, []);

    // Regulatory obligations.
    const fetchObligationsBoard = useCallback(async (framework?: string) => {
        return gatewayClient.fetchObligationsBoard(framework);
    }, []);

    const fetchSystemObligations = useCallback(async (
        systemId: string, framework?: string
    ) => {
        return gatewayClient.fetchSystemObligations(systemId, framework);
    }, []);

    const fetchAISystemBaselines = useCallback(async (
        aiSystemId: string
    ): Promise<BaselineListResponse> => {
        return gatewayClient.fetchAISystemBaselines(aiSystemId);
    }, []);

    const acknowledgeDriftEvent = useCallback(async (
        eventId: string, note?: string
    ): Promise<DriftEvent> => {
        return gatewayClient.acknowledgeDriftEvent(eventId, note);
    }, [gatewayClient]);

    // --- Drift resolution the detector honours ------------------

    const closeDriftEvent = useCallback(async (
        eventId: string, request: CloseDriftRequest
    ): Promise<DriftResolution> => {
        return gatewayClient.closeDriftEvent(eventId, request);
    }, [gatewayClient]);

    const fetchAISystemBaselineEpochs = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAISystemBaselineEpochs(aiSystemId);
    }, [gatewayClient]);

    const fetchAISystemIncidentWindows = useCallback(async (
        aiSystemId: string, params?: { since?: string; until?: string }
    ) => {
        return gatewayClient.fetchAISystemIncidentWindows(aiSystemId, params);
    }, [gatewayClient]);

    const fetchIncidentWindows = useCallback(async (
        params?: { ai_system_id?: string; since?: string; until?: string }
    ) => {
        return gatewayClient.fetchIncidentWindows(params);
    }, [gatewayClient]);

    const createIncidentWindow = useCallback(async (
        body: IncidentWindowCreate, aiSystemId?: string
    ) => {
        return gatewayClient.createIncidentWindow(body, aiSystemId);
    }, [gatewayClient]);

    const updateIncidentWindow = useCallback(async <Sent>(
        windowId: string, body: ApiBodyExactly<'/v1/admin/incident-windows/{window_id}', 'patch', Sent>
    ) => {
        return gatewayClient.updateIncidentWindow(windowId, body);
    }, [gatewayClient]);

    const deleteIncidentWindow = useCallback(async (windowId: string) => {
        return gatewayClient.deleteIncidentWindow(windowId);
    }, [gatewayClient]);

    // --- Contracts + lifecycle gate ---------------------

    const fetchContractVersion = useCallback(
        async (aiSystemId: string, version: number) => {
            return gatewayClient.fetchContractVersion(aiSystemId, version);
        },
        [],
    );

    // Composition and declared dependency edges.
    const fetchAISystemComposition = useCallback(async (aiSystemId: string, windowHours?: number) => {
        return gatewayClient.fetchAISystemComposition(aiSystemId, windowHours);
    }, []);

    const createAISystemDependency = useCallback(async (aiSystemId: string, draft: DependencyDraft) => {
        return gatewayClient.createAISystemDependency(aiSystemId, draft);
    }, []);

    const updateAISystemDependency = useCallback(async <Sent>(
        aiSystemId: string,
        edgeId: string,
        patch: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/dependencies/{edge_id}', 'patch', Sent>,
    ) => {
        return gatewayClient.updateAISystemDependency(aiSystemId, edgeId, patch);
    }, []);

    const deleteAISystemDependency = useCallback(async (aiSystemId: string, edgeId: string) => {
        return gatewayClient.deleteAISystemDependency(aiSystemId, edgeId);
    }, []);

    const listAgentCards = useCallback(async () => {
        return gatewayClient.listA2AAgentCards();
    }, []);

    const fetchAISystemContracts = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAISystemContracts(aiSystemId);
    }, [gatewayClient]);

    const fetchCurrentContract = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchCurrentContract(aiSystemId);
    }, [gatewayClient]);

    const mintAISystemContract = useCallback(async (
        aiSystemId: string,
        opts: { force?: boolean } = {},
    ) => {
        return gatewayClient.mintAISystemContract(aiSystemId, opts);
    }, [gatewayClient]);

    const approveContract = useCallback(async (contractId: string) => {
        return gatewayClient.approveContract(contractId);
    }, [gatewayClient]);

    const rejectContract = useCallback(async (contractId: string, reason: string) => {
        return gatewayClient.rejectContract(contractId, reason);
    }, [gatewayClient]);

    const promoteContract = useCallback(async (contractId: string) => {
        return gatewayClient.promoteContract(contractId);
    }, [gatewayClient]);

    // --- The closed loop --------------------------------

    // --- Health + assurance -----------------------------

    const fetchSystemHealth = useCallback(async (
        aiSystemId: string, refresh = false
    ) => {
        return gatewayClient.fetchSystemHealth(aiSystemId, refresh);
    }, [gatewayClient]);

    const fetchAssuranceReport = useCallback(async (
        aiSystemId: string, periodHours = 720
    ) => {
        return gatewayClient.fetchAssuranceReport(aiSystemId, periodHours);
    }, [gatewayClient]);

    const captureAssuranceReport = useCallback(async (
        aiSystemId: string, periodHours = 720
    ) => {
        return gatewayClient.captureAssuranceReport(aiSystemId, periodHours);
    }, [gatewayClient]);

    const fetchAssuranceHistory = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAssuranceHistory(aiSystemId);
    }, [gatewayClient]);

    const fetchResponsePolicies = useCallback(async (
        aiSystemId: string, includeInherited = true
    ) => {
        return gatewayClient.fetchResponsePolicies(aiSystemId, includeInherited);
    }, [gatewayClient]);

    const createResponsePolicy = useCallback(async (
        aiSystemId: string, body: Record<string, unknown>
    ) => {
        return gatewayClient.createResponsePolicy(aiSystemId, body);
    }, [gatewayClient]);

    const previewResponsePolicy = useCallback(async (
        aiSystemId: string, body: Record<string, unknown>
    ) => {
        return gatewayClient.previewResponsePolicy(aiSystemId, body);
    }, [gatewayClient]);

    const updateResponsePolicy = useCallback(async <Sent>(
        policyId: string, body: ApiBodyExactly<'/v1/admin/response-policies/{policy_id}', 'patch', Sent>
    ) => {
        return gatewayClient.updateResponsePolicy(policyId, body);
    }, [gatewayClient]);

    const deleteResponsePolicy = useCallback(async (policyId: string) => {
        return gatewayClient.deleteResponsePolicy(policyId);
    }, [gatewayClient]);

    const fetchResponseHistory = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchResponseHistory(aiSystemId);
    }, [gatewayClient]);

    const restoreAutonomy = useCallback(async (
        aiSystemId: string, body: Record<string, unknown>
    ) => {
        return gatewayClient.restoreAutonomy(aiSystemId, body);
    }, [gatewayClient]);

    // Data scope and record reach
    const fetchDataScope = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchDataScope(aiSystemId);
    }, [gatewayClient]);

    const updateDataScope = useCallback(async <Sent>(
        aiSystemId: string, body: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/data-scope', 'put', Sent>
    ) => {
        return gatewayClient.updateDataScope(aiSystemId, body);
    }, [gatewayClient]);

    const fetchDataScopeActivity = useCallback(async (aiSystemId: string, days = 7) => {
        return gatewayClient.fetchDataScopeActivity(aiSystemId, days);
    }, [gatewayClient]);

    const fetchReachRules = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchReachRules(aiSystemId);
    }, [gatewayClient]);

    const createReachRule = useCallback(async (
        aiSystemId: string, body: import('../../lib/aiSystemTypes').ReachRuleBody
    ) => {
        return gatewayClient.createReachRule(aiSystemId, body);
    }, [gatewayClient]);

    const updateReachRule = useCallback(async <Sent>(
        aiSystemId: string, ruleId: string, body: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/reach-rules/{rule_id}', 'patch', Sent>
    ) => {
        return gatewayClient.updateReachRule(aiSystemId, ruleId, body);
    }, [gatewayClient]);

    const deleteReachRule = useCallback(async (aiSystemId: string, ruleId: string) => {
        return gatewayClient.deleteReachRule(aiSystemId, ruleId);
    }, [gatewayClient]);

    const previewReachRule = useCallback(async (
        aiSystemId: string, body: import('../../lib/aiSystemTypes').ReachRuleBody
    ) => {
        return gatewayClient.previewReachRule(aiSystemId, body);
    }, [gatewayClient]);

    const fetchReplayStatus = useCallback(
        async (aiSystemId: string, suiteId?: string) => {
            return gatewayClient.fetchReplayStatus(aiSystemId, suiteId);
        },
        [gatewayClient]
    );

    const buildReplaySuite = useCallback(
        async (aiSystemId: string, body: BuildSuiteRequest) => {
            return gatewayClient.buildReplaySuite(aiSystemId, body);
        },
        [gatewayClient]
    );

    const requestReplayRun = useCallback(
        async (aiSystemId: string, suiteId?: string) => {
            return gatewayClient.requestReplayRun(aiSystemId, suiteId);
        },
        [gatewayClient]
    );

    const fetchAISystemLifecycle = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAISystemLifecycle(aiSystemId);
    }, [gatewayClient]);

    const transitionAISystemLifecycle = useCallback(async (
        aiSystemId: string,
        payload: import('../../lib/aiSystemTypes').TransitionRequest
    ) => {
        return gatewayClient.transitionAISystemLifecycle(aiSystemId, payload);
    }, [gatewayClient]);

    const fetchAISystemEnvelope = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAISystemEnvelope(aiSystemId);
    }, [gatewayClient]);

    const putAISystemEnvelope = useCallback(async (
        aiSystemId: string,
        envelope: Record<string, number> | null
    ) => {
        return gatewayClient.putAISystemEnvelope(aiSystemId, envelope);
    }, [gatewayClient]);

    const fetchAISystemOutcomes = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAISystemOutcomes(aiSystemId);
    }, [gatewayClient]);

    const putAISystemOutcomes = useCallback(async (
        aiSystemId: string,
        outcomes: Record<string, unknown>[] | null
    ) => {
        return gatewayClient.putAISystemOutcomes(aiSystemId, outcomes);
    }, [gatewayClient]);

    const exportContractDocument = useCallback(async (
        aiSystemId: string,
        version: number
    ) => {
        return gatewayClient.exportContractDocument(aiSystemId, version);
    }, [gatewayClient]);

    const exportAssuranceCertificate = useCallback(async (
        aiSystemId: string,
        reportId: string
    ) => {
        return gatewayClient.exportAssuranceCertificate(aiSystemId, reportId);
    }, [gatewayClient]);

    const fetchAISystemEvidence = useCallback(async (aiSystemId: string) => {
        return gatewayClient.fetchAISystemEvidence(aiSystemId);
    }, [gatewayClient]);

    const createAISystemEvidence = useCallback(async (
        aiSystemId: string,
        payload: import('../../lib/aiSystemTypes').EvidenceCreateRequest
    ) => {
        return gatewayClient.createAISystemEvidence(aiSystemId, payload);
    }, [gatewayClient]);

    const deleteAISystemEvidence = useCallback(async (
        aiSystemId: string,
        evidenceId: string
    ) => {
        return gatewayClient.deleteAISystemEvidence(aiSystemId, evidenceId);
    }, [gatewayClient]);


    const fetchResourceGroup = useCallback(async (
        groupId: string,
        includes?: {
            stats?: boolean;
            ancestors?: boolean;
            children?: boolean;
        }
    ): Promise<ResourceGroup> => {
        return gatewayClient.fetchResourceGroup(groupId, includes);
    }, []);

    const createResourceGroup = useCallback(async (data: CreateResourceGroupRequest): Promise<ResourceGroup> => {
        return gatewayClient.createResourceGroup(data);
    }, []);

    const updateResourceGroup = useCallback(async <Sent>(
        groupId: string,
        data: ApiBodyExactly<'/v1/admin/resource-groups/{group_id}', 'patch', Sent>
    ): Promise<ResourceGroup> => {
        return gatewayClient.updateResourceGroup(groupId, data);
    }, []);

    const deleteResourceGroup = useCallback(async (groupId: string): Promise<void> => {
        return gatewayClient.deleteResourceGroup(groupId);
    }, []);

    // ============================================
    // Group Members Operations
    // ============================================

    const fetchGroupMembers = useCallback(async (
        groupId: string,
        options?: {
            include_descendants?: boolean;
        }
    ): Promise<GroupMemberListResponse> => {
        return gatewayClient.fetchGroupMembers(groupId, options);
    }, []);

    const addGroupMembers = useCallback(async (
        groupId: string,
        userIds: string[],
        role?: string,
        makePrimary: boolean = false
    ) => {
        return gatewayClient.addGroupMembers(groupId, userIds, role, makePrimary);
    }, []);

    const removeGroupMember = useCallback(async (
        groupId: string,
        userId: string
    ): Promise<void> => {
        return gatewayClient.removeGroupMember(groupId, userId);
    }, []);

    return {
        abortRun,
        acceptAISystemLiveness,
        acknowledgeDriftEvent,
        addGroupMembers,
        approveContract,
        buildReplaySuite,
        captureAssuranceReport,
        closeDriftEvent,
        createAISystemDependency,
        createAISystemEvidence,
        createIncidentWindow,
        createReachRule,
        createResourceGroup,
        createResponsePolicy,
        deleteAISystemDependency,
        deleteAISystemEvidence,
        deleteIncidentWindow,
        deleteReachRule,
        deleteResourceGroup,
        deleteResponsePolicy,
        exportAssuranceCertificate,
        exportContractDocument,
        fetchAISystemBaselineEpochs,
        fetchAISystemBaselines,
        fetchAISystemComposition,
        fetchAISystemContracts,
        fetchAISystemDrift,
        fetchAISystemEnvelope,
        fetchAISystemEvidence,
        fetchAISystemIncidentWindows,
        fetchAISystemLifecycle,
        fetchAISystemLiveness,
        fetchAISystemOutcomes,
        fetchAISystemRunStats,
        fetchAISystemRunTimeseries,
        fetchAISystemRuns,
        fetchAISystemServedCalls,
        fetchAISystems,
        fetchAssuranceHistory,
        fetchAssuranceReport,
        fetchContractVersion,
        fetchCurrentContract,
        fetchDataScope,
        fetchDataScopeActivity,
        fetchGroupHierarchyTree,
        fetchGroupMembers,
        fetchIncidentWindows,
        fetchLiveRuns,
        fetchMissionControlCards,
        fetchMissionControlExplainers,
        fetchObligationsBoard,
        fetchReachRules,
        fetchReplayStatus,
        fetchResourceGroup,
        fetchResponseHistory,
        fetchResponsePolicies,
        fetchRun,
        fetchRunConsequences,
        fetchRunIntervention,
        fetchRunTrajectory,
        fetchSystemHealth,
        fetchSystemObligations,
        fetchTenantDriftFeed,
        fetchUnattributedRuns,
        learnAISystemLiveness,
        listAgentCards,
        mintAISystemContract,
        previewReachRule,
        previewResponsePolicy,
        promoteContract,
        putAISystemEnvelope,
        putAISystemOutcomes,
        rejectContract,
        removeGroupMember,
        requestReplayRun,
        restoreAutonomy,
        searchActions,
        transitionAISystemLifecycle,
        updateAISystemDependency,
        updateAISystemLiveness,
        updateDataScope,
        updateIncidentWindow,
        updateReachRule,
        updateResourceGroup,
        updateResponsePolicy,
    };
}
