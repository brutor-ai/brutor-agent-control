/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control → Approvals in the sidebar: shown
 * to whoever may read the queue (`tool_approval:read`), absent otherwise,
 * and it opens the `approvals` screen.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: {} }),
    TenantThemeContext: { Provider: ({ children }: { children: React.ReactNode }) => children },
}));
vi.mock('../../../hooks/useTheme', () => ({
    useTheme: () => ({ isDarkMode: false, toggleTheme: () => {} }),
}));

import AppSidebar from '../../AppSidebar';
import { NAV_SECTIONS } from '../../../app/navigation';
import { TOOL_APPROVAL_READ } from '../../../lib/toolApprovalTypes';

const onTabChange = vi.fn();
const baseProps = {
    activeTab: 'mission-control',
    onTabChange,
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
    isTenantAdmin: true,
};

describe('Approvals in the sidebar', () => {
    beforeEach(() => vi.clearAllMocks());

    it('is declared next to the Inbox, behind the read permission and no other gate', () => {
        const section = NAV_SECTIONS.find((s) => s.label === 'Mission Control');
        const ids = (section?.items ?? []).map((i) => i.id);
        expect(ids.indexOf('approvals')).toBe(ids.indexOf('inbox') + 1);
        const item = section?.items.find((i) => i.id === 'approvals');
        expect(item?.requiresPermission).toBe(TOOL_APPROVAL_READ);
        expect(item?.requiresPrincipal).toBeUndefined();
        expect(item?.requiresGate).toBeUndefined();
    });

    it('is shown to an administrator who may read approvals, and opens the screen', () => {
        render(<AppSidebar {...baseProps} hasPermission={(p: string) => p === TOOL_APPROVAL_READ} />);
        fireEvent.click(screen.getByText('Approvals'));
        expect(onTabChange).toHaveBeenCalledWith('approvals');
    });

    it('is absent for one who may not', () => {
        render(<AppSidebar {...baseProps} hasPermission={() => false} />);
        expect(screen.queryByText('Approvals')).not.toBeInTheDocument();
        // the rest of Mission Control is still there
        expect(screen.getByText('Inbox')).toBeInTheDocument();
    });
});
