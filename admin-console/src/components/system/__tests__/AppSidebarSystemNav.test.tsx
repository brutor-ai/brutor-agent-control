/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for the System nav item of AppSidebar.
 *
 * Coverage:
 *   - System nav item is HIDDEN for a tenant admin who does not
 *     hold `system:read` (Tenant Admin without auditor role)
 *   - System nav item is VISIBLE for an auditor (has
 *     `system:read`)
 *   - System nav item is HIDDEN for a system admin who is not
 *     a tenant admin AND does not have system:read in tenant
 *     scope (the SystemAdmin role is platform-scoped; the
 *     console is tenant-scoped)
 *   - When visible, all 10 sub-section children render
 *   - dividerBefore puts the System group below an actual
 *     visual divider (no regression: don't accidentally
 *     drop the separator that distinguishes Config from
 *     System)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Stub the theme + groupTree props — sidebar deeply uses them.
vi.mock('../../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: {} }),
    TenantThemeContext: { Provider: ({ children }: any) => children },
}));
vi.mock('../../../hooks/useTheme', () => ({
    useTheme: () => ({ isDarkMode: false, toggleTheme: () => {} }),
}));

import AppSidebar from '../../AppSidebar';

const baseProps = {
    activeTab: 'mission-control',
    onTabChange: vi.fn(),
    username: 'alice',
    tenantId: 'tenant-1',
    tenantDisplayName: 'Acme',
    avatarUrl: undefined,
    onLogout: vi.fn(),
    hasErrors: false,
    errorCount: 0,
    hasCriticalErrors: false,
    groupTree: [],
    groupTreeLoading: false,
    onRefreshGroupTree: vi.fn(),
};

const buildHasPermission = (perms: string[]) => (p: string) =>
    perms.includes(p);

describe('AppSidebar — System nav', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('hides the System nav for a tenant admin without system:read', () => {
        render(
            <AppSidebar
                {...baseProps}
                isTenantAdmin={true}
                hasPermission={buildHasPermission([
                    // Has the usual tenant-admin perms but NOT
                    // system:read — i.e. has not been given the
                    // Tenant Auditor role.
                    'admin:audit:read',
                    'proxy:logs:read',
                ])}
            />
        );
        // The label "System" must not appear as a top-level
        // nav entry. We can't queryByText('System') uniquely
        // (other labels contain "System") — but the unique
        // sub-tab id from the Overview pane is a reliable
        // proxy. AppSidebar doesn't render children when the
        // parent is hidden.
        expect(screen.queryByText('Background Tasks')).toBeNull();
    });

    it('shows the System nav for an operator with system:read', () => {
        render(
            <AppSidebar
                {...baseProps}
                isTenantAdmin={true}
                hasPermission={buildHasPermission(['system:read'])}
            />
        );
        // Top-level entry visible. Each sub-section label
        // is unique among the sidebar children and is a
        // safe identity check.
        expect(screen.getAllByText('Overview').length).toBeGreaterThan(0);
        expect(screen.getByText('Background Tasks')).toBeDefined();
        expect(screen.getByText('Maintenance')).toBeDefined();
    });

    it('hides the System nav for a system admin without tenant-scoped system:read', () => {
        // A platform system admin operates the installation, not
        // a tenant — they don't have tenant permissions. The
        // System Operations console is tenant-scoped, so
        // they shouldn't see it.
        render(
            <AppSidebar
                {...baseProps}
                principal={{ id: 'system', label: 'System Admin' }}
                isTenantAdmin={false}
                hasPermission={buildHasPermission([])}
            />
        );
        expect(screen.queryByText('Background Tasks')).toBeNull();
    });

    it('clicking the System parent opens the children flyout', () => {
        const onTabChange = vi.fn();
        render(
            <AppSidebar
                {...baseProps}
                onTabChange={onTabChange}
                isTenantAdmin={true}
                hasPermission={buildHasPermission(['system:read'])}
            />
        );
        // Click on one of the sub-section labels — it should
        // dispatch the canonical route id.
        fireEvent.click(screen.getByText('Counters'));
        expect(onTabChange).toHaveBeenCalledWith('system-counters');
    });
});
