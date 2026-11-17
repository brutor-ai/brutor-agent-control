/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { ServerCog, ShieldAlert } from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { TenantScoped } from '../common/TenantScoped';
import SystemOverviewPane from './SystemOverviewPane';
import SystemCountersPane from './SystemCountersPane';
import SystemCachesPane from './SystemCachesPane';
import SystemAuditPane from './SystemAuditPane';
import SystemChainsPane from './SystemChainsPane';
import EvidenceLogsSection from './EvidenceLogsSection';
import SystemConnectionsPane from './SystemConnectionsPane';
import SystemTasksPane from './SystemTasksPane';
import SystemSessionsPane from './SystemSessionsPane';
import SystemLogsPane from './SystemLogsPane';
import SystemMaintenancePane from './SystemMaintenancePane';
import { PERM_READ } from './systemPermissions';
import { SUB_SECTIONS, SUB_SECTION_BY_ROUTE, type SystemSubSection } from './systemConsoleSections';









// ─── Permission helpers ──────────────────────────────────────────────────────
//
// The System nav itself is hidden when `system:read` is absent (see
// AppSidebar.isVisible). But a curl-armed operator could still hit the
// route directly — so the parent component re-checks at render time and
// shows a hard-deny pane instead of the section. The same check guards
// destructive actions inside each sub-section page.


interface SystemConsoleTabProps {
    /**
     * The current `activeTab` value from App.tsx: the `system-*` id of
     * a sub-section.
     */
    activeTab: string;
    onTabChange: (tab: string) => void;
    currentUser?: { username?: string; role_name?: string | null; role_id?: string | null };
}

/**
 * The System console. Its routes are answered for one tenant, so an
 * administrator of no tenant is asked which tenant first.
 */
const SystemConsoleTab: React.FC<SystemConsoleTabProps> = (props) => (
    <TenantScoped pageName="The System console">
        <TenantSystemConsole {...props} />
    </TenantScoped>
);

const TenantSystemConsole: React.FC<SystemConsoleTabProps> = ({
    activeTab,
    onTabChange,
    currentUser,
}) => {
    const { hasPermission } = useAuth();

    const section = SUB_SECTION_BY_ROUTE[activeTab] ?? SUB_SECTIONS[0];

    // Permission gate. The parent's `requiresPermission` on the nav
    // item already hides the section for non-auditors, but defence-in-
    // depth: a direct URL hit must still get bounced.
    if (!hasPermission(PERM_READ)) {
        return <PermissionDeniedPane />;
    }

    return (
        <div className="app-column h-full">
            {/* Header */}
            <div className="px-6 pt-6 pb-4">
                <div className="flex items-center space-x-3 mb-2">
                    <ServerCog className="w-7 h-7 text-primary" />
                    <h1 className="text-2xl font-bold text-default">System Operations</h1>
                </div>
                <p className="text-secondary">
                    Operator console for runtime health, throttling, caches, and incident response.
                    Every action here is audited and takes effect on every gateway replica.
                </p>
                {currentUser && (
                    <div className="flex items-center mt-3">
                        <span className="text-sm text-secondary">
                            Logged in as {currentUser.username}
                            {(currentUser.role_name || currentUser.role_id) && (
                                <> ({currentUser.role_name || currentUser.role_id})</>
                            )}
                        </span>
                    </div>
                )}
            </div>

            {/* Sub-Tab Navigation */}
            <div className="flex border-b border-default bg-surface px-6 overflow-x-auto">
                {SUB_SECTIONS.map(({ id, routeId, label, icon: Icon }) => {
                    const isActive = section.id === id;
                    return (
                        <button
                            key={id}
                            data-testid={`system-tab-${id}`}
                            onClick={() => onTabChange(routeId)}
                            className={`flex items-center space-x-2 px-4 py-2.5 border-b-2 transition-colors text-sm font-medium whitespace-nowrap ${
                                isActive
                                    ? 'border-primary-500 text-primary-600'
                                    : 'border-transparent text-secondary hover:text-default hover:border-strong'
                            }`}
                        >
                            <Icon className="w-4 h-4" />
                            <span>{label}</span>
                        </button>
                    );
                })}
            </div>

            {/* Tab Content. Each sub-section lives in its own
                component. Dispatching here (vs. in App.tsx) keeps
                the sub-section catalogue + permission gating + tab
                nav in one place. */}
            <div className="scroll-region p-6">{PANES[section.id](onTabChange)}</div>
        </div>
    );
};

// ─── Permission-denied pane ──────────────────────────────────────────────────

const PermissionDeniedPane: React.FC = () => (
    <div className="app-column h-full">
        <div className="px-6 pt-6 pb-4">
            <div className="flex items-center space-x-3 mb-2">
                <ServerCog className="w-7 h-7 text-primary" />
                <h1 className="text-2xl font-bold text-default">System Operations</h1>
            </div>
        </div>
        <div className="scroll-region p-6">
            <div
                data-testid="system-permission-denied"
                className="max-w-2xl bg-surface rounded-lg border border-default p-6"
            >
                <div className="flex items-start space-x-4">
                    <div className="flex-shrink-0 p-3 rounded-lg bg-red-50 dark:bg-red-900/20">
                        <ShieldAlert className="w-6 h-6 text-red-600 dark:text-red-400" />
                    </div>
                    <div className="flex-1 min-h-0 min-w-0">
                        <h2 className="text-lg font-semibold text-default mb-2">
                            Access required
                        </h2>
                        <p className="text-secondary">
                            The System Operations console is restricted to roles that hold the{' '}
                            <code className="px-1.5 py-0.5 rounded bg-surface-muted text-xs">
                                system:read
                            </code>{' '}
                            permission. Ask your tenant administrator to assign the
                            Tenant Auditor or Tenant Read-Only role, or to add{' '}
                            <code className="px-1.5 py-0.5 rounded bg-surface-muted text-xs">
                                system:read
                            </code>{' '}
                            to an existing role.
                        </p>
                    </div>
                </div>
            </div>
        </div>
    </div>
);

// ─── Sub-section dispatch ────────────────────────────────────────────────────
//
// The pane of each sub-section of the catalogue. The type holds the two
// together: a sub-section without a pane does not compile.
const PANES: Record<SystemSubSection, (onTabChange: (tab: string) => void) => React.ReactNode> = {
    overview: (onTabChange) => <SystemOverviewPane onTabChange={onTabChange} />,
    counters: () => <SystemCountersPane />,
    caches: () => <SystemCachesPane />,
    // The row chains, then the tenant evidence logs and their head grades.
    chains: () => (
        <div className="space-y-8">
            <SystemChainsPane />
            <EvidenceLogsSection />
        </div>
    ),
    audit: () => <SystemAuditPane />,
    connections: () => <SystemConnectionsPane />,
    tasks: () => <SystemTasksPane />,
    sessions: () => <SystemSessionsPane />,
    logs: () => <SystemLogsPane />,
    maintenance: () => <SystemMaintenancePane />,
};

export default SystemConsoleTab;
