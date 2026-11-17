/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The domain hooks of `useGateway`, with the Enterprise
 * Edition feature each belongs to (null: Community). The facade calls every
 * hook here, statically and in this order — React hooks cannot be called
 * from a list — so this is the record the tests
 * hold the facade to, not the composition itself.
 *
 * These are the hooks of the Community Edition: every one has no feature.
 * The domain hooks of the Enterprise features are listed, the same way, for
 * the Enterprise facade (`enterprise/src/hooks/modules.ts`).
 */

import type { EnterpriseFeature } from '../../app/editions';
import { useResourceGroupsGateway } from './resourceGroups';
import { useGuardrailsGateway } from './guardrails';
import { useArgumentPoliciesGateway } from './argumentPolicies';
import { useGroupResourcesGateway } from './groupResources';
import { useLimitsGateway } from './limits';
import { useMcpServersGateway } from './mcpServers';
import { useHealthGateway } from './health';
import { useAdminUsersGateway } from './adminUsers';
import { useTenantGateway } from './tenant';
import { useMetricsGateway } from './metrics';
import { useAlertsGateway } from './alerts';
import { useOAuthGateway } from './oauth';
import { useProxyLogsGateway } from './proxyLogs';
import { useIdpGateway } from './idp';
import { useAuditExportGateway } from './auditExport';
import { useEndUsersGateway } from './endUsers';
import { useAgentSkillsGateway } from './agentSkills';

export interface GatewayHookModuleSpec {
    readonly name: string;
    readonly feature: EnterpriseFeature | null;
    readonly hook: (...args: never[]) => object;
}

export const GATEWAY_HOOK_MODULES = [
    { name: 'resourceGroups', feature: null, hook: useResourceGroupsGateway },
    { name: 'guardrails', feature: null, hook: useGuardrailsGateway },
    { name: 'argumentPolicies', feature: null, hook: useArgumentPoliciesGateway },
    { name: 'groupResources', feature: null, hook: useGroupResourcesGateway },
    { name: 'limits', feature: null, hook: useLimitsGateway },
    { name: 'mcpServers', feature: null, hook: useMcpServersGateway },
    { name: 'health', feature: null, hook: useHealthGateway },
    { name: 'adminUsers', feature: null, hook: useAdminUsersGateway },
    { name: 'tenant', feature: null, hook: useTenantGateway },
    { name: 'metrics', feature: null, hook: useMetricsGateway },
    { name: 'alerts', feature: null, hook: useAlertsGateway },
    { name: 'oauth', feature: null, hook: useOAuthGateway },
    { name: 'proxyLogs', feature: null, hook: useProxyLogsGateway },
    { name: 'idp', feature: null, hook: useIdpGateway },
    { name: 'auditExport', feature: null, hook: useAuditExportGateway },
    { name: 'endUsers', feature: null, hook: useEndUsersGateway },
    { name: 'agentSkills', feature: null, hook: useAgentSkillsGateway },
] as const satisfies readonly GatewayHookModuleSpec[];
