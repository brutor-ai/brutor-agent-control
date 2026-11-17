/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Compliance console shell: tab strip dispatch, the
 * framework switcher (registry-fed, EU AI Act default, persisted).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const listComplianceFrameworks = vi.fn();
// One client object for the whole module: the real hook hands out a stable
// instance, and components list it as an effect dependency.
const client = { listComplianceFrameworks };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: client }),
}));

vi.mock('../pages/OverviewPage', () => ({
    default: (p: { framework: string }) => <div data-testid="page-overview">overview:{p.framework}</div>,
}));
vi.mock('../pages/FrameworksPage', () => ({ default: () => <div data-testid="page-frameworks" /> }));
vi.mock('../pages/ProfilesPage', () => ({ default: () => <div data-testid="page-profiles" /> }));
vi.mock('../pages/ObligationsPage', () => ({
    default: (p: { framework: string }) => <div data-testid="page-obligations">obligations:{p.framework}</div>,
}));
vi.mock('../pages/EvidenceLedgerPage', () => ({ default: () => <div data-testid="page-evidence" /> }));
vi.mock('../pages/OversightPage', () => ({ default: () => <div data-testid="page-oversight" /> }));
vi.mock('../pages/TransparencyPage', () => ({ default: () => <div data-testid="page-transparency" /> }));
vi.mock('../pages/ReportsPage', () => ({ default: () => <div data-testid="page-reports" /> }));
vi.mock('../pages/IncidentsPage', () => ({ default: () => <div data-testid="page-incidents" /> }));

import ComplianceConsoleTab from '../ComplianceConsoleTab';
import { COMPLIANCE_SECTIONS, FRAMEWORK_STORAGE_KEY } from '../complianceSections';

const REGISTRY = {
    items: [
        { id: 'eu-ai-act', title: 'EU AI Act', enabled: true, obligation_count: 9, offered: true },
        { id: 'iso-42001', title: 'ISO/IEC 42001', enabled: true, obligation_count: 0, offered: true },
        { id: 'soc2', title: 'SOC 2', enabled: false, obligation_count: 0, offered: true },
    ],
};

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    listComplianceFrameworks.mockResolvedValue(REGISTRY);
});

describe('ComplianceConsoleTab', () => {
    it('renders a tab per catalogue page and dispatches the route id', async () => {
        const onTabChange = vi.fn();
        render(<ComplianceConsoleTab activeTab="compliance-overview" onTabChange={onTabChange} />);
        const tabs = screen.getAllByRole('tab');
        expect(tabs.map((t) => t.textContent)).toHaveLength(COMPLIANCE_SECTIONS.length);
        expect(screen.getByTestId('compliance-tab-overview')).toHaveAttribute('aria-selected', 'true');
        fireEvent.click(screen.getByTestId('compliance-tab-evidence'));
        expect(onTabChange).toHaveBeenCalledWith('compliance-evidence');
        await screen.findByTestId('page-overview');
    });

    it('renders the page for the active route', () => {
        for (const s of COMPLIANCE_SECTIONS) {
            const { unmount } = render(<ComplianceConsoleTab activeTab={s.routeId} onTabChange={vi.fn()} />);
            expect(screen.getByTestId(`page-${s.id}`)).toBeInTheDocument();
            unmount();
        }
    });

    it('feeds the switcher from the registry, defaults to the EU AI Act and persists the choice', async () => {
        render(<ComplianceConsoleTab activeTab="compliance-overview" onTabChange={vi.fn()} />);
        const select = await screen.findByTestId<HTMLSelectElement>('framework-switcher');
        await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
        expect(select.value).toBe('eu-ai-act');
        expect(screen.getByText('SOC 2 (not enabled)')).toBeInTheDocument();

        fireEvent.change(select, { target: { value: 'iso-42001' } });
        expect(window.localStorage.getItem(FRAMEWORK_STORAGE_KEY)).toBe('iso-42001');
        expect(screen.getByTestId('page-overview')).toHaveTextContent('overview:iso-42001');
    });

    it('restores the persisted framework', async () => {
        window.localStorage.setItem(FRAMEWORK_STORAGE_KEY, 'soc2');
        render(<ComplianceConsoleTab activeTab="compliance-overview" onTabChange={vi.fn()} />);
        await screen.findByTestId('framework-switcher');
        expect(screen.getByTestId('page-overview')).toHaveTextContent('overview:soc2');
    });

    it('an id that names no page shows Overview, with the remembered framework', async () => {
        for (const other of ['compliance', 'iso42001', 'eu-ai-act']) {
            window.localStorage.setItem(FRAMEWORK_STORAGE_KEY, 'soc2');
            const { unmount } = render(<ComplianceConsoleTab activeTab={other} onTabChange={vi.fn()} />);
            await screen.findByTestId('framework-switcher');
            expect(screen.getByTestId('page-overview')).toHaveTextContent('overview:soc2');
            unmount();
        }
    });

    it('choosing a framework stays on the page', async () => {
        const onTabChange = vi.fn();
        render(<ComplianceConsoleTab activeTab="compliance-obligations" onTabChange={onTabChange} />);
        const select = await screen.findByTestId<HTMLSelectElement>('framework-switcher');
        await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
        fireEvent.change(select, { target: { value: 'soc2' } });
        expect(onTabChange).not.toHaveBeenCalled();
        expect(window.localStorage.getItem(FRAMEWORK_STORAGE_KEY)).toBe('soc2');
        expect(screen.getByTestId('page-obligations')).toHaveTextContent('obligations:soc2');
    });

    it('reports a registry failure without losing the switcher', async () => {
        listComplianceFrameworks.mockRejectedValueOnce(new Error('registry down'));
        render(<ComplianceConsoleTab activeTab="compliance-overview" onTabChange={vi.fn()} />);
        expect(await screen.findByTestId('frameworks-error')).toHaveTextContent('registry down');
        expect(screen.getByTestId<HTMLSelectElement>('framework-switcher').value).toBe('eu-ai-act');
    });
});
