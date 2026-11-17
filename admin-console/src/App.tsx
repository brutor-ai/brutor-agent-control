/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import { AlertCircle } from 'lucide-react';

import { env } from './env';

// Import components
import AppSidebar from './components/AppSidebar';
import { renderScreen } from './app/screens';
import { composeAdmin, type AdminComposition, type AdminExtension } from './app/extensions';
import { useSlot } from './app/slots';
import { SlotProvider } from './app/SlotProvider';
import { ErrorDisplay } from './components/common/ErrorDisplay';
import LoginForm from './components/LoginForm';
import PasswordChangeRequiredScreen from './components/users/PasswordChangeRequiredScreen';
import { ToastProvider } from './components/common/Toast';
import { ConfirmationProvider } from './components/common/ConfirmationDialog';

// Import the hooks and libs
import { useGateway } from './hooks/useGateway';
import { useAuth, AuthProvider } from './hooks/useAuth';
import { ThemeProvider } from './hooks/ThemeProvider';
import { TenantThemeProvider } from './context/TenantThemeContext';
import { TenantFeaturesProvider, useTenantFeatures } from './context/TenantFeaturesContext';
import { GroupHierarchyTree as GroupTree } from './lib/groupTypes';
import { asyncHandler, runBackground } from './lib/asyncSafe';
import { useReadFailures } from './hooks/useRead';
import ReadFailureNotice from './components/common/ReadFailureNotice';
import { errorKind } from './lib/narrow';


const AppContent: React.FC<{ composition: AdminComposition }> = ({ composition }) => {
    const [activeTab, setActiveTabRaw] = useState('mission-control');

    const setActiveTab = React.useCallback((tab: string) => {
        setActiveTabRaw(tab);
    }, []);

    // Authentication
    const {
        user,
        isAuthenticated,
        isLoading: authLoading,
        logout,
        isTokenExpired,
        isTenantAdmin,
        principal,
        getCurrentTenantId,
        hasPermission,
    } = useAuth();

    // Set the correct default tab whenever the authenticated user changes.
    useEffect(() => {
        if (isAuthenticated && user) {
            // A contributed principal says where the console opens for it.
            setActiveTab(principal?.landingTab ?? 'mission-control');
        }
    }, [isAuthenticated, user?.id, principal, isTenantAdmin]);

    // Use the useGateway hook
    const {
        errors,
        hasErrors,
        hasCriticalErrors,
        dismissError,
        clearErrors,
        getErrorsBySeverity,
        initialLoading: gatewayInitialLoading,
        fetchGroupHierarchyTree,
        fetchAssuranceInboxCounts,
    } = useGateway();

    // Sidebar badges for Mission Control → Inbox: unresolved findings by
    // severity, refreshed on a slow poll. Best-effort — a failed read leaves
    // the badges as they were rather than blanking them.
    const [inboxBadges, setInboxBadges] = useState<{ critical: number; warning: number } | null>(null);
    useEffect(() => {
        if (!isAuthenticated || !isTenantAdmin()) return;
        let cancelled = false;
        const read = async () => {
            try {
                const c = await fetchAssuranceInboxCounts();
                if (!cancelled) setInboxBadges({ critical: c.open_critical ?? 0, warning: c.open_warning ?? 0 });
            } catch {
                // The badges stay at their last value; the Inbox says its own failure.
            }
        };
        void read();
        const t = setInterval(() => void read(), 60_000);
        return () => {
            cancelled = true;
            clearInterval(t);
        };
    }, [isAuthenticated, isTenantAdmin, fetchAssuranceInboxCounts, activeTab]);

    // Tenant feature flags + display name — loaded by TenantFeaturesProvider
    // from /v1/admin/tenants/current (always fresh; avoids stale login cache).
    const { isFeatureEnabled, displayName: tenantDisplayNameFromApi, failure: tenantFeaturesFailure, reload: reloadTenantFeatures } = useTenantFeatures();
    const banners = useSlot('app.banners');

    // ── Group hierarchy tree (shared between sidebar and content) ────────────
    const [groupTree, setGroupTree] = useState<GroupTree[]>([]);
    const [groupTreeLoading, setGroupTreeLoading] = useState(false);
    const [readFailure, reads] = useReadFailures<'groups'>();
    const [groupTreeVersion, setGroupTreeVersion] = useState(0);

    const loadGroupTree = useCallback(async () => {
        try {
            setGroupTreeLoading(true);
            const response = await fetchGroupHierarchyTree();
            setGroupTree(response.tree);
            reads.loaded('groups');
        } catch (err) {
            reads.failed('groups', 'Failed to load the resource groups', err);
        } finally {
            setGroupTreeLoading(false);
        }
    }, [fetchGroupHierarchyTree, reads]);

    // Fetch tree on mount and whenever version bumps (create/update/delete).
    // The tree is one tenant's: an administrator of no tenant (a contributed
    // principal) has none to show, and the control plane refuses the request
    // without a tenant.
    useEffect(() => {
        if (isAuthenticated && user && !principal) {
            runBackground(loadGroupTree(), 'group tree');
        }
    }, [isAuthenticated, user?.id, principal, groupTreeVersion]);

    const refreshGroupTree = useCallback(() => {
        setGroupTreeVersion(v => v + 1);
    }, []);

    // ── User info ───────────────────────────────────────────────────────────
    const username = React.useMemo(() => {
        return user?.username || user?.email || user?.full_name || 'User';
    }, [user]);

    const tenantId = React.useMemo(() => {
        return getCurrentTenantId?.() || user?.tenant_id || 'default';
    }, [user, getCurrentTenantId]);

    const tenantDisplayName = React.useMemo(() => {
        // Prefer the fresh value from /tenants/current; fall back to login
        // response, then raw tenantId so existing consumers never see empty.
        return tenantDisplayNameFromApi || user?.tenant_display_name || tenantId;
    }, [tenantDisplayNameFromApi, user, tenantId]);

    // ── Auth gates ──────────────────────────────────────────────────────────
    if (authLoading || gatewayInitialLoading) {
        return (
            <div className="min-h-screen bg-surface-alt flex items-center justify-center">
                <div className="text-center">
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
                    <p className="text-secondary mb-2">
                        {authLoading ? 'Checking authentication...' : 'Initializing...'}
                    </p>
                    <p className="text-sm text-muted">Please wait while we verify your session.</p>
                </div>
            </div>
        );
    }

    if (!isAuthenticated || !user || isTokenExpired) {
        // One form, wherever the console is opened: its own, with an option
        // per contributed principal.
        return <LoginForm />;
    }

    const criticalErrors = getErrorsBySeverity('critical');
    const highErrors = getErrorsBySeverity('high');

    const handleLogout = async () => {
        try {
            await logout();
        } catch (error) {
            console.error('Logout error:', errorKind(error));
        }
    };

    // ── Tab content routing ─────────────────────────────────────────────────
    // Which screen answers which tab id is defined in `app/screens.tsx`.
    const renderTabContent = () =>
        renderScreen(composition.screens, { activeTab, setActiveTab, user, isFeatureEnabled, refreshGroupTree });

    const userIsTenantAdmin = isTenantAdmin?.() ?? false;

    return (
        <div className="app-shell bg-surface-raised">
            {/* ── Left Sidebar ────────────────────────────────────────────── */}
            <AppSidebar
                sections={composition.navSections}
                activeTab={activeTab}
                onTabChange={setActiveTab}
                username={username}
                tenantId={tenantId}
                tenantDisplayName={tenantDisplayName}
                isTenantAdmin={userIsTenantAdmin}
                principal={principal}
                avatarUrl={user?.avatar_url}
                onLogout={asyncHandler(handleLogout, 'logout')}
                hasErrors={hasErrors}
                errorCount={errors.length}
                hasCriticalErrors={hasCriticalErrors}
                inboxBadges={inboxBadges}
                groupTree={groupTree}
                groupTreeLoading={groupTreeLoading}
                groupTreeFailure={readFailure('groups')}
                onRefreshGroupTree={refreshGroupTree}
                hasPermission={hasPermission}
            />

            {/* ── Main Content Area ───────────────────────────────────────── */}
            <div className="flex-1 flex flex-col min-w-0 min-h-0">
                {/* Banners contributed to the `app.banners` slot (the trial banner is). */}
                {banners.map((banner) => (
                    <React.Fragment key={banner.id}>{banner.render({ user })}</React.Fragment>
                ))}

                <ReadFailureNotice className="mx-4 mt-3 mb-0 !p-3 flex-shrink-0" failure={tenantFeaturesFailure} onRetry={reloadTenantFeatures} />

                {/* Critical Error Banner */}
                {hasCriticalErrors && (
                    <div className="mx-4 mt-3 p-3 bg-red-50 border border-red-200 rounded-lg flex-shrink-0">
                        <div className="flex items-center justify-between">
                            <div className="flex items-center space-x-2">
                                <AlertCircle className="w-5 h-5 text-red-600" />
                                <div>
                                    <h4 className="text-red-800 font-medium text-sm">Critical System Errors</h4>
                                    <p className="text-red-700 text-xs">
                                        {criticalErrors.length} critical error{criticalErrors.length !== 1 ? 's' : ''} may affect gateway functionality.
                                    </p>
                                </div>
                            </div>
                            <button
                                onClick={() => clearErrors()}
                                className="text-red-600 hover:text-red-800 text-xs underline flex-shrink-0 ml-4"
                            >
                                Dismiss
                            </button>
                        </div>
                    </div>
                )}

                {/* High Priority Errors */}
                {highErrors.length > 0 && !hasCriticalErrors && (
                    <div className="mx-4 mt-3 flex-shrink-0">
                        <ErrorDisplay
                            errors={highErrors}
                            onDismiss={dismissError}
                            className="mb-0"
                        />
                    </div>
                )}

                {/* Page content */}
                <div className="scroll-region">
                    {renderTabContent()}
                </div>
            </div>
        </div>
    );
};

/**
 * An account that must change its password first (its sign-in or `/me`
 * said so, or a route was refused with 403 `password_change_required`) gets
 * the change-password screen and nothing else: none of what is below it is
 * mounted, so nothing asks the Control Plane a route it refuses or the core
 * a session it refuses, and nothing signs the account out in a loop.
 */
const PasswordChangeGate: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const { passwordChangeRequired } = useAuth();
    return passwordChangeRequired ? <PasswordChangeRequiredScreen /> : <>{children}</>;
};

/**
 * The console rendered from a composition: its slots, screens and
 * navigation. The slots are provided outermost, because the auth provider
 * reads one (`app.principals`: who can sign in beside the tenant
 * administrator).
 */
export const AppShell: React.FC<{ composition: AdminComposition }> = ({ composition }) => (
    <SlotProvider slots={composition.slots}>
        <AuthProvider gatewayUrl={env.BRUTOR_CONTROL_PLANE_PUBLIC_URL}>
            <ThemeProvider>
                <PasswordChangeGate>
                    <TenantThemeProvider>
                        <TenantFeaturesProvider>
                            <ToastProvider>
                                <ConfirmationProvider>
                                    <AppContent composition={composition} />
                                </ConfirmationProvider>
                            </ToastProvider>
                        </TenantFeaturesProvider>
                    </TenantThemeProvider>
                </PasswordChangeGate>
            </ThemeProvider>
        </AuthProvider>
    </SlotProvider>
);

/**
 * The console. `extensions` are composed with the built-in registries
 * (`app/extensions.ts`); with none the console is the built-in
 * screens, navigation, slots and API modules and nothing else.
 */
const App: React.FC<{ extensions?: readonly AdminExtension[] }> = ({ extensions }) => {
    // Composed once per extension list, before anything renders from it; a
    // refused extension throws here and nothing is installed.
    const composition = React.useMemo(() => composeAdmin(extensions), [extensions]);

    return <AppShell composition={composition} />;
};

export default App;
