/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the Resource Group details panel test share: the
 * gateway doubles, the stubs that stand for the panel's panes, and the group
 * every case opens. The panel's own cases are Community tests
 * (`components/groups/__tests__/GroupDetailsPanel.test.tsx`); the cases that
 * need a real Enterprise tab on the panel are under `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the panel, so that a mock factory can await it.
 */

import { vi } from 'vitest';

import { GroupType, type ResourceGroup } from '../../lib/groupTypes';

export const mockFetchGroupAlerts = vi.fn();
export const mockAcknowledgeUsageAlert = vi.fn();
export const mockListPolicyBundles = vi.fn();

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({
    useGateway: () => ({
        fetchGroupAlerts: mockFetchGroupAlerts,
        acknowledgeUsageAlert: mockAcknowledgeUsageAlert,
    }),
});

/** `hooks/useAuth`. */
export const useAuthModule = () => {
    const gatewayClient = { listPolicyBundles: (...a: unknown[]): unknown => mockListPolicyBundles(...a) };
    return { useAuth: () => ({ gatewayClient }) };
};

/** `context/TenantThemeContext`. */
export const tenantThemeModule = () => ({ useTenantTheme: () => ({ theme: {} }) });

/** `GroupGeneralTab`: shows the section it was asked to open on. */
export const generalTabModule = () => ({
    default: ({ initialSubTab }: { initialSubTab?: string }) => (
        <div data-testid="pane-general">{initialSubTab ?? 'no-section'}</div>
    ),
});

/** Any other pane of the panel: an empty element, `pane-<name>`. */
export const paneModule = (name: string) => ({ default: () => <div data-testid={`pane-${name}`} /> });

export function group(overrides: Partial<ResourceGroup> = {}): ResourceGroup {
    return {
        id: 'rg-1',
        name: 'engineering',
        display_name: 'Engineering',
        group_type: GroupType.DEPARTMENT,
        is_active: true,
        hierarchy_path: 'acme/engineering',
        ...overrides,
    } as ResourceGroup;
}

/** How every case starts: no alert, no policy bundle, the console quiet. For a `beforeEach`. */
export function resetGroupDetailsPanel(): void {
    vi.clearAllMocks();
    mockFetchGroupAlerts.mockResolvedValue({ alerts: [] });
    mockAcknowledgeUsageAlert.mockResolvedValue({});
    mockListPolicyBundles.mockResolvedValue({ bundles: [] });
    vi.spyOn(console, 'error').mockImplementation(() => {});
}
