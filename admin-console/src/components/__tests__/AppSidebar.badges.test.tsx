/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: { primary_color: '#1bb5e8', secondary_color: '#87ceeb' } }),
}));
vi.mock('../../hooks/useTheme', () => ({
    useTheme: () => ({ theme: 'light', setTheme: vi.fn() }),
}));

import AppSidebar from '../AppSidebar';

function sidebar(badges: { critical: number; warning: number } | null) {
    return (
        <AppSidebar
            activeTab="mission-control"
            onTabChange={vi.fn()}
            username="admin"
            tenantId="t1"
            isTenantAdmin
            onLogout={vi.fn()}
            hasErrors={false}
            errorCount={0}
            hasCriticalErrors={false}
            inboxBadges={badges}
            groupTree={[]}
            groupTreeLoading={false}
            onRefreshGroupTree={vi.fn()}
            hasPermission={() => true}
        />
    );
}

describe('AppSidebar — Inbox badges', () => {
    it('shows a red critical badge and a yellow warning badge on the Inbox item', () => {
        render(sidebar({ critical: 8, warning: 10 }));
        expect(screen.getByTestId('inbox-badge-critical')).toHaveTextContent('8');
        expect(screen.getByTestId('inbox-badge-critical').className).toContain('bg-red-100');
        expect(screen.getByTestId('inbox-badge-warning')).toHaveTextContent('10');
        expect(screen.getByTestId('inbox-badge-warning').className).toContain('bg-yellow-100');
    });

    it('shows only the badge with a count, and nothing when both are zero', () => {
        const { unmount } = render(sidebar({ critical: 0, warning: 3 }));
        expect(screen.queryByTestId('inbox-badge-critical')).not.toBeInTheDocument();
        expect(screen.getByTestId('inbox-badge-warning')).toHaveTextContent('3');
        unmount();
        render(sidebar({ critical: 0, warning: 0 }));
        expect(screen.queryByTestId('inbox-badges')).not.toBeInTheDocument();
    });
});
