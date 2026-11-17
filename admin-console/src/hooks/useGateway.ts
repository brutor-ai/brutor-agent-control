/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The gateway hook: every operation of the Admin Console against the Control
 * Plane, with the session's client and error handling underneath.
 *
 * It is composed from the hooks in ./gateway: `useGatewayCore`
 * (options, client, errors) and one hook per domain, each returning the
 * operations of its area. This file calls them in a fixed order.
 *
 * It is the hook of the Community Edition: the domain
 * hooks of the Enterprise features are not called here. A component of an
 * Enterprise feature calls `useEnterpriseGateway`
 * (`enterprise/src/hooks/useEnterpriseGateway.ts`), which is built on the
 * same core and returns those operations. What the two return together is
 * what this hook returned alone before
 * (`__tests__/recorded/useGateway.keys.json`).
 */

import { useGatewayCore, type UseGatewayOptions } from './gateway/core';
import { useResourceGroupsGateway } from './gateway/resourceGroups';
import { useGuardrailsGateway } from './gateway/guardrails';
import { useArgumentPoliciesGateway } from './gateway/argumentPolicies';
import { useGroupResourcesGateway } from './gateway/groupResources';
import { useLimitsGateway } from './gateway/limits';
import { useMcpServersGateway } from './gateway/mcpServers';
import { useHealthGateway } from './gateway/health';
import { useAdminUsersGateway } from './gateway/adminUsers';
import { useTenantGateway } from './gateway/tenant';
import { useMetricsGateway } from './gateway/metrics';
import { useAlertsGateway } from './gateway/alerts';
import { useOAuthGateway } from './gateway/oauth';
import { useProxyLogsGateway } from './gateway/proxyLogs';
import { useIdpGateway } from './gateway/idp';
import { useAuditExportGateway } from './gateway/auditExport';
import { useEndUsersGateway } from './gateway/endUsers';
import { useAgentSkillsGateway } from './gateway/agentSkills';

export const useGateway = (options: UseGatewayOptions = {}) => {
    const core = useGatewayCore(options);
    const { clearErrors, dismissError, errors, gatewayClient, gatewayUrl, getErrorsBySeverity, hasCriticalErrors, hasErrors, initialLoading, isAuthenticated, llmProxyUrl } = core;
    const { abortRun, acceptAISystemLiveness, acknowledgeDriftEvent, addGroupMembers, approveContract, buildReplaySuite, captureAssuranceReport, closeDriftEvent, createAISystemDependency, createAISystemEvidence, createIncidentWindow, createReachRule, createResourceGroup, createResponsePolicy, deleteAISystemDependency, deleteAISystemEvidence, deleteIncidentWindow, deleteReachRule, deleteResourceGroup, deleteResponsePolicy, exportAssuranceCertificate, exportContractDocument, fetchAISystemBaselineEpochs, fetchAISystemBaselines, fetchAISystemComposition, fetchAISystemContracts, fetchAISystemDrift, fetchAISystemEnvelope, fetchAISystemEvidence, fetchAISystemIncidentWindows, fetchAISystemLifecycle, fetchAISystemLiveness, fetchAISystemOutcomes, fetchAISystemRunStats, fetchAISystemRunTimeseries, fetchAISystemRuns, fetchAISystemServedCalls, fetchAISystems, fetchAssuranceHistory, fetchAssuranceReport, fetchContractVersion, fetchCurrentContract, fetchDataScope, fetchDataScopeActivity, fetchGroupHierarchyTree, fetchGroupMembers, fetchIncidentWindows, fetchLiveRuns, fetchMissionControlCards, fetchMissionControlExplainers, fetchObligationsBoard, fetchReachRules, fetchReplayStatus, fetchResourceGroup, fetchResponseHistory, fetchResponsePolicies, fetchRun, fetchRunConsequences, fetchRunIntervention, fetchRunTrajectory, fetchSystemHealth, fetchSystemObligations, fetchTenantDriftFeed, fetchUnattributedRuns, learnAISystemLiveness, listAgentCards, mintAISystemContract, previewReachRule, previewResponsePolicy, promoteContract, putAISystemEnvelope, putAISystemOutcomes, rejectContract, removeGroupMember, requestReplayRun, restoreAutonomy, searchActions, transitionAISystemLifecycle, updateAISystemDependency, updateAISystemLiveness, updateDataScope, updateIncidentWindow, updateReachRule, updateResourceGroup, updateResponsePolicy } = useResourceGroupsGateway(core);
    const { createGuardrailConfig, deleteGuardrailConfig, fetchEffectiveGuardrails, fetchGuardrailConfig, fetchGuardrailProviders, getGuardrailConfigById, listGuardrailConfigs, setGuardrailGroups, testGuardrails, updateGuardrailConfig, updateGuardrailConfigById } = useGuardrailsGateway(core);
    const { createArgumentPolicy, deleteArgumentPolicy, exportArgumentPoliciesYaml, getArgumentPolicy, importArgumentPoliciesYaml, listArgumentPolicies, setArgumentPolicyGroups, updateArgumentPolicy } = useArgumentPoliciesGateway(core);
    const { addModelToGroup, addServerToGroup, clearMCPCapabilityFilters, createGroupAPIKey, disableAPIKey, enableAPIKey, fetchEffectiveAgentSkills, fetchEffectiveLLMModels, fetchEffectiveServerConfigs, fetchGroupAPIKeyUsage, fetchGroupAPIKeys, getMCPCapabilityFilters, removeModelFromGroup, removeServerFromGroup, removeSkillFromGroup, revokeAPIKey, updateMCPCapabilityFilters } = useGroupResourcesGateway(core);
    const { acknowledgeUsageAlert, clearLLMGlobalGovernance, clearLLMModelGovernance, clearLLMModelLimits, clearMCPGlobalGovernance, clearMCPServerLimits, clearSkillGlobalGovernance, clearSkillGovernance, clearSkillItemLimits, fetchAllUsageAlerts, fetchGroupAlerts, fetchGroupUsage, fetchLLMGlobalLimits, fetchLLMModelLimits, fetchMCPGlobalLimits, fetchMCPServerLimits, fetchMediaGlobalGovernance, fetchMediaGlobalLimits, fetchSkillGlobalLimits, fetchSkillItemLimits, getLLMGlobalGovernance, getLLMModelGovernance, getMCPGlobalGovernance, getSkillGlobalGovernance, getSkillGovernance, updateLLMGlobalGovernance, updateLLMGlobalLimits, updateLLMModelGovernance, updateLLMModelLimits, updateMCPGlobalGovernance, updateMCPGlobalLimits, updateMCPServerLimits, updateMediaGlobalGovernance, updateMediaGlobalLimits, updateSkillGlobalGovernance, updateSkillGlobalLimits, updateSkillGovernance, updateSkillItemLimits } = useLimitsGateway(core);
    const { createServer, createServerConfig, createVirtualServer, deleteServer, deleteServerConfig, deleteServerIcon, deleteVirtualServer, fetchServerConfigs, fetchServers, fetchVirtualServers, getServerIconUrl, refreshServerCapabilities, testServerConnection, updateServer, updateServerConfig, updateVirtualServer, uploadServerIcon } = useMcpServersGateway(core);
    const { fetchStats, triggerAllHealthChecks, triggerServerHealthCheck } = useHealthGateway(core);
    const { createUser, deleteUser, fetchRoles, fetchUsers, getUserDisplayName, getUserRoles, getUsernameById, loadingUsers, updateUser, users, usersLoaded } = useAdminUsersGateway(core);
    const { fetchCurrentTenantThemeVariants } = useTenantGateway(core);
    const { exportMetricsData, fetchDashboardMetrics, fetchErrorRateTimeSeries, fetchHourlyHeatmap, fetchLatencyTimeSeries, fetchPerformanceMetrics, fetchRealTimeMetrics, fetchRequestsTimeSeries, fetchServerHealthMetrics, fetchTenantAuditLogs } = useMetricsGateway(core);
    const { acknowledgeSystemAlert, fetchAssuranceInboxCounts, fetchSystemAlerts, resolveSystemAlert } = useAlertsGateway(core);
    const { createOAuthAppClient, deleteOAuthAppClient, fetchOAuthAppClients, fetchOAuthTokens, getOAuthAppClientUsage, revokeOAuthToken, revokeOAuthTokensBulk, updateOAuthAppClient } = useOAuthGateway(core);
    const { exportAuditLogs, exportProxyLogs, fetchProxyLogs, fetchProxyStats } = useProxyLogsGateway(core);
    const { createIdPConfig, deleteIdPConfig, discoverIdPEndpoints, fetchIdPConfig, fetchIdPConfigs, testExistingIdP, updateIdPConfig } = useIdpGateway(core);
    const { createAuditExportDestination, deleteAuditExportDestination, fetchAuditExportDestination, fetchAuditExportDestinations, runAuditExportDestinationNow, updateAuditExportDestination } = useAuditExportGateway(core);
    const { deleteEndUser, exportEndUsers, fetchEndUserDetails, fetchEndUsers, updateEndUserStatus } = useEndUsersGateway(core);
    const { createSkillAsset, createSkillResource, createSkillScript, deleteSkillAsset, deleteSkillResource, deleteSkillScript, getSkillAsset, getSkillResource, getSkillScript, listSkillAssets, listSkillResources, listSkillScripts, updateSkillAsset, updateSkillResource, updateSkillScript } = useAgentSkillsGateway(core);

    return {
        // Gateway URLs
        gatewayUrl,
        llmProxyUrl,

        // Auth and loading state
        isAuthenticated,
        initialLoading,

        // User management data
        users,
        usersLoaded,
        loadingUsers,

        // Error handling
        errors,
        hasErrors,
        hasCriticalErrors,
        dismissError,
        clearErrors,
        getErrorsBySeverity,

        // Data fetching functions
        fetchServers,
        fetchVirtualServers,
        fetchStats,

        // The console's own tenant
        fetchCurrentTenantThemeVariants,

        // Server actions
        createServer,
        updateServer,
        refreshServerCapabilities,
        deleteServer,

        // Icon management actions
        uploadServerIcon,
        deleteServerIcon,
        getServerIconUrl,

        // Virtual server actions
        createVirtualServer,
        updateVirtualServer,
        deleteVirtualServer,

        // Health check actions
        triggerServerHealthCheck,
        triggerAllHealthChecks,

        // Transport layer actions
        testServerConnection,

        // Admin user management actions
        fetchUsers,
        createUser,
        updateUser,
        deleteUser,
        fetchRoles,

        // User helper functions
        getUsernameById,
        getUserDisplayName,
        getUserRoles,

        // Identity Provider (IdP) management actions
        fetchIdPConfigs,
        fetchIdPConfig,
        createIdPConfig,
        updateIdPConfig,
        deleteIdPConfig,
        discoverIdPEndpoints,
        testExistingIdP,

        // Audit export destinations
        fetchAuditExportDestinations,
        fetchAuditExportDestination,
        createAuditExportDestination,
        updateAuditExportDestination,
        deleteAuditExportDestination,
        runAuditExportDestinationNow,

        // OAuth app client management actions
        fetchOAuthAppClients,
        createOAuthAppClient,
        updateOAuthAppClient,
        deleteOAuthAppClient,
        getOAuthAppClientUsage,

        // Server config management actions
        fetchServerConfigs,
        createServerConfig,
        updateServerConfig,
        deleteServerConfig,

        // End user management actions (Admin)
        fetchEndUsers,
        fetchEndUserDetails,
        updateEndUserStatus,
        deleteEndUser,
        exportEndUsers,

        // Guardrails Management
        fetchEffectiveGuardrails,
        fetchGuardrailConfig,
        updateGuardrailConfig,
        testGuardrails,
        fetchGuardrailProviders,
        listGuardrailConfigs,
        createGuardrailConfig,

        // Argument policies
        listArgumentPolicies,
        getArgumentPolicy,
        createArgumentPolicy,
        updateArgumentPolicy,
        deleteArgumentPolicy,
        setArgumentPolicyGroups,
        exportArgumentPoliciesYaml,
        importArgumentPoliciesYaml,

        getGuardrailConfigById,
        updateGuardrailConfigById,
        deleteGuardrailConfig,
        setGuardrailGroups,

        // Resource Group Management
        fetchGroupHierarchyTree,
        fetchAISystems,
        fetchAISystemRuns,
        fetchAISystemServedCalls,
        fetchAISystemRunStats,
        fetchAISystemRunTimeseries,
        fetchRunTrajectory,
        fetchRunConsequences,
        searchActions,
        fetchRun,
        fetchLiveRuns,
        abortRun,
        fetchRunIntervention,
        fetchUnattributedRuns,
        fetchAISystemLiveness,
        updateAISystemLiveness,
        learnAISystemLiveness,
        acceptAISystemLiveness,
        fetchAISystemDrift,
        fetchTenantDriftFeed,
        fetchMissionControlCards,
        fetchMissionControlExplainers,
        fetchObligationsBoard,
        fetchSystemObligations,
        fetchAISystemBaselines,
        acknowledgeDriftEvent,
        closeDriftEvent,
        fetchAISystemBaselineEpochs,
        fetchAISystemIncidentWindows,
        fetchIncidentWindows,
        createIncidentWindow,
        updateIncidentWindow,
        deleteIncidentWindow,
        fetchAISystemContracts,
        fetchAISystemComposition,
        createAISystemDependency,
        updateAISystemDependency,
        deleteAISystemDependency,
        listAgentCards,
        fetchContractVersion,
        fetchCurrentContract,
        mintAISystemContract,
        approveContract,
        rejectContract,
        promoteContract,
        fetchAISystemLifecycle,
        transitionAISystemLifecycle,
        fetchAISystemEnvelope,
        putAISystemEnvelope,
        fetchAISystemOutcomes,
        putAISystemOutcomes,
        exportContractDocument,
        exportAssuranceCertificate,
        fetchAISystemEvidence,
        createAISystemEvidence,
        deleteAISystemEvidence,
        fetchSystemHealth,
        fetchAssuranceReport,
        captureAssuranceReport,
        fetchAssuranceHistory,
        fetchResponsePolicies,
        createResponsePolicy,
        previewResponsePolicy,
        updateResponsePolicy,
        deleteResponsePolicy,
        fetchResponseHistory,
        restoreAutonomy,
        fetchDataScope,
        updateDataScope,
        fetchDataScopeActivity,
        fetchReachRules,
        createReachRule,
        updateReachRule,
        deleteReachRule,
        previewReachRule,
        fetchReplayStatus,
        buildReplaySuite,
        requestReplayRun,
        fetchResourceGroup,
        createResourceGroup,
        updateResourceGroup,
        deleteResourceGroup,
        fetchGroupMembers,
        addGroupMembers,
        removeGroupMember,
        fetchEffectiveLLMModels,
        addModelToGroup,
        removeModelFromGroup,
        fetchEffectiveServerConfigs,
        addServerToGroup,
        removeServerFromGroup,
        getMCPCapabilityFilters,
        updateMCPCapabilityFilters,
        clearMCPCapabilityFilters,
        fetchEffectiveAgentSkills,
        removeSkillFromGroup,
        fetchGroupAPIKeys,
        fetchGroupAPIKeyUsage,
        createGroupAPIKey,
        enableAPIKey,
        disableAPIKey,
        revokeAPIKey,
        fetchLLMGlobalLimits,
        fetchMCPGlobalLimits,
        updateLLMGlobalLimits,
        updateMCPGlobalLimits,
        fetchSkillGlobalLimits,
        updateSkillGlobalLimits,
        fetchMediaGlobalLimits,
        updateMediaGlobalLimits,
        fetchMediaGlobalGovernance,
        updateMediaGlobalGovernance,
        fetchLLMModelLimits,
        updateLLMModelLimits,
        clearLLMModelLimits,
        fetchMCPServerLimits,
        updateMCPServerLimits,
        clearMCPServerLimits,
        fetchSkillItemLimits,
        updateSkillItemLimits,
        clearSkillItemLimits,
        getLLMModelGovernance,
        updateLLMModelGovernance,
        clearLLMModelGovernance,
        getLLMGlobalGovernance,
        updateLLMGlobalGovernance,
        clearLLMGlobalGovernance,
        getMCPGlobalGovernance,
        updateMCPGlobalGovernance,
        clearMCPGlobalGovernance,
        getSkillGlobalGovernance,
        updateSkillGlobalGovernance,
        clearSkillGlobalGovernance,
        getSkillGovernance,
        updateSkillGovernance,
        clearSkillGovernance,
        fetchGroupUsage,
        fetchAllUsageAlerts,
        fetchGroupAlerts,
        acknowledgeUsageAlert,

        // Proxy logs actions
        fetchProxyLogs,
        fetchProxyStats,
        exportProxyLogs,
        exportAuditLogs,

        // Enhanced metrics actions
        fetchPerformanceMetrics,
        fetchRealTimeMetrics,
        fetchServerHealthMetrics,
        fetchDashboardMetrics,
        fetchSystemAlerts,
        fetchAssuranceInboxCounts,
        acknowledgeSystemAlert,
        resolveSystemAlert,
        fetchOAuthTokens,
        revokeOAuthToken,
        revokeOAuthTokensBulk,
        exportMetricsData,
        fetchRequestsTimeSeries,
        fetchLatencyTimeSeries,
        fetchErrorRateTimeSeries,
        fetchHourlyHeatmap,
        fetchTenantAuditLogs,

        // Agent Skill Resources & Scripts
        listSkillResources,
        getSkillResource,
        createSkillResource,
        updateSkillResource,
        deleteSkillResource,
        listSkillScripts,
        getSkillScript,
        createSkillScript,
        updateSkillScript,
        deleteSkillScript,

        // Agent Skill Assets
        listSkillAssets,
        getSkillAsset,
        createSkillAsset,
        updateSkillAsset,
        deleteSkillAsset,

        // Client access for advanced usage
        gatewayClient
    };
};
