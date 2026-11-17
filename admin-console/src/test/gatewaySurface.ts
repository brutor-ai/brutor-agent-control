/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The table-driven tests of a gateway hook's API surface, shared by the
 * Community hook (`hooks/__tests__/useGateway.apiSurface.test.ts`) and the
 * Enterprise one (`enterprise/src/hooks/__tests__/`), and the Community
 * tables.
 *
 * Most of a gateway hook is a method per endpoint, so the bug it can have is
 * a boring and damaging one: the wrong client method, or arguments dropped
 * or swapped. The tables pin the mapping, and for the methods that report
 * failures, that each one still rethrows and raises a banner a surface can
 * filter on (`code` and `context.resource`). An auth failure belongs to the
 * auth layer, so it must not raise a banner as well.
 *
 * The rows are divided between the two editions;
 * `hooks/__tests__/recorded/useGateway.surface.json` is the undivided
 * record, and the Enterprise suite holds the two halves to it.
 */

import { act, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Mock } from 'vitest';

/** [hook method, client method, argument count] */
export type PassThroughRow = [string, string, number];
/** [hook method, client method, argument count, banner code, banner resource, auth failures stay silent] */
export type ReportingRow = [string, string, number, string, string | null, boolean];

export type HookFn = (...args: unknown[]) => Promise<unknown>;
type Hook = Record<string, HookFn>;

export const hookFn = (current: unknown, name: string) => (current as Hook)[name];
export const args = (n: number) => Array.from({ length: n }, (_, i) => ({ arg: i }));
export const httpError = (status: number, message: string, details?: unknown) =>
    Object.assign(new Error(message), { status, details });

/** [hook method, client method, argument count] */
export const PASS_THROUGH: PassThroughRow[] = [
    ['fetchGroupHierarchyTree', 'fetchGroupHierarchyTree', 0],
    ['fetchAISystems', 'fetchAISystems', 1],
    ['fetchAISystemRuns', 'fetchAISystemRuns', 2],
    ['fetchAISystemServedCalls', 'fetchAISystemServedCalls', 2],
    ['fetchAISystemRunStats', 'fetchAISystemRunStats', 2],
    ['fetchAISystemRunTimeseries', 'fetchAISystemRunTimeseries', 2],
    ['fetchRunTrajectory', 'fetchRunTrajectory', 2],
    ['fetchRunConsequences', 'fetchRunConsequences', 2],
    ['searchActions', 'searchActions', 1],
    ['fetchLiveRuns', 'fetchLiveRuns', 1],
    ['abortRun', 'abortRun', 2],
    ['fetchRunIntervention', 'fetchRunIntervention', 1],
    ['fetchRun', 'fetchRun', 1],
    ['fetchUnattributedRuns', 'fetchUnattributedRuns', 1],
    ['fetchAISystemLiveness', 'fetchAISystemLiveness', 1],
    ['updateAISystemLiveness', 'updateAISystemLiveness', 2],
    ['learnAISystemLiveness', 'learnAISystemLiveness', 1],
    ['acceptAISystemLiveness', 'acceptAISystemLiveness', 1],
    ['fetchAISystemDrift', 'fetchAISystemDrift', 2],
    ['fetchTenantDriftFeed', 'fetchTenantDriftFeed', 1],
    ['fetchMissionControlCards', 'fetchMissionControlCards', 1],
    ['fetchMissionControlExplainers', 'fetchMissionControlExplainers', 0],
    ['fetchObligationsBoard', 'fetchObligationsBoard', 1],
    ['fetchSystemObligations', 'fetchSystemObligations', 2],
    ['fetchAISystemBaselines', 'fetchAISystemBaselines', 1],
    ['acknowledgeDriftEvent', 'acknowledgeDriftEvent', 2],
    ['closeDriftEvent', 'closeDriftEvent', 2],
    ['fetchAISystemBaselineEpochs', 'fetchAISystemBaselineEpochs', 1],
    ['fetchAISystemIncidentWindows', 'fetchAISystemIncidentWindows', 2],
    ['fetchIncidentWindows', 'fetchIncidentWindows', 1],
    ['createIncidentWindow', 'createIncidentWindow', 2],
    ['updateIncidentWindow', 'updateIncidentWindow', 2],
    ['deleteIncidentWindow', 'deleteIncidentWindow', 1],
    ['fetchContractVersion', 'fetchContractVersion', 2],
    ['fetchAISystemComposition', 'fetchAISystemComposition', 2],
    ['createAISystemDependency', 'createAISystemDependency', 2],
    ['updateAISystemDependency', 'updateAISystemDependency', 3],
    ['deleteAISystemDependency', 'deleteAISystemDependency', 2],
    ['listAgentCards', 'listA2AAgentCards', 0],
    ['fetchAISystemContracts', 'fetchAISystemContracts', 1],
    ['fetchCurrentContract', 'fetchCurrentContract', 1],
    ['mintAISystemContract', 'mintAISystemContract', 2],
    ['approveContract', 'approveContract', 1],
    ['rejectContract', 'rejectContract', 2],
    ['promoteContract', 'promoteContract', 1],
    ['fetchSystemHealth', 'fetchSystemHealth', 2],
    ['fetchAssuranceReport', 'fetchAssuranceReport', 2],
    ['captureAssuranceReport', 'captureAssuranceReport', 2],
    ['fetchAssuranceHistory', 'fetchAssuranceHistory', 1],
    ['fetchResponsePolicies', 'fetchResponsePolicies', 2],
    ['createResponsePolicy', 'createResponsePolicy', 2],
    ['previewResponsePolicy', 'previewResponsePolicy', 2],
    ['updateResponsePolicy', 'updateResponsePolicy', 2],
    ['deleteResponsePolicy', 'deleteResponsePolicy', 1],
    ['fetchResponseHistory', 'fetchResponseHistory', 1],
    ['restoreAutonomy', 'restoreAutonomy', 2],
    ['fetchDataScope', 'fetchDataScope', 1],
    ['updateDataScope', 'updateDataScope', 2],
    ['fetchDataScopeActivity', 'fetchDataScopeActivity', 2],
    ['fetchReachRules', 'fetchReachRules', 1],
    ['createReachRule', 'createReachRule', 2],
    ['updateReachRule', 'updateReachRule', 3],
    ['deleteReachRule', 'deleteReachRule', 2],
    ['previewReachRule', 'previewReachRule', 2],
    ['fetchReplayStatus', 'fetchReplayStatus', 2],
    ['buildReplaySuite', 'buildReplaySuite', 2],
    ['requestReplayRun', 'requestReplayRun', 2],
    ['fetchAISystemLifecycle', 'fetchAISystemLifecycle', 1],
    ['transitionAISystemLifecycle', 'transitionAISystemLifecycle', 2],
    ['fetchAISystemEnvelope', 'fetchAISystemEnvelope', 1],
    ['putAISystemEnvelope', 'putAISystemEnvelope', 2],
    ['fetchAISystemOutcomes', 'fetchAISystemOutcomes', 1],
    ['putAISystemOutcomes', 'putAISystemOutcomes', 2],
    ['exportContractDocument', 'exportContractDocument', 2],
    ['exportAssuranceCertificate', 'exportAssuranceCertificate', 2],
    ['fetchAISystemEvidence', 'fetchAISystemEvidence', 1],
    ['createAISystemEvidence', 'createAISystemEvidence', 2],
    ['deleteAISystemEvidence', 'deleteAISystemEvidence', 2],
    ['fetchResourceGroup', 'fetchResourceGroup', 2],
    ['createResourceGroup', 'createResourceGroup', 1],
    ['updateResourceGroup', 'updateResourceGroup', 2],
    ['deleteResourceGroup', 'deleteResourceGroup', 1],
    ['fetchGroupMembers', 'fetchGroupMembers', 2],
    ['addGroupMembers', 'addGroupMembers', 4],
    ['removeGroupMember', 'removeGroupMember', 2],
    ['fetchEffectiveGuardrails', 'fetchEffectiveGuardrails', 1],
    ['fetchGuardrailConfig', 'getGuardrailConfig', 0],
    ['updateGuardrailConfig', 'updateGuardrailConfig', 1],
    ['testGuardrails', 'testGuardrails', 1],
    ['fetchGuardrailProviders', 'listGuardrailProviders', 0],
    ['listGuardrailConfigs', 'listGuardrailConfigs', 0],
    ['createGuardrailConfig', 'createGuardrailConfig', 1],
    ['getGuardrailConfigById', 'getGuardrailConfigById', 1],
    ['updateGuardrailConfigById', 'updateGuardrailConfigById', 2],
    ['deleteGuardrailConfig', 'deleteGuardrailConfig', 1],
    ['setGuardrailGroups', 'setGuardrailGroups', 2],
    ['fetchEffectiveLLMModels', 'fetchEffectiveLLMModels', 1],
    ['addModelToGroup', 'addModelToGroup', 2],
    ['removeModelFromGroup', 'removeModelFromGroup', 2],
    ['fetchEffectiveServerConfigs', 'fetchEffectiveServerConfigs', 1],
    ['addServerToGroup', 'addServerToGroup', 2],
    ['removeServerFromGroup', 'removeServerFromGroup', 2],
    ['getMCPCapabilityFilters', 'getMCPCapabilityFilters', 2],
    ['updateMCPCapabilityFilters', 'updateMCPCapabilityFilters', 3],
    ['clearMCPCapabilityFilters', 'clearMCPCapabilityFilters', 2],
    ['fetchEffectiveAgentSkills', 'fetchEffectiveAgentSkills', 1],
    ['removeSkillFromGroup', 'removeSkillFromGroup', 2],
    ['fetchGroupAPIKeys', 'fetchGroupAPIKeys', 1],
    ['fetchGroupAPIKeyUsage', 'fetchGroupAPIKeyUsage', 1],
    ['createGroupAPIKey', 'createGroupAPIKey', 2],
    ['enableAPIKey', 'enableAPIKey', 1],
    ['disableAPIKey', 'disableAPIKey', 1],
    ['revokeAPIKey', 'revokeAPIKey', 1],
    ['fetchLLMGlobalLimits', 'fetchLLMGlobalLimits', 1],
    ['fetchMCPGlobalLimits', 'fetchMCPGlobalLimits', 1],
    ['updateLLMGlobalLimits', 'updateLLMGlobalLimits', 2],
    ['updateMCPGlobalLimits', 'updateMCPGlobalLimits', 2],
    ['fetchSkillGlobalLimits', 'fetchSkillGlobalLimits', 1],
    ['updateSkillGlobalLimits', 'updateSkillGlobalLimits', 2],
    ['fetchMediaGlobalLimits', 'fetchMediaGlobalLimits', 1],
    ['updateMediaGlobalLimits', 'updateMediaGlobalLimits', 2],
    ['fetchMediaGlobalGovernance', 'fetchMediaGlobalGovernance', 1],
    ['updateMediaGlobalGovernance', 'updateMediaGlobalGovernance', 2],
    ['fetchLLMModelLimits', 'fetchLLMModelLimits', 2],
    ['updateLLMModelLimits', 'updateLLMModelLimits', 3],
    ['clearLLMModelLimits', 'clearLLMModelLimits', 2],
    ['fetchMCPServerLimits', 'fetchMCPServerLimits', 2],
    ['updateMCPServerLimits', 'updateMCPServerLimits', 3],
    ['clearMCPServerLimits', 'clearMCPServerLimits', 2],
    ['fetchSkillItemLimits', 'fetchSkillItemLimits', 2],
    ['updateSkillItemLimits', 'updateSkillItemLimits', 3],
    ['clearSkillItemLimits', 'clearSkillItemLimits', 2],
    ['getLLMModelGovernance', 'getLLMModelGovernance', 2],
    ['updateLLMModelGovernance', 'updateLLMModelGovernance', 3],
    ['clearLLMModelGovernance', 'clearLLMModelGovernance', 2],
    ['getLLMGlobalGovernance', 'getLLMGlobalGovernance', 1],
    ['updateLLMGlobalGovernance', 'updateLLMGlobalGovernance', 2],
    ['clearLLMGlobalGovernance', 'clearLLMGlobalGovernance', 1],
    ['getMCPGlobalGovernance', 'getMCPGlobalGovernance', 1],
    ['updateMCPGlobalGovernance', 'updateMCPGlobalGovernance', 2],
    ['clearMCPGlobalGovernance', 'clearMCPGlobalGovernance', 1],
    ['getSkillGlobalGovernance', 'getSkillGlobalGovernance', 1],
    ['updateSkillGlobalGovernance', 'updateSkillGlobalGovernance', 2],
    ['clearSkillGlobalGovernance', 'clearSkillGlobalGovernance', 1],
    ['getSkillGovernance', 'getSkillGovernance', 2],
    ['updateSkillGovernance', 'updateSkillGovernance', 3],
    ['clearSkillGovernance', 'clearSkillGovernance', 2],
    ['fetchGroupUsage', 'fetchGroupUsage', 2],
    ['fetchAllUsageAlerts', 'fetchAllUsageAlerts', 1],
    ['fetchGroupAlerts', 'fetchGroupAlerts', 2],
    ['acknowledgeUsageAlert', 'acknowledgeUsageAlert', 1],
    // The console reads its own tenant's theme at /current.
    ['fetchCurrentTenantThemeVariants', 'fetchCurrentTenantThemeVariants', 0],
    ['fetchAssuranceInboxCounts', 'getAssuranceInboxCounts', 0],
    ['fetchRequestsTimeSeries', 'fetchRequestsTimeSeries', 1],
    ['fetchLatencyTimeSeries', 'fetchLatencyTimeSeries', 1],
    ['fetchErrorRateTimeSeries', 'fetchErrorRateTimeSeries', 1],
    ['fetchHourlyHeatmap', 'fetchHourlyHeatmap', 1],
    ['fetchTenantAuditLogs', 'fetchTenantAuditLogs', 1],
    ['getServerIconUrl', 'getServerIconUrl', 1],
    ['listSkillResources', 'listSkillResources', 1],
    ['getSkillResource', 'getSkillResource', 2],
    ['createSkillResource', 'createSkillResource', 2],
    ['updateSkillResource', 'updateSkillResource', 3],
    ['deleteSkillResource', 'deleteSkillResource', 2],
    ['listSkillScripts', 'listSkillScripts', 1],
    ['getSkillScript', 'getSkillScript', 2],
    ['createSkillScript', 'createSkillScript', 2],
    ['updateSkillScript', 'updateSkillScript', 3],
    ['deleteSkillScript', 'deleteSkillScript', 2],
    ['listSkillAssets', 'listSkillAssets', 1],
    ['getSkillAsset', 'getSkillAsset', 2],
    ['createSkillAsset', 'createSkillAsset', 2],
    ['updateSkillAsset', 'updateSkillAsset', 3],
    ['deleteSkillAsset', 'deleteSkillAsset', 2],
    ['listArgumentPolicies', 'listArgumentPolicies', 1],
    ['getArgumentPolicy', 'getArgumentPolicy', 1],
    ['createArgumentPolicy', 'createArgumentPolicy', 1],
    ['updateArgumentPolicy', 'updateArgumentPolicy', 2],
    ['deleteArgumentPolicy', 'deleteArgumentPolicy', 1],
    ['setArgumentPolicyGroups', 'setArgumentPolicyGroups', 2],
    ['exportArgumentPoliciesYaml', 'exportArgumentPoliciesYaml', 0],
    ['importArgumentPoliciesYaml', 'importArgumentPoliciesYaml', 1],
];

/** [hook method, client method, argument count, banner code, banner resource, auth failures stay silent] */
export const REPORTING: ReportingRow[] = [
    ['triggerServerHealthCheck', 'checkServerHealth', 1, 'TRIGGER_SERVER_HEALTH_CHECK_ERROR', 'health_check', false],
    ['fetchPerformanceMetrics', 'fetchPerformanceMetrics', 1, 'FETCH_PERFORMANCE_METRICS_ERROR', 'performance_metrics', false],
    ['fetchRealTimeMetrics', 'fetchRealTimeMetrics', 1, 'FETCH_REAL_TIME_METRICS_ERROR', 'real_time_metrics', false],
    ['fetchServerHealthMetrics', 'fetchServerHealthMetrics', 1, 'FETCH_SERVER_HEALTH_METRICS_ERROR', 'server_health_metrics', false],
    ['fetchDashboardMetrics', 'fetchDashboardMetrics', 0, 'FETCH_DASHBOARD_METRICS_ERROR', 'dashboard_metrics', false],
    ['fetchSystemAlerts', 'fetchSystemAlerts', 1, 'FETCH_SYSTEM_ALERTS_ERROR', 'system_alerts', false],
    ['acknowledgeSystemAlert', 'acknowledgeSystemAlert', 1, 'ACKNOWLEDGE_SYSTEM_ALERT_ERROR', 'system_alerts', false],
    ['resolveSystemAlert', 'resolveSystemAlert', 2, 'RESOLVE_SYSTEM_ALERT_ERROR', 'system_alerts', false],
    ['fetchOAuthTokens', 'fetchOAuthTokens', 1, 'FETCH_OAUTH_TOKENS_ERROR', 'oauth_tokens', false],
    ['revokeOAuthToken', 'revokeOAuthToken', 1, 'REVOKE_OAUTH_TOKEN_ERROR', 'oauth_tokens', false],
    ['revokeOAuthTokensBulk', 'revokeOAuthTokensBulk', 1, 'REVOKE_OAUTH_TOKENS_BULK_ERROR', 'oauth_tokens', false],
    ['exportMetricsData', 'exportMetricsData', 1, 'EXPORT_METRICS_DATA_ERROR', 'metrics_export', false],
    ['fetchProxyLogs', 'fetchProxyLogs', 1, 'FETCH_PROXY_LOGS_ERROR', 'proxy_logs', false],
    ['fetchProxyStats', 'fetchProxyStats', 1, 'FETCH_PROXY_STATS_ERROR', 'proxy_stats', false],
    ['exportProxyLogs', 'exportProxyLogs', 2, 'EXPORT_PROXY_LOGS_ERROR', 'proxy_logs', false],
    ['exportAuditLogs', 'exportAuditLogs', 2, 'EXPORT_AUDIT_LOGS_ERROR', 'audit_logs', false],
    ['createServer', 'createServer', 1, 'CREATE_SERVER_ERROR', 'server', false],
    ['updateServer', 'updateServer', 2, 'UPDATE_SERVER_ERROR', 'server', false],
    ['refreshServerCapabilities', 'refreshServerCapabilities', 1, 'REFRESH_CAPABILITIES_ERROR', 'server', false],
    ['deleteServer', 'deleteServer', 1, 'DELETE_SERVER_ERROR', 'server', false],
    ['uploadServerIcon', 'uploadServerIcon', 2, 'UPLOAD_ICON_ERROR', 'server_icon', false],
    ['deleteServerIcon', 'deleteServerIcon', 1, 'DELETE_ICON_ERROR', 'server_icon', false],
    ['createVirtualServer', 'createVirtualServer', 1, 'CREATE_VIRTUAL_SERVER_ERROR', 'virtual_server', false],
    ['updateVirtualServer', 'updateVirtualServer', 2, 'UPDATE_VIRTUAL_SERVER_ERROR', 'virtual_server', false],
    ['deleteVirtualServer', 'deleteVirtualServer', 1, 'DELETE_VIRTUAL_SERVER_ERROR', 'virtual_server', false],
    ['testServerConnection', 'testServerConnection', 1, 'TEST_SERVER_CONNECTION_ERROR', 'server_connection', false],
    ['createUser', 'createTenantAdminUser', 1, 'CREATE_USER_ERROR', 'user', false],
    ['updateUser', 'updateTenantAdminUser', 2, 'UPDATE_USER_ERROR', 'user', false],
    ['deleteUser', 'deleteTenantAdminUser', 1, 'DELETE_USER_ERROR', 'user', false],
    ['fetchRoles', 'fetchRoles', 0, 'FETCH_ROLES_ERROR', 'roles', false],
    ['fetchIdPConfigs', 'fetchIdPConfigs', 1, 'FETCH_IDP_CONFIGS_ERROR', 'idp_configs', false],
    ['fetchIdPConfig', 'fetchIdPConfig', 1, 'FETCH_IDP_CONFIG_ERROR', 'idp_config', false],
    ['createIdPConfig', 'createIdPConfig', 1, 'CREATE_IDP_CONFIG_ERROR', 'idp_config', false],
    ['updateIdPConfig', 'updateIdPConfig', 2, 'UPDATE_IDP_CONFIG_ERROR', 'idp_config', false],
    ['deleteIdPConfig', 'deleteIdPConfig', 1, 'DELETE_IDP_CONFIG_ERROR', 'idp_config', false],
    ['discoverIdPEndpoints', 'discoverIdPEndpoints', 2, 'DISCOVER_IDP_ENDPOINTS_ERROR', 'idp_discovery', false],
    ['testExistingIdP', 'testExistingIdP', 1, 'TEST_EXISTING_IDP_ERROR', 'idp_test', false],
    ['fetchAuditExportDestinations', 'fetchAuditExportDestinations', 0, 'FETCH_AUDIT_EXPORT_DESTINATIONS_ERROR', 'audit_export_destinations', false],
    ['fetchAuditExportDestination', 'fetchAuditExportDestination', 1, 'FETCH_AUDIT_EXPORT_DESTINATION_ERROR', null, false],
    ['createAuditExportDestination', 'createAuditExportDestination', 1, 'CREATE_AUDIT_EXPORT_DESTINATION_ERROR', null, false],
    ['updateAuditExportDestination', 'updateAuditExportDestination', 2, 'UPDATE_AUDIT_EXPORT_DESTINATION_ERROR', null, false],
    ['deleteAuditExportDestination', 'deleteAuditExportDestination', 1, 'DELETE_AUDIT_EXPORT_DESTINATION_ERROR', null, false],
    ['runAuditExportDestinationNow', 'runAuditExportDestinationNow', 1, 'RUN_AUDIT_EXPORT_DESTINATION_ERROR', null, false],
    ['fetchEndUsers', 'fetchEndUsers', 1, 'FETCH_END_USERS_ERROR', 'end_users', false],
    ['fetchEndUserDetails', 'fetchEndUserDetails', 1, 'FETCH_END_USER_DETAILS_ERROR', 'end_user', false],
    ['updateEndUserStatus', 'updateEndUserStatus', 2, 'UPDATE_END_USER_STATUS_ERROR', 'end_user', false],
    ['deleteEndUser', 'deleteEndUser', 1, 'DELETE_END_USER_ERROR', 'end_user', false],
    ['exportEndUsers', 'exportEndUsers', 1, 'EXPORT_END_USERS_ERROR', 'end_users', false],
    ['fetchOAuthAppClients', 'fetchOAuthAppClients', 1, 'FETCH_OAUTH_APP_CLIENTS_ERROR', 'oauth_app_clients', false],
    ['createOAuthAppClient', 'createOAuthAppClient', 1, 'CREATE_OAUTH_APP_CLIENT_ERROR', 'oauth_app_client', false],
    ['updateOAuthAppClient', 'updateOAuthAppClient', 2, 'UPDATE_OAUTH_APP_CLIENT_ERROR', 'oauth_app_client', false],
    ['deleteOAuthAppClient', 'deleteOAuthAppClient', 1, 'DELETE_OAUTH_APP_CLIENT_ERROR', 'oauth_app_client', false],
    ['getOAuthAppClientUsage', 'getOAuthAppClientUsage', 1, 'GET_OAUTH_APP_CLIENT_USAGE_ERROR', 'oauth_app_client', false],
    ['fetchServerConfigs', 'fetchServerConfigs', 1, 'FETCH_SERVER_CONFIGS_ERROR', 'server_configs', false],
    ['createServerConfig', 'createServerConfig', 1, 'CREATE_SERVER_CONFIG_ERROR', 'server_config', false],
    ['updateServerConfig', 'updateServerConfig', 2, 'UPDATE_SERVER_CONFIG_ERROR', 'server_config', false],
    ['deleteServerConfig', 'deleteServerConfig', 1, 'DELETE_SERVER_CONFIG_ERROR', 'server_config', false],
];

/** These resolve to nothing, whatever the endpoint replied. */
export const RESOLVES_EMPTY = new Set([
    'deleteServer',
    'deleteServerIcon',
    'deleteVirtualServer',
]);

export interface GatewaySurface {
    /** The hook's name, as the test titles say it. */
    name: string;
    /** Render the hook under test. */
    render: () => { result: { current: { errors: Array<{ code?: string; message?: string; context?: { resource?: string } }> } } };
    /** The mock standing in for a client method. */
    clientFn: (name: string) => Mock;
    PASS_THROUGH: PassThroughRow[];
    REPORTING: ReportingRow[];
    RESOLVES_EMPTY: Set<string>;
}

/** The tests every row of the two tables gets. */
export function describeGatewaySurface({ name, render: renderGateway, clientFn, PASS_THROUGH, REPORTING, RESOLVES_EMPTY }: GatewaySurface): void {
    describe(`${name} methods reach the right endpoint`, () => {
        it.each(PASS_THROUGH)('%s calls %s with its arguments and returns the result', async (hook, client, count) => {
            const reply = { reply: hook };
            clientFn(client).mockResolvedValue(reply);
            const { result } = renderGateway();

            const got = await hookFn(result.current, hook)(...args(count));

            expect(clientFn(client)).toHaveBeenCalledWith(...args(count));
            expect(got).toBe(reply);
        });

        it.each(REPORTING)('%s calls %s with its arguments and returns the result', async (hook, client, count) => {
            const reply = { reply: hook };
            clientFn(client).mockResolvedValue(reply);
            const { result } = renderGateway();

            let got: unknown;
            await act(async () => {
                got = await hookFn(result.current, hook)(...args(count));
            });

            expect(clientFn(client)).toHaveBeenCalledWith(...args(count));
            expect(got).toBe(RESOLVES_EMPTY.has(hook) ? undefined : reply);
        });
    });

    describe(`${name} methods report failures`, () => {
        it.each(REPORTING)('%s (%s) rethrows and raises its banner', async (hook, client, count, code, resource) => {
            const failure = httpError(500, `${hook} exploded`);
            clientFn(client).mockRejectedValue(failure);
            const { result } = renderGateway();

            await act(async () => {
                await expect(hookFn(result.current, hook)(...args(count))).rejects.toBe(failure);
            });

            await waitFor(() => expect(result.current.errors.map((e) => e.code)).toContain(code));
            const banner = result.current.errors.find((e) => e.code === code);
            expect(banner?.message).toBe(`${hook} exploded`);
            expect(banner?.context?.resource).toBe(resource ?? undefined);
        });

        it.each(REPORTING.filter((row) => row[5]))(
            '%s (%s) leaves an auth failure to the auth layer',
            async (hook, client, count, code) => {
                const failure = httpError(403, 'forbidden');
                clientFn(client).mockRejectedValue(failure);
                const { result } = renderGateway();

                await act(async () => {
                    await expect(hookFn(result.current, hook)(...args(count))).rejects.toBe(failure);
                });

                expect(result.current.errors.map((e) => e.code)).not.toContain(code);
            },
        );
    });
}
