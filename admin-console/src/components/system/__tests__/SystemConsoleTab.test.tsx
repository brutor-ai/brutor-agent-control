/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemConsoleTab — sub-section dispatch + permission gating.
 *
 * The pane internals (each SystemFooPane) are mocked here so this suite
 * stays focused on the catalogue, the dispatch switch, and the
 * permission gate. Pane-specific behaviour is covered by each pane's
 * own test file.
 *
 * Coverage:
 *   - All 10 sub-section tabs render in the expected order
 *   - A route id that names no sub-section shows Overview
 *   - Each sub-section route dispatches to the correct pane, and
 *     every entry of the catalogue has one
 *   - Permission-denied pane renders when SYSTEM_READ is absent
 *     (defence-in-depth: nav already hides the link, but a
 *     URL-deep-link must also be bounced)
 *   - onTabChange fires with the sub-section route id when a
 *     tab is clicked (lets the sidebar stay in sync)
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock useAuth before importing SystemConsoleTab so the hook
// reads from a controllable double rather than the real
// AuthProvider (which would need a wrapping <AuthProvider />
// and a real fetch mock for the user profile load).
const mockHasPermission = vi.fn();
let principal: unknown = null;
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission, principal }),
}));

// Mock every real pane so this suite stays focused on dispatch
// behaviour. Each pane has its own test file for the internals.
vi.mock('../SystemOverviewPane', () => ({
    default: () => <div data-testid="mock-overview-pane" />,
}));
vi.mock('../SystemCountersPane', () => ({
    default: () => <div data-testid="mock-counters-pane" />,
}));
vi.mock('../SystemCachesPane', () => ({
    default: () => <div data-testid="mock-caches-pane" />,
}));
vi.mock('../SystemAuditPane', () => ({
    default: () => <div data-testid="mock-audit-pane" />,
}));
vi.mock('../SystemConnectionsPane', () => ({
    default: () => <div data-testid="mock-connections-pane" />,
}));
vi.mock('../SystemTasksPane', () => ({
    default: () => <div data-testid="mock-tasks-pane" />,
}));
vi.mock('../SystemSessionsPane', () => ({
    default: () => <div data-testid="mock-sessions-pane" />,
}));
vi.mock('../SystemLogsPane', () => ({
    default: () => <div data-testid="mock-logs-pane" />,
}));
vi.mock('../SystemMaintenancePane', () => ({
    default: () => <div data-testid="mock-maintenance-pane" />,
}));

import SystemConsoleTab from '../SystemConsoleTab';
import { SUB_SECTIONS } from '../systemConsoleSections';
import { PERM_READ } from '../systemPermissions';

// Helper: configure mockHasPermission to grant a specific set
// of permissions. The mock default for any unlisted permission
// is false — matches how the real hook behaves for absent
// permissions.
const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

describe('SystemConsoleTab', () => {
    beforeEach(() => {
        mockHasPermission.mockReset();
        principal = null;
    });

    it('asks an administrator of no tenant which tenant to show before it shows a pane', () => {
        // Every route of the console is answered for one tenant; the control
        // plane answers such an administrator 400 unless the request names it.
        grant([PERM_READ]);
        principal = { id: 'system' };
        render(<SystemConsoleTab activeTab="system-overview" onTabChange={() => {}} />);
        expect(screen.getByTestId('tenant-needed')).toHaveTextContent('The System console shows the data of one tenant.');
        expect(screen.queryByTestId('system-tab-overview')).toBeNull();
    });

    describe('tab navigation', () => {
        it('renders all 10 sub-section tabs in canonical order', () => {
            grant([PERM_READ]);
            render(
                <SystemConsoleTab
                    activeTab="system-overview"
                    onTabChange={() => {}}
                />
            );
            const expectedIds = [
                'overview',
                'counters',
                'caches',
                'chains',
                'audit',
                'connections',
                'tasks',
                'sessions',
                'logs',
                'maintenance',
            ];
            // The catalogue is the spec — the assertion guards
            // against accidental reordering of UI tabs vs the
            // documented IA order.
            expect(SUB_SECTIONS.map(s => s.id)).toEqual(expectedIds);
            for (const id of expectedIds) {
                expect(screen.getByTestId(`system-tab-${id}`)).toBeDefined();
            }
        });

        it('clicking a tab dispatches the sub-section route id', () => {
            grant([PERM_READ]);
            const onTabChange = vi.fn();
            render(
                <SystemConsoleTab
                    activeTab="system-overview"
                    onTabChange={onTabChange}
                />
            );
            fireEvent.click(screen.getByTestId('system-tab-counters'));
            expect(onTabChange).toHaveBeenCalledWith('system-counters');
        });

        it.each(['system-bogus-not-real', 'system'])('shows Overview for %s, a route id that names no sub-section', (route) => {
            grant([PERM_READ]);
            render(<SystemConsoleTab activeTab={route} onTabChange={() => {}} />);
            expect(screen.getByTestId('mock-overview-pane')).toBeDefined();
        });
    });

    describe('sub-section dispatch', () => {
        // Each route id should mount the matching pane. The point of
        // this slice is "the right component dispatches for the right
        // route id" — pane internals are covered elsewhere.
        it.each([
            ['system-overview', 'mock-overview-pane'],
            ['system-counters', 'mock-counters-pane'],
            ['system-caches', 'mock-caches-pane'],
            ['system-audit', 'mock-audit-pane'],
            ['system-connections', 'mock-connections-pane'],
            ['system-tasks', 'mock-tasks-pane'],
            ['system-sessions', 'mock-sessions-pane'],
            ['system-logs', 'mock-logs-pane'],
            ['system-maintenance', 'mock-maintenance-pane'],
        ])('%s dispatches to %s', (route, paneTestId) => {
            grant([PERM_READ]);
            render(
                <SystemConsoleTab
                    activeTab={route}
                    onTabChange={() => {}}
                />
            );
            expect(screen.getByTestId(paneTestId)).toBeDefined();
        });
    });

    describe('the catalogue', () => {
        it('holds what the tab strip and the header read, and no action of its own', () => {
            for (const section of SUB_SECTIONS) {
                expect(Object.keys(section).sort()).toEqual(['description', 'icon', 'id', 'label', 'routeId']);
            }
        });

        it('has a pane for every entry: the console source has no stand-in for a missing one', () => {
            const source = readFileSync(resolve(__dirname, '../SystemConsoleTab.tsx'), 'utf8');
            for (const section of SUB_SECTIONS) {
                expect(source).toContain(`${section.id}: `);
            }
            expect(source).not.toMatch(/Placeholder|Not available yet|sample/i);
        });
    });

    describe('permission gating', () => {
        it('renders permission-denied pane when SYSTEM_READ is absent', () => {
            grant([]); // No permissions at all
            render(
                <SystemConsoleTab
                    activeTab="system-overview"
                    onTabChange={() => {}}
                />
            );
            expect(
                screen.getByTestId('system-permission-denied')
            ).toBeDefined();
            // None of the regular tabs should render in the
            // deny path.
            expect(screen.queryByTestId('system-tab-overview')).toBeNull();
            // And no real pane should be mounted either.
            expect(screen.queryByTestId('mock-overview-pane')).toBeNull();
        });

        it('renders the full console (tabs + pane) when SYSTEM_READ is present', () => {
            grant([PERM_READ]);
            render(
                <SystemConsoleTab
                    activeTab="system-overview"
                    onTabChange={() => {}}
                />
            );
            expect(screen.getByTestId('system-tab-overview')).toBeDefined();
            expect(screen.getByTestId('mock-overview-pane')).toBeDefined();
            expect(
                screen.queryByTestId('system-permission-denied')
            ).toBeNull();
        });
    });
});
