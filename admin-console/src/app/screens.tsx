/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The screens of the Admin Console: which component answers which tab id.
 *
 * The console has no URL router. The active screen is a tab id held in state
 * (`mission-control`, `groups:<uuid>:usage`, ...). This list is the one place
 * that maps a tab id to what is rendered; `App.tsx` resolves the active tab
 * against it and renders the result.
 *
 * It is a list, not a `switch`, so that another distribution can add screens
 * without editing this file, and so the mapping can be tested
 * without mounting the whole application.
 *
 * Two ways a screen claims a tab id:
 *   - `ids`: the tab id equals one of them;
 *   - `prefixes`: the tab id starts with one of them. These are the deep
 *     links: the rest of the id carries what to open (`ai-systems:<id>:drift`).
 * A prefix wins over an exact id. No two screens claim the same tab id.
 *
 * A navigation entry that has children is not a tab id: the sidebar expands
 * it, or opens its first child. No screen claims the id of one.
 *
 * These are the screens of the Community Edition. `edition` names the
 * Enterprise Edition feature a screen belongs to; a screen that has one is
 * not listed here but contributed by an extension
 * (`enterprise/src/screens.tsx`), and `composeAdmin` puts it after these.
 */

import React from 'react';

import MissionControl from '../components/monitoring/MissionControl';
import MissionControlDashboard from '../components/monitoring/MissionControlDashboard';
import AssuranceInboxPage from '../components/monitoring/AssuranceInboxPage';
import ApprovalsPage from '../components/approvals/ApprovalsPage';
import ServerConfigTab from '../components/mcp/ServerConfigTab';
import GuardrailsTab from '../components/guardrails/GuardrailsTab';
import ArgumentPoliciesTab from '../components/argument-policies/ArgumentPoliciesTab';
import IncidentWindowsTab from '../components/governance/IncidentWindowsTab';
import AgentIdentitiesResourceTab from '../components/agent/AgentIdentitiesResourceTab';
import AgentPoliciesTab from '../components/governance/AgentPoliciesTab';
import SemanticPoliciesTab from '../components/governance/SemanticPoliciesTab';
import ComplianceConsoleTab from '../components/compliance/ComplianceConsoleTab';
import AIAssetsTab from '../components/ai-assets/AIAssetsTab';
import AISystemsFleetTab from '../components/ai-systems/AISystemsFleetTab';
import LLMCatalogAdminTab from '../components/llm/catalog/LLMCatalogAdminTab';
import MCPCatalogAdminTab from '../components/mcp/catalog/MCPCatalogAdminTab';
import AgentSkillsTab from '../components/skills/AgentSkillsTab';
import { SkillSourcesTab } from '../components/skills/SkillSourcesTab';
import { FeatureDisabled } from '../components/ui/FeatureDisabled';
import IdPServersTab from '../components/idp/IdPServersTab';
import AuditLogViewer from '../components/monitoring/AuditLogViewer';
import ProxyLogsViewer from '../components/monitoring/ProxyLogsViewer';
import AuditExportTab from '../components/audit-export/AuditExportTab';
import ConfigurationTab from '../components/ConfigurationTab';
import SystemConsoleTab from '../components/system/SystemConsoleTab';
import ResourceGroupsTab from '../components/groups/ResourceGroupsTab';
import UserManagementTab from '../components/users/UserManagementTab';
import LLModelTab from '../components/llm/config/LLModelTab';
import LLMRoutingTab from '../components/llm/router/LLMRoutingTab.tsx';
import ProvidersTab from '../components/llm/providers/ProvidersTab';
import VideoGenJobsTab from '../components/video/VideoGenJobsTab';
import AgentCardsTab from '../components/a2a/AgentCardsTab';
// A2A outbound policy is edited per group on each Resource Group's A2A
// governance tab; there is no top-level screen for it. Root-group values act
// as the tenant-wide ceiling via the restrictive-merge inheritance.
import A2ADelegationGraphsTab from '../components/a2a/A2ADelegationGraphsTab';
import A2ATasksTab from '../components/a2a/A2ATasksTab';
import A2ATenantKeyTab from '../components/a2a/A2ATenantKeyTab';
import type { useAuth } from '../hooks/useAuth';
import type { EnterpriseFeature } from './editions';

/** What a screen is rendered from. */
export interface ScreenContext {
    /** The full active tab id, including any `:` deep-link suffix. */
    activeTab: string;
    setActiveTab: (tab: string) => void;
    /** The signed-in administrator. Screens are only rendered once there is one. */
    user: NonNullable<ReturnType<typeof useAuth>['user']>;
    /** Tenant feature flag check (`TenantFeaturesContext`). */
    isFeatureEnabled: (key: string) => boolean;
    refreshGroupTree: () => void;
}

export interface ScreenRoute {
    /** Unique and stable. */
    name: string;
    /** Tab ids this screen answers. */
    ids?: readonly string[];
    /** Deep-link prefixes this screen answers. */
    prefixes?: readonly string[];
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    render: (ctx: ScreenContext) => React.ReactNode;
}

/**
 * A screen behind a tenant feature flag: the screen when the flag is on, the
 * "feature disabled" page when it is off. (A flag that is off shows the page
 * as disabled; it does not hide the nav item.)
 */
export function gated(
    flag: string,
    featureName: string,
    render: (ctx: ScreenContext) => React.ReactNode,
): (ctx: ScreenContext) => React.ReactNode {
    return (ctx) => (ctx.isFeatureEnabled(flag) ? render(ctx) : <FeatureDisabled flag={flag} featureName={featureName} />);
}

const A2A_NAME = 'Agent-to-Agent (A2A)';

export const SCREENS: readonly ScreenRoute[] = [
    {
        name: 'inbox',
        ids: ['inbox'],
        // `inbox:reviews:<check id>` opens the Inbox with that
        // judged check's blind review queue expanded.
        prefixes: ['inbox:reviews:'],
        render: ({ activeTab, setActiveTab }) =>
            activeTab.startsWith('inbox:reviews:') ? (
                <AssuranceInboxPage
                    key={activeTab}
                    setActiveTab={setActiveTab}
                    reviewCheckId={activeTab.slice('inbox:reviews:'.length)}
                />
            ) : (
                <AssuranceInboxPage setActiveTab={setActiveTab} />
            ),
    },
    {
        name: 'approvals',
        ids: ['approvals'],
        render: () => <ApprovalsPage />,
    },
    {
        // The RG-card dashboard is the landing page.
        name: 'mission-control',
        ids: ['mission-control'],
        render: ({ setActiveTab }) => <MissionControlDashboard setActiveTab={setActiveTab} />,
    },
    {
        // The tabbed metrics view. `mission-control-metrics:<groupId>` opens
        // it pre-scoped to one Resource Group (the card dashboard's per-card
        // "Metrics" action).
        name: 'mission-control-metrics',
        ids: ['mission-control-metrics'],
        prefixes: ['mission-control-metrics:'],
        render: ({ activeTab, setActiveTab }) => (
            <MissionControl
                setActiveTab={setActiveTab}
                initialGroupId={
                    activeTab.startsWith('mission-control-metrics:')
                        ? activeTab.slice('mission-control-metrics:'.length)
                        : undefined
                }
            />
        ),
    },
    {
        name: 'llm-providers',
        ids: ['llm-providers'],
        render: gated('models', 'Providers & Models', () => <ProvidersTab />),
    },
    {
        // `llm-models:<id>` is set by Mission Control's System-Alerts row
        // when the operator clicks an alert that references a model: open
        // the LLM Models tab and tell it which model to scroll to and select.
        name: 'llm-models',
        ids: ['llm-models'],
        prefixes: ['llm-models:'],
        render: gated('models', 'Providers & Models', ({ activeTab }) => (
            <LLModelTab
                initialFocusModelId={
                    activeTab.startsWith('llm-models:') ? activeTab.slice('llm-models:'.length) : null
                }
            />
        )),
    },
    {
        name: 'llm-routing',
        ids: ['llm-routing'],
        render: gated('model_routing', 'Model Routing', () => <LLMRoutingTab />),
    },
    {
        name: 'mcp-config',
        ids: ['mcp-config'],
        render: gated('mcp_servers', 'MCP Servers', () => <ServerConfigTab />),
    },
    { name: 'idp', ids: ['idp'], render: () => <IdPServersTab /> },
    {
        name: 'agent-skills',
        ids: ['agent-skills'],
        render: gated('agent_skills', 'Skills', () => <AgentSkillsTab />),
    },
    { name: 'agent-skill-sources', ids: ['agent-skill-sources'], render: () => <SkillSourcesTab /> },
    {
        name: 'guardrails',
        ids: ['guardrails'],
        render: gated('guardrails', 'Guardrails', () => <GuardrailsTab />),
    },
    {
        name: 'argument-policies',
        ids: ['argument-policies'],
        // A governance control: behind no tenant flag.
        render: () => <ArgumentPoliciesTab />,
    },
    {
        // Tenant-wide incident windows (Governance).
        name: 'incident-windows',
        ids: ['incident-windows'],
        render: () => <IncidentWindowsTab />,
    },
    {
        // The Compliance console. Every `compliance-*` id goes
        // to one shell that dispatches on the active tab (same pattern as the
        // System console below).
        name: 'compliance',
        ids: [
            'compliance-overview',
            'compliance-frameworks',
            'compliance-profiles',
            'compliance-obligations',
            'compliance-evidence',
            'compliance-oversight',
            'compliance-transparency',
            'compliance-reports',
            'compliance-incidents',
        ],
        render: gated('compliance', 'Compliance', ({ activeTab, setActiveTab }) => (
            <ComplianceConsoleTab activeTab={activeTab} onTabChange={setActiveTab} setActiveTab={setActiveTab} />
        )),
    },
    {
        // The AI System fleet board. Clicking a system
        // opens the fleet drilldown (Signals, Runs, Drift...). The RG detail
        // behind a system stays reachable via `groups:<id>`.
        //
        // Deep link: `ai-systems:{groupId}` or `ai-systems:{groupId}:{section}`
        // opens the drilldown on that system (Mission Control's findings
        // badge, for example, links to drift).
        // `ai-systems:{groupId}:composition:{runId}` applies a run to the
        // Composition tab's Simple graph (the run overlay). The run is NOT
        // part of the key: choosing another run re-routes without remounting
        // the page.
        name: 'ai-systems',
        ids: ['ai-systems'],
        prefixes: ['ai-systems:'],
        render: ({ activeTab, setActiveTab }) => {
            if (!activeTab.startsWith('ai-systems:')) {
                return <AISystemsFleetTab setActiveTab={setActiveTab} />;
            }
            const [, systemId, section, runId] = activeTab.split(':');
            return (
                <AISystemsFleetTab
                    key={`ai-systems:${systemId}:${section ?? ''}`}
                    initialDrilldownId={systemId}
                    initialSection={(section as never) || undefined}
                    routeRunId={runId || null}
                    setActiveTab={setActiveTab}
                />
            );
        },
    },
    { name: 'ai-assets', ids: ['ai-assets'], render: () => <AIAssetsTab /> },
    {
        // Agent Identities (definition) live under Resources > Agents.
        // `a2a-identities:<agent id>:implementation` opens the
        // identity on its Implementation section (the inbox's agent findings).
        name: 'a2a-identities',
        ids: ['a2a-identities'],
        prefixes: ['a2a-identities:'],
        render: ({ activeTab }) => {
            if (!activeTab.startsWith('a2a-identities:')) {
                return <AgentIdentitiesResourceTab />;
            }
            const [, agentId, section] = activeTab.split(':');
            return <AgentIdentitiesResourceTab key={activeTab} initialAgentId={agentId} initialSection={section} />;
        },
    },
    {
        // Agent Policies (grants) live under Governance.
        name: 'agent-policies',
        ids: ['agent-policies'],
        // Agent policies are grants to agent identities: they follow the
        // agents flag (`a2a`), as the control plane's endpoint does — not
        // the Policy-as-Code flag.
        render: gated('a2a', A2A_NAME, () => <AgentPoliciesTab />),
    },
    {
        name: 'semantic-policies',
        ids: ['semantic-policies'],
        // A governance control: behind no tenant flag.
        render: () => <SemanticPoliciesTab />,
    },
    {
        name: 'a2a-agent-cards',
        ids: ['a2a-agent-cards'],
        render: gated('a2a', A2A_NAME, () => <AgentCardsTab />),
    },
    {
        name: 'a2a-delegation-graphs',
        ids: ['a2a-delegation-graphs'],
        render: gated('a2a', A2A_NAME, ({ setActiveTab }) => <A2ADelegationGraphsTab setActiveTab={setActiveTab} />),
    },
    { name: 'a2a-tasks', ids: ['a2a-tasks'], render: gated('a2a', A2A_NAME, () => <A2ATasksTab />) },
    {
        name: 'a2a-tenant-key',
        ids: ['a2a-tenant-key'],
        render: gated('a2a', A2A_NAME, () => <A2ATenantKeyTab />),
    },
    {
        // Resource Groups: `groups` (landing), `groups:<uuid>` (detail),
        // `groups:<uuid>:usage` (detail opened on a tab; set by Mission
        // Control's "Open usage details"), `groups-create`.
        // A 4th segment addresses a sub-section inside the detail tab:
        // `groups:<uuid>:general:ownership` is what the contract lifecycle
        // gate links to when it reports a missing owner or intended use.
        name: 'groups',
        ids: ['groups', 'groups-create'],
        prefixes: ['groups:'],
        render: ({ activeTab, setActiveTab, refreshGroupTree }) => {
            const [, selectedGroupId, detailTab, generalSection] = activeTab.startsWith('groups:')
                ? activeTab.split(':')
                : [];
            return (
                <ResourceGroupsTab
                    selectedGroupId={selectedGroupId}
                    initialDetailTab={detailTab}
                    initialGeneralSection={generalSection as never}
                    showCreateModal={activeTab === 'groups-create'}
                    onGroupTreeChanged={refreshGroupTree}
                    onNavigateToGroup={(id) => setActiveTab(id ? `groups:${id}` : 'groups')}
                />
            );
        },
    },
    { name: 'users', ids: ['users'], render: () => <UserManagementTab /> },
    { name: 'video-gen-jobs', ids: ['video-gen-jobs'], render: () => <VideoGenJobsTab /> },
    {
        name: 'monitoring-audit-logs',
        ids: ['monitoring-audit-logs'],
        render: () => (
            <div className="p-6 h-full">
                <AuditLogViewer />
            </div>
        ),
    },
    {
        name: 'monitoring-proxy-logs',
        ids: ['monitoring-proxy-logs'],
        render: () => (
            <div className="p-6 h-full">
                <ProxyLogsViewer />
            </div>
        ),
    },
    { name: 'monitoring-audit-export', ids: ['monitoring-audit-export'], render: () => <AuditExportTab /> },
    { name: 'llm-catalog-admin', ids: ['llm-catalog-admin'], render: () => <LLMCatalogAdminTab /> },
    { name: 'mcp-catalog-admin', ids: ['mcp-catalog-admin'], render: () => <MCPCatalogAdminTab /> },
    {
        // Settings. `config:<tab>:<tenant sub-tab>` opens it on
        // a sub-tab (the inbox's evidence_unwitnessed finding links to
        // `config:tenant:evidence`).
        name: 'config',
        ids: ['config'],
        prefixes: ['config:'],
        render: ({ activeTab, user }) => {
            if (!activeTab.startsWith('config:')) {
                return <ConfigurationTab currentUser={user} />;
            }
            const [, configTab, tenantSubTab] = activeTab.split(':');
            return (
                <ConfigurationTab
                    key={activeTab}
                    currentUser={user}
                    initialTab={configTab}
                    initialTenantSubTab={tenantSubTab}
                />
            );
        },
    },
    {
        // System Operations Console: every sub-section id goes to
        // the same component, which dispatches on the active tab.
        name: 'system',
        ids: [
            'system-overview',
            'system-counters',
            'system-caches',
            'system-chains',
            'system-audit',
            'system-connections',
            'system-tasks',
            'system-sessions',
            'system-logs',
            'system-maintenance',
        ],
        render: ({ activeTab, setActiveTab, user }) => (
            <SystemConsoleTab activeTab={activeTab} onTabChange={setActiveTab} currentUser={user} />
        ),
    },
];

/** The screen shown for a tab id no screen claims. */
export const FALLBACK_SCREEN: ScreenRoute = {
    name: 'fallback',
    render: ({ setActiveTab }) => <MissionControlDashboard setActiveTab={setActiveTab} />,
};

/**
 * The screen that answers a tab id: the first screen with a matching deep-link
 * prefix, otherwise the first screen that lists the id, otherwise `undefined`.
 */
export function resolveScreen(screens: readonly ScreenRoute[], tab: string): ScreenRoute | undefined {
    return (
        screens.find((screen) => screen.prefixes?.some((prefix) => tab.startsWith(prefix))) ??
        screens.find((screen) => screen.ids?.includes(tab))
    );
}

/** Render the screen for the active tab, or the fallback when none claims it. */
export function renderScreen(screens: readonly ScreenRoute[], ctx: ScreenContext): React.ReactNode {
    return (resolveScreen(screens, ctx.activeTab) ?? FALLBACK_SCREEN).render(ctx);
}
