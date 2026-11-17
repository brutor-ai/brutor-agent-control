/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The modules of the Admin Console's API client.
 *
 * Every method the client offers is declared by one module: a class that
 * extends `GatewayApiBase` and nothing else. `installApiModules` copies
 * the methods of each module onto the client's prototype, so one instance
 * answers all of them. `feature` names the Enterprise Edition feature a
 * module belongs to, or null for the Community Edition.
 *
 * The built-in modules are the Community Edition's. A
 * module of an Enterprise feature is contributed by an extension
 * (`enterprise/src/apiModules.ts`): `composeAdmin` installs it on the client
 * class, and the extension adds its methods to the client's type.
 */

import type { EnterpriseFeature } from '../../app/editions';
import { GatewayApiBase } from './base';
import { AuthApi } from './auth';
import { TenantApi } from './tenant';
import { GuardrailsApi } from './guardrails';
import { ArgumentPoliciesApi } from './argumentPolicies';
import { AiAssetsApi } from './aiAssets';
import { EvidenceApi } from './evidence';
import { ComplianceApi } from './compliance';
import { AgentSkillsApi } from './agentSkills';
import { McpServersApi } from './mcpServers';
import { HealthApi } from './health';
import { AdminUsersApi } from './adminUsers';
import { IdpApi } from './idp';
import { AuditExportApi } from './auditExport';
import { EndUsersApi } from './endUsers';
import { ResourceGroupsApi } from './resourceGroups';
import { LifecycleApi } from './lifecycle';
import { GroupResourcesApi } from './groupResources';
import { LimitsApi } from './limits';
import { AlertsApi } from './alerts';
import { OAuthApi } from './oauth';
import { MetricsApi } from './metrics';
import { ProxyLogsApi } from './proxyLogs';
import { LlmProvidersApi } from './llmProviders';
import { LlmModelsApi } from './llmModels';
import { RoutingApi } from './routing';
import { VideoJobsApi } from './videoJobs';
import { A2aApi } from './a2a';
import { SystemOpsApi } from './systemOps';
import { ToolApprovalsApi } from './toolApprovals';

/** A class whose prototype methods are installed on the client. */
export type ApiMixin = abstract new (...args: never[]) => GatewayApiBase;

export interface ApiModuleSpec {
    /** Unique within the registry; the name of the module file. */
    readonly name: string;
    /** The Enterprise Edition feature the module belongs to, or null for Community. */
    readonly feature: EnterpriseFeature | null;
    readonly mixin: ApiMixin;
}

/** The modules of the Community Edition, in the order their methods are installed. */
export const BUILTIN_API_MODULES = [
    { name: 'auth', feature: null, mixin: AuthApi },
    { name: 'tenant', feature: null, mixin: TenantApi },
    { name: 'guardrails', feature: null, mixin: GuardrailsApi },
    { name: 'argumentPolicies', feature: null, mixin: ArgumentPoliciesApi },
    { name: 'aiAssets', feature: null, mixin: AiAssetsApi },
    { name: 'evidence', feature: null, mixin: EvidenceApi },
    { name: 'compliance', feature: null, mixin: ComplianceApi },
    { name: 'agentSkills', feature: null, mixin: AgentSkillsApi },
    { name: 'mcpServers', feature: null, mixin: McpServersApi },
    { name: 'health', feature: null, mixin: HealthApi },
    { name: 'adminUsers', feature: null, mixin: AdminUsersApi },
    { name: 'idp', feature: null, mixin: IdpApi },
    { name: 'auditExport', feature: null, mixin: AuditExportApi },
    { name: 'endUsers', feature: null, mixin: EndUsersApi },
    { name: 'resourceGroups', feature: null, mixin: ResourceGroupsApi },
    { name: 'lifecycle', feature: null, mixin: LifecycleApi },
    { name: 'groupResources', feature: null, mixin: GroupResourcesApi },
    { name: 'limits', feature: null, mixin: LimitsApi },
    { name: 'alerts', feature: null, mixin: AlertsApi },
    { name: 'oauth', feature: null, mixin: OAuthApi },
    { name: 'metrics', feature: null, mixin: MetricsApi },
    { name: 'proxyLogs', feature: null, mixin: ProxyLogsApi },
    { name: 'llmProviders', feature: null, mixin: LlmProvidersApi },
    { name: 'llmModels', feature: null, mixin: LlmModelsApi },
    { name: 'routing', feature: null, mixin: RoutingApi },
    { name: 'videoJobs', feature: null, mixin: VideoJobsApi },
    { name: 'a2a', feature: null, mixin: A2aApi },
    { name: 'systemOps', feature: null, mixin: SystemOpsApi },
    { name: 'toolApprovals', feature: null, mixin: ToolApprovalsApi },
] as const satisfies readonly ApiModuleSpec[];

type UnionToIntersection<U> = (U extends unknown ? (x: U) => void : never) extends (x: infer I) => void ? I : never;

/** The instance type that a list of modules gives the client. */
export type ApiModuleMethods<M extends readonly ApiModuleSpec[]> = UnionToIntersection<InstanceType<M[number]['mixin']>>;

const installed = new WeakMap<object, Map<string, ApiMixin>>();

/**
 * Install the methods of `modules` on `target`'s prototype. Refuses, with
 * nothing installed, a module name already registered on the target with a
 * different mixin and a method name the target already answers: a silently
 * replaced method would be an API nobody chose. A module already installed
 * as it is (same name, same mixin) is skipped: an extension composed into a
 * second `App` installs its modules once.
 */
export function installApiModules(target: abstract new (...args: never[]) => GatewayApiBase, modules: readonly ApiModuleSpec[]): void {
    const names = installed.get(target) ?? new Map<string, ApiMixin>();
    const proto = target.prototype as object;
    const planned: Array<[string, PropertyDescriptor]> = [];
    const plannedNames = new Set<string>();
    const adding: ApiModuleSpec[] = [];
    for (const mod of modules) {
        if (!mod.name || modules.filter((m) => m.name === mod.name).length > 1) {
            throw new Error(`API module registered twice: ${mod.name || '(empty name)'}`);
        }
        if (names.has(mod.name)) {
            if (names.get(mod.name) === mod.mixin) continue;
            throw new Error(`API module registered twice: ${mod.name}`);
        }
        adding.push(mod);
        for (const key of Object.getOwnPropertyNames(mod.mixin.prototype as object)) {
            if (key === 'constructor') continue;
            if (key in proto || plannedNames.has(key)) {
                throw new Error(`API module ${mod.name} redefines ${key}`);
            }
            const descriptor = Object.getOwnPropertyDescriptor(mod.mixin.prototype as object, key);
            if (descriptor) { planned.push([key, descriptor]); plannedNames.add(key); }
        }
    }
    for (const [key, descriptor] of planned) Object.defineProperty(proto, key, descriptor);
    for (const mod of adding) names.set(mod.name, mod.mixin);
    installed.set(target, names);
}

/** The names of the modules installed on `target`, in installation order. */
export function installedApiModules(target: abstract new (...args: never[]) => GatewayApiBase): readonly string[] {
    return [...(installed.get(target)?.keys() ?? [])];
}
