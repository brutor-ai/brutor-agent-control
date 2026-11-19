/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
    ShieldAlert,
    AlertTriangle,
    XCircle,
    Check,
    Info,
    TrendingUp,
    ShieldCheck,
    Scale,
    ScrollText,
    Users,
    Key,
    Gauge,
    FileCode,
    Boxes,
    Cpu,
    Server,
    Wrench,
    Bot,
    Hourglass,
    Fingerprint,
} from 'lucide-react';
import { ResourceGroup, UsageAlertInfo, GROUP_TYPE_LABELS } from '../../lib/groupTypes';
import { useGateway } from '../../hooks/useGateway';
import { useTenantTheme } from '../../context/TenantThemeContext';
import GroupGeneralTab from './subtabs/GroupGeneralTab';
import GroupUsageTab from './subtabs/GroupUsageTab';
import GroupResourceGovernanceTab from './subtabs/GroupResourceGovernanceTab';
import GroupApprovalWindowTab from './subtabs/GroupApprovalWindowTab';
import GroupIdentityTab from './subtabs/GroupIdentityTab';
import GroupGuardrailsPanel from './subtabs/GroupGuardrailsPanel';
import GroupMembersTab from './subtabs/GroupMembersTab';
import GroupAPIKeysTab from './subtabs/GroupAPIKeysTab';
import GroupResourceLimitsTab from './subtabs/GroupResourceLimitsTab';
import GroupComplianceTab from './subtabs/GroupComplianceTab';
// Classic per-type resource tabs (the RG's Resources view).
import GroupLLModelsTab from './subtabs/GroupLLModelsTab';
import GroupMCPServersTab from './subtabs/GroupMCPServersTab';
import GroupAgentSkillsTab from './subtabs/GroupAgentSkillsTab';
import GroupAgentsTab from './subtabs/GroupAgentsTab';
import { runBackground } from '../../lib/asyncSafe';
import { mergeTabs, useSlot } from '../../app/slots';
import { errorKind, failureSentence } from '../../lib/narrow';
import toast from 'react-hot-toast';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

// ============================================================================
// Tab Configuration
// ============================================================================

// The panel's own top-level tabs. More are contributed to the
// `resource-group.tabs` slot (Cache stats is), so a tab id is a string.
type OwnTab =
    | 'general'
    | 'usage'
    | 'resources'
    | 'members'
    | 'api-keys'
    | 'governance';

export type TabType = string;

// Sub-tabs under the Resources parent — the per-type resource views (which
// models / MCP servers / skills / agents this group can reach). More are
// contributed to the `resource-group.resource-tabs` slot (Knowledge Base is).
type OwnResourceSubTab = 'models' | 'mcp' | 'skills' | 'agents';

// Sub-tabs under the Governance parent. The order intentionally
// reads as a posture pipeline:
//   declare → admit → enforce → meter → policy-as-code.
type GovernanceSubTab =
    | 'compliance'      // Declare: which regimes apply, EU AI Act tier, PHI scope
    | 'identity'        // Admit: what credential a caller must present
    | 'guardrails'      // Enforce: runtime PII / prompt-injection / etc. checks
    | 'usage-limits'    // Meter: quotas / rate limits
    | 'usage-policies'  // Policy-as-code: argument policies, denylists
    | 'approvals';      // Human oversight: how long a hold waits for a person

type IconProps = { className?: string; style?: React.CSSProperties };

interface TabDef {
    id: OwnTab;
    label: string;
    icon: React.ComponentType<IconProps>;
}

interface GovernanceSubTabDef {
    id: GovernanceSubTab;
    label: string;
    icon: React.ComponentType<IconProps>;
}

interface ResourceSubTabDef {
    id: OwnResourceSubTab;
    label: string;
    icon: React.ComponentType<IconProps>;
}

const TABS: TabDef[] = [
    { id: 'general',        label: 'General',            icon: Info },
    { id: 'usage',          label: 'Usage',              icon: TrendingUp },
    { id: 'resources',      label: 'Resources',          icon: Boxes },
    { id: 'members',        label: 'Members',            icon: Users },
    { id: 'api-keys',       label: 'API Keys',           icon: Key },
    { id: 'governance',     label: 'Governance',         icon: Scale },
];

const RESOURCE_SUB_TABS: ResourceSubTabDef[] = [
    { id: 'models',  label: 'AI Models',   icon: Cpu },
    { id: 'mcp',     label: 'MCP Servers', icon: Server },
    { id: 'skills',  label: 'Skills',      icon: Wrench },
    { id: 'agents',  label: 'Agents',      icon: Bot },
];
const RESOURCE_SUB_TAB_STORAGE_KEY = 'groupDetailsResourceSubTab';

const GOVERNANCE_SUB_TABS: GovernanceSubTabDef[] = [
    { id: 'compliance',     label: 'Compliance',     icon: ScrollText },
    { id: 'identity',       label: 'Identity',       icon: Fingerprint },
    { id: 'guardrails',     label: 'Guardrails',     icon: ShieldCheck },
    { id: 'usage-limits',   label: 'Limits',         icon: Gauge },
    { id: 'usage-policies', label: 'Policy',         icon: FileCode },
    { id: 'approvals',      label: 'Approvals',      icon: Hourglass },
];

const TAB_STORAGE_KEY = 'groupDetailsActiveTab';
const GOV_SUB_TAB_STORAGE_KEY = 'groupDetailsGovernanceSubTab';

// ============================================================================
// Component
// ============================================================================

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
    onDelete: () => void;
    /** Deep-link target (e.g. Mission Control's "Open usage details" →
     *  'usage'). Wins over the locally persisted tab when set. */
    initialTab?: TabType;
    /** Sub-section within the General tab (e.g. 'ownership'), for deep
     *  links that target one field rather than the tab as a whole. */
    initialGeneralSection?: 'identity' | 'ownership' | 'hierarchy' | 'audit';
}

const GroupDetailsPanel: React.FC<Props> = ({ group, onUpdate, onDelete, initialTab, initialGeneralSection }) => {
    const { fetchGroupAlerts, acknowledgeUsageAlert } = useGateway();
    const { theme } = useTenantTheme();
    const primaryColor = theme.primary_color || '#1BB5E8';

    // ── Tabs: the panel's own plus those contributed to its slots ───────────
    const slotTabs = useSlot('resource-group.tabs');
    const slotResourceTabs = useSlot('resource-group.resource-tabs');
    const headerItems = useSlot('resource-group.header');
    const { tabs, contributed: contributedTabs } = useMemo(() => mergeTabs(TABS, slotTabs), [slotTabs]);
    const { tabs: resourceSubTabs, contributed: contributedResourceTabs } = useMemo(
        () => mergeTabs(RESOURCE_SUB_TABS, slotResourceTabs),
        [slotResourceTabs],
    );
    // ── Internal tab state (persisted) ──────────────────────────────────────
    //
    // The tab and the two sub-tabs are each remembered in localStorage. A
    // stored id that names no tab of the panel is ignored.
    const [activeTab, setActiveTab] = useState<TabType>(() => {
        try {
            const saved = localStorage.getItem(TAB_STORAGE_KEY);
            if (saved && tabs.some(t => t.id === saved)) return saved;
        } catch { /* ignore */ }
        return 'general';
    });

    const [governanceSubTab, setGovernanceSubTab] = useState<GovernanceSubTab>(() => {
        try {
            const savedSub = localStorage.getItem(GOV_SUB_TAB_STORAGE_KEY);
            if (savedSub && GOVERNANCE_SUB_TABS.some(t => t.id === savedSub)) {
                return savedSub as GovernanceSubTab;
            }
        } catch { /* ignore */ }
        return 'compliance';
    });

    // A deep-link target overrides whatever tab the operator left the
    // panel on last time — "Open usage details" must land on Usage, not
    // on a stale persisted tab.
    useEffect(() => {
        if (initialTab && tabs.some(t => t.id === initialTab)) {
            setActiveTab(initialTab);
        }
    }, [initialTab, tabs]);

    useEffect(() => {
        try { localStorage.setItem(TAB_STORAGE_KEY, activeTab); }
        catch { /* ignore */ }
    }, [activeTab]);

    useEffect(() => {
        try { localStorage.setItem(GOV_SUB_TAB_STORAGE_KEY, governanceSubTab); }
        catch { /* ignore */ }
    }, [governanceSubTab]);

    const [resourceSubTab, setResourceSubTab] = useState<string>(() => {
        try {
            const savedSub = localStorage.getItem(RESOURCE_SUB_TAB_STORAGE_KEY);
            if (savedSub && resourceSubTabs.some(t => t.id === savedSub)) {
                return savedSub;
            }
        } catch { /* ignore */ }
        return 'models';
    });

    useEffect(() => {
        try { localStorage.setItem(RESOURCE_SUB_TAB_STORAGE_KEY, resourceSubTab); }
        catch { /* ignore */ }
    }, [resourceSubTab]);

    // ── Alerts ──────────────────────────────────────────────────────────────
    const [groupAlerts, setGroupAlerts] = useState<UsageAlertInfo[]>([]);
    const [showAlertDropdown, setShowAlertDropdown] = useState(false);
    const [readFailure, reads] = useReadFailures<'alerts'>();

    const loadGroupAlerts = useCallback(async () => {
        try {
            const response = await fetchGroupAlerts(group.id, { status: 'active' });
            setGroupAlerts(response.alerts || []);
            reads.loaded('alerts');
        } catch (err) {
            // No badge is not "nothing is alerting": the header says the alerts were not read.
            setGroupAlerts([]);
            reads.lost('alerts', 'Failed to load the alerts of this group', err);
        }
    }, [group.id]);

    useEffect(() => {
        runBackground(loadGroupAlerts(), 'group alerts');
    }, [group.id]);

    const handleAcknowledge = async (alertId: string) => {
        try {
            await acknowledgeUsageAlert(alertId);
            setGroupAlerts(prev => prev.filter(a => a.id !== alertId));
        } catch (error) {
            console.error('Failed to acknowledge alert:', errorKind(error));
            toast.error(failureSentence('The alert was not acknowledged', error));
        }
    };

    const hardStopCount = groupAlerts.filter(a => a.alert_type === 'hard_stop').length;
    const warningCount = groupAlerts.filter(a => a.alert_type === 'warning').length;

    // ── Tab content ─────────────────────────────────────────────────────────
    //
    // Governance is a parent tab — its body is itself a tab strip with
    // four sub-tabs. The "declare → enforce → meter → policy-as-code"
    // ordering is intentional; see GOVERNANCE_SUB_TABS above.
    const renderGovernanceSubTabContent = () => {
        switch (governanceSubTab) {
            case 'compliance':     return <GroupComplianceTab group={group} onUpdate={onUpdate} />;
            case 'identity':       return <GroupIdentityTab group={group} />;
            case 'guardrails':     return <GroupGuardrailsPanel group={group} />;
            case 'usage-limits':   return <GroupResourceLimitsTab group={group} onUpdate={onUpdate} />;
            case 'usage-policies': return <GroupResourceGovernanceTab group={group} onUpdate={onUpdate} />;
            case 'approvals':      return <GroupApprovalWindowTab group={group} onUpdate={onUpdate} />;
            default:               return null;
        }
    };

    const renderGovernance = () => (
        <div className="app-column h-full">
            <div className="flex border-b border-default bg-surface-alt px-6">
                {GOVERNANCE_SUB_TABS.map(({ id, label, icon: Icon }) => {
                    const active = governanceSubTab === id;
                    return (
                        <button
                            key={id}
                            onClick={() => setGovernanceSubTab(id)}
                            className={`flex items-center gap-2 px-4 py-2.5 border-b-2 text-sm font-medium transition-colors whitespace-nowrap ${
                                active
                                    ? 'text-default'
                                    : 'border-transparent text-secondary hover:text-default hover:border-strong'
                            }`}
                            style={active ? { color: primaryColor, borderColor: primaryColor } : undefined}
                        >
                            <Icon
                                className="w-4 h-4"
                                style={active ? { color: primaryColor } : undefined}
                            />
                            <span>{label}</span>
                        </button>
                    );
                })}
            </div>
            <div className="scroll-region">
                {renderGovernanceSubTabContent()}
            </div>
        </div>
    );

    // Resources is a parent tab — the per-type resource views (which models /
    // MCP servers / skills / agents / KB this group can reach), classic tabs.
    const renderResourceSubTabContent = () => {
        const slotTab = contributedResourceTabs.get(resourceSubTab);
        if (slotTab) return slotTab.render({ group });
        switch (resourceSubTab) {
            case 'models':  return <GroupLLModelsTab group={group} onUpdate={onUpdate} />;
            case 'mcp':     return <GroupMCPServersTab group={group} onUpdate={onUpdate} />;
            case 'skills':  return <GroupAgentSkillsTab group={group} onUpdate={onUpdate} />;
            case 'agents':  return <GroupAgentsTab group={group} />;
            default:        return null;
        }
    };

    const renderResources = () => (
        <div className="app-column h-full">
            <div className="flex border-b border-default bg-surface-alt px-6">
                {resourceSubTabs.map(({ id, label, icon: Icon }) => {
                    const active = resourceSubTab === id;
                    return (
                        <button
                            key={id}
                            onClick={() => setResourceSubTab(id)}
                            className={`flex items-center gap-2 px-4 py-2.5 border-b-2 text-sm font-medium transition-colors whitespace-nowrap ${
                                active
                                    ? 'text-default'
                                    : 'border-transparent text-secondary hover:text-default hover:border-strong'
                            }`}
                            style={active ? { color: primaryColor, borderColor: primaryColor } : undefined}
                        >
                            <Icon
                                className="w-4 h-4"
                                style={active ? { color: primaryColor } : undefined}
                            />
                            <span>{label}</span>
                        </button>
                    );
                })}
            </div>
            <div className="scroll-region">
                {renderResourceSubTabContent()}
            </div>
        </div>
    );

    const renderTabContent = () => {
        const slotTab = contributedTabs.get(activeTab);
        if (slotTab) return slotTab.render({ group, onUpdate });
        switch (activeTab) {
            case 'general':      return <GroupGeneralTab group={group} onUpdate={onUpdate} onDelete={onDelete} initialSubTab={initialGeneralSection} />;
            case 'usage':        return <GroupUsageTab group={group} />;
            case 'resources':    return renderResources();
            case 'members':      return <GroupMembersTab group={group} onUpdate={onUpdate} />;
            case 'api-keys':     return <GroupAPIKeysTab group={group} />;
            case 'governance':   return renderGovernance();
            default:             return null;
        }
    };

    return (
        <div className="app-column h-full bg-surface">
            {/* ── Header ─────────────────────────────────────────────────── */}
            <div className="border-b border-default flex-shrink-0">
                <div className="px-6 py-4">
                    <div className="flex items-center justify-between">
                        <div>
                            <h1 className="text-xl font-semibold text-default">
                                Resource Group - {group.display_name || group.name}
                                {group.group_type && (
                                    <span className="text-base font-normal text-muted">
                                        {' '}({GROUP_TYPE_LABELS[group.group_type] ?? group.group_type})
                                    </span>
                                )}
                            </h1>
                            {group.hierarchy_path && (
                                <p className="text-sm text-muted mt-1">
                                    {group.hierarchy_path}
                                </p>
                            )}
                        </div>
                        <div className="flex items-center space-x-2">
                            <ReadFailureNotice compact failure={readFailure('alerts')} onRetry={() => runBackground(loadGroupAlerts(), 'group alerts')} />
                            {/* Usage Alert Badge */}
                            {groupAlerts.length > 0 && (
                                <div className="relative">
                                    <button
                                        onClick={() => setShowAlertDropdown(!showAlertDropdown)}
                                        className={`flex items-center space-x-1.5 px-3 py-1 text-sm font-medium rounded-full transition-colors ${
                                            hardStopCount > 0
                                                ? 'bg-surface-alt text-red-700 hover:bg-red-200'
                                                : 'bg-surface-alt text-yellow-700 hover:bg-yellow-200'
                                        }`}
                                    >
                                        {hardStopCount > 0 ? (
                                            <XCircle className="w-3.5 h-3.5" />
                                        ) : (
                                            <AlertTriangle className="w-3.5 h-3.5" />
                                        )}
                                        <span>
                                            {hardStopCount > 0
                                                ? `${hardStopCount} blocked`
                                                : `${warningCount} warning${warningCount !== 1 ? 's' : ''}`
                                            }
                                        </span>
                                    </button>

                                    {/* Alert Dropdown */}
                                    {showAlertDropdown && (
                                        <>
                                            <div
                                                className="fixed inset-0 z-40"
                                                onClick={() => setShowAlertDropdown(false)}
                                            />
                                            <div className="absolute right-0 top-full mt-2 w-80 bg-surface rounded-lg shadow-lg border border-default z-50 max-h-80 overflow-y-auto">
                                                <div className="p-3 border-b border-default flex items-center justify-between">
                                                    <div className="flex items-center space-x-2">
                                                        <ShieldAlert className="w-4 h-4 text-secondary" />
                                                        <span className="text-sm font-semibold text-default">
                                                            Usage Alerts
                                                        </span>
                                                    </div>
                                                    <span className="text-xs text-muted">
                                                        {groupAlerts.length} active
                                                    </span>
                                                </div>
                                                <div className="p-2 space-y-1.5">
                                                    {groupAlerts.map((alert) => (
                                                        <div
                                                            key={alert.id}
                                                            className={`p-2.5 rounded-md border text-sm ${
                                                                alert.alert_type === 'hard_stop'
                                                                    ? 'bg-surface-alt border-default'
                                                                    : 'bg-surface-alt border-default'
                                                            }`}
                                                        >
                                                            <div className="flex items-start justify-between gap-2">
                                                                <div className="flex-1 min-w-0 min-h-0">
                                                                    <div className="flex items-center gap-1.5">
                                                                        <span className={`text-xs font-semibold px-1.5 py-0.5 rounded ${
                                                                            alert.alert_type === 'hard_stop'
                                                                                ? 'bg-surface-alt text-red-700'
                                                                                : 'bg-surface-alt text-yellow-700'
                                                                        }`}>
                                                                            {alert.alert_type === 'hard_stop' ? 'BLOCKED' : 'WARNING'}
                                                                        </span>
                                                                        <span className="text-xs text-muted uppercase">
                                                                            {alert.resource_type}
                                                                        </span>
                                                                    </div>
                                                                    <p className="text-xs text-secondary mt-1">
                                                                        {alert.metric.replace(/_/g, ' ')} — {alert.utilization_pct.toFixed(0)}% used
                                                                    </p>
                                                                    <div className="w-full bg-surface-inset rounded-full h-1 mt-1.5">
                                                                        <div
                                                                            className={`h-1 rounded-full ${
                                                                                alert.utilization_pct >= 100 ? 'bg-red-500' : 'bg-yellow-500'
                                                                            }`}
                                                                            style={{ width: `${Math.min(100, alert.utilization_pct)}%` }}
                                                                        />
                                                                    </div>
                                                                    <p className="text-xs text-muted mt-1">
                                                                        {alert.period}
                                                                    </p>
                                                                </div>
                                                                <button
                                                                    onClick={(e) => {
                                                                        e.stopPropagation();
                                                                        runBackground(handleAcknowledge(alert.id), 'acknowledge');
                                                                    }}
                                                                    className="flex-shrink-0 p-1 rounded hover:bg-surface/80 text-muted hover:text-green-600 transition-colors"
                                                                    title="Acknowledge"
                                                                >
                                                                    <Check className="w-3.5 h-3.5" />
                                                                </button>
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        </>
                                    )}
                                </div>
                            )}

                            <span className={`px-3 py-1 text-sm font-medium rounded-full ${
                                group.is_active
                                    ? 'text-green-700 bg-surface-alt'
                                    : 'text-secondary bg-surface-raised'
                            }`}>
                                {group.is_active ? 'Active' : 'Inactive'}
                            </span>
                        </div>
                    </div>
                </div>

                {/* Items contributed to the `resource-group.header` slot (the
                    "Managed by policy" banner is). */}
                {headerItems.map((item) => (
                    <React.Fragment key={item.id}>{item.render({ group })}</React.Fragment>
                ))}

                {/* ── Horizontal Tab Bar ──────────────────────────────────── */}
                <div className="px-6 -mb-px flex space-x-1 overflow-x-auto">
                    {tabs.map(tab => {
                        const TabIcon = tab.icon;
                        const active = activeTab === tab.id;
                        return (
                            <button
                                key={tab.id}
                                onClick={() => setActiveTab(tab.id)}
                                className={`
                                    flex items-center space-x-1.5 px-3 py-2.5 text-xs font-medium
                                    border-b-2 transition-colors whitespace-nowrap
                                    ${active
                                    ? 'border-current text-default'
                                    : 'border-transparent text-muted hover:text-secondary hover:border-strong'
                                }
                                `}
                                style={active ? { color: primaryColor, borderColor: primaryColor } : undefined}
                            >
                                <TabIcon
                                    className="w-3.5 h-3.5"
                                    style={active ? { color: primaryColor } : undefined}
                                />
                                <span>{tab.label}</span>
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* ── Tab Content ────────────────────────────────────────────── */}
            <div className="scroll-region">
                {renderTabContent()}
            </div>
        </div>
    );
};

export default GroupDetailsPanel;
