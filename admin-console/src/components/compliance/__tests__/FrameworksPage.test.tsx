/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const setComplianceFrameworkEnabled = vi.fn();
const getComplianceOverview = vi.fn();
const getComplianceDashboard = vi.fn();
const downloadComplianceExportCsv = vi.fn();
const downloadBlob = vi.fn();
const toast = { success: vi.fn(), error: vi.fn() };

const gateway = {
    gatewayClient: { setComplianceFrameworkEnabled, getComplianceOverview, getComplianceDashboard, downloadComplianceExportCsv },
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../../lib/csvExport', () => ({ downloadBlob: (...a: unknown[]) => { downloadBlob(...a); } }));

import FrameworksPage from '../pages/FrameworksPage';
import { clearFrameworkCatalogCache } from '../useFrameworkCatalog';
import type { ComplianceFramework } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS: ComplianceFramework[] = [
    {
        id: 'eu-ai-act', tag_key: 'eu_ai_act', title: 'EU AI Act', kind: 'regulation', jurisdiction: 'EU',
        reference: 'Regulation (EU) 2024/1689', catalog_version: '2026.09', verified_against_instrument: true, offered: true, unavailable_reason: null,
        retention_floor_days: 3650, deadline_rules: { serious_incident: { standard_days: 15, death_days: 10 } }, sort_order: 1, enabled: true, obligation_count: 9, instrument_status: null, verified_on: null,
    },
    {
        id: 'hipaa', tag_key: 'hipaa', title: 'HIPAA', kind: 'regulation', jurisdiction: 'US',
        reference: '45 CFR Parts 160, 164', catalog_version: 'placeholder', verified_against_instrument: true, offered: true, unavailable_reason: null,
        retention_floor_days: 2190, deadline_rules: {}, sort_order: 4, enabled: false, obligation_count: 0, instrument_status: null, verified_on: null,
    },
    {
        id: 'dora', tag_key: null, title: 'DORA', kind: 'regulation', jurisdiction: 'EU',
        reference: 'Regulation (EU) 2022/2554', catalog_version: 'placeholder', verified_against_instrument: false, offered: false, unavailable_reason: 'Not checked against its published text yet; not available until it has.',
        retention_floor_days: null, deadline_rules: {}, sort_order: 6, enabled: false, obligation_count: 0, instrument_status: null, verified_on: null,
    },
];

const CATALOG = {
    framework: {
        id: 'eu-ai-act', title: 'EU AI Act', kind: 'regulation', jurisdiction: 'EU', reference: 'Regulation (EU) 2024/1689',
        catalog_version: '2026-09', versions: [{ catalog_version: '2024-08', effective_from: '2024-08-01', effective_to: '2026-07-27' }, { catalog_version: '2026-09', effective_from: '2026-07-28' }],
        roles: ['provider', 'deployer'], classifications: ['minimal', 'limited', 'high'], deadline_rules: { serious_incident: { hours: 360 } },
        retention_floors: { high: 3650 }, verified_against_instrument: true, offered: true, unavailable_reason: null, obligation_count: 9, enabled: true, registered: true, not_legal_advice: 'n',
    },
    window: { from: '', to: '' }, evidence: { record_lag: {}, open_capture_gap_items: 0 }, timeline: [], systems: [],
};

const props = () => ({
    framework: 'eu-ai-act',
    frameworks: FRAMEWORKS.filter((f) => f.offered),
    registry: FRAMEWORKS,
    refreshFrameworks: vi.fn().mockResolvedValue(undefined),
    onTabChange: vi.fn(),
});

beforeEach(() => {
    vi.clearAllMocks();
    clearFrameworkCatalogCache();
    setComplianceFrameworkEnabled.mockResolvedValue({});
    getComplianceOverview.mockResolvedValue(CATALOG);
    getComplianceDashboard.mockResolvedValue({
        cards: [
            { framework: 'eu_ai_act', declared: true, total_tagged_rows: 1200, by_tag: { 'eu_ai_act.high': 1190, 'eu_ai_act.limited': 10 } },
            { framework: 'hipaa', declared: false, total_tagged_rows: 3, by_tag: { 'hipaa.phi': 3, 'hipaa.rg_marked': 0 } },
        ],
    });
    downloadComplianceExportCsv.mockResolvedValue(new Blob(['a,b']));
});

describe('FrameworksPage', () => {
    it('lists the registry with catalog, retention floor, deadline rules and obligation count', async () => {
        render(<FrameworksPage {...props()} />);
        const eu = screen.getByTestId('framework-row-eu-ai-act');
        expect(eu).toHaveTextContent('EU AI Act');
        expect(eu).toHaveTextContent('2026.09');
        expect(eu).toHaveTextContent('3650 days');
        expect(eu).toHaveTextContent('9');
        expect(screen.getByTestId('framework-deadlines-eu-ai-act')).toHaveTextContent('standard 15 d · death 10 d');
        expect(screen.queryByTestId('framework-unavailable-eu-ai-act')).toBeNull();
        expect(screen.getByTestId('framework-row-dora')).toHaveTextContent('none yet');
        // The selected framework's catalog: dated versions, floors, rules.
        await screen.findByTestId('framework-catalog-versions');
        expect(getComplianceOverview).toHaveBeenCalledWith('eu-ai-act');
        const [firstFrom, firstUntil, secondFrom] = ['2024-08-01', '2026-07-27', '2026-07-28'];
        expect(screen.getByTestId('framework-catalog-versions')).toHaveTextContent(`2024-08 · from ${firstFrom} until ${firstUntil}`);
        expect(screen.getByTestId('framework-catalog-versions')).toHaveTextContent(`2026-09 · from ${secondFrom} (in force)`);
        expect(screen.getByTestId('framework-catalog-floors')).toHaveTextContent('high: 3650 days');
        expect(screen.getByTestId('framework-catalog')).toHaveTextContent('serious incident 15 d — shown on incidents, never enforced');
    });

    it('lists a framework that is not offered with the reason, cannot switch it on, and skips its catalog panel', () => {
        render(<FrameworksPage {...props()} framework="dora" />);
        expect(screen.getByTestId('framework-unavailable-dora')).toHaveTextContent('Not checked against its published text yet');
        expect(screen.getByTestId('framework-toggle-dora')).toBeDisabled();
        expect(screen.getByTestId('framework-catalog-none')).toBeInTheDocument();
        expect(getComplianceOverview).not.toHaveBeenCalled();
    });

    it('toggles enablement through PATCH and refreshes the registry', async () => {
        const p = props();
        render(<FrameworksPage {...p} />);
        fireEvent.click(screen.getByTestId('framework-toggle-hipaa'));
        await waitFor(() => expect(setComplianceFrameworkEnabled).toHaveBeenCalledWith('hipaa', true));
        await waitFor(() => expect(p.refreshFrameworks).toHaveBeenCalled());
        expect(toast.success).toHaveBeenCalled();
    });

    it('reports a failed toggle', async () => {
        setComplianceFrameworkEnabled.mockRejectedValueOnce(new Error('nope'));
        render(<FrameworksPage {...props()} />);
        fireEvent.click(screen.getByTestId('framework-toggle-eu-ai-act'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Update failed: nope'));
    });

    it('renders the empty registry honestly and a catalog failure as an error', async () => {
        const { unmount } = render(<FrameworksPage {...props()} frameworks={[]} registry={[]} />);
        expect(screen.getByTestId('frameworks-empty')).toBeInTheDocument();
        unmount();

        getComplianceOverview.mockRejectedValueOnce(new Error('catalog down'));
        render(<FrameworksPage {...props()} />);
        expect(await screen.findByTestId('framework-catalog-error')).toHaveTextContent('catalog down');
    });
});

// A framework whose catalog carries classification along
// the value chain (EU AI Act) says so; others do not.
describe('FrameworksPage — composite classification', () => {
    it('notes it for a framework with composite_classification', async () => {
        getComplianceOverview.mockResolvedValue({ ...CATALOG, framework: { ...CATALOG.framework, composite_classification: true } });
        render(<FrameworksPage {...props()} />);
        expect(await screen.findByTestId('framework-composite-classification')).toHaveTextContent(
            'evaluated at its effective tier',
        );
    });

    it('shows no note for a framework without it', async () => {
        getComplianceOverview.mockResolvedValue({ ...CATALOG, framework: { ...CATALOG.framework, composite_classification: false } });
        render(<FrameworksPage {...props()} />);
        await screen.findByTestId('framework-catalog-versions');
        expect(screen.queryByTestId('framework-composite-classification')).toBeNull();
    });
});

// The tagging facts hang off the registry itself: one switch, every row —
// no second "Declared" checkbox, and not only the frameworks that tag.
describe('FrameworksPage — request-log tagging', () => {
    it('shows the tagged-row count per framework and says which frameworks do not tag', async () => {
        render(<FrameworksPage {...props()} />);
        await waitFor(() => expect(screen.getByTestId('framework-tagged-eu-ai-act')).toHaveTextContent('1,200'));
        expect(screen.getByTestId('framework-tagged-hipaa')).toHaveTextContent('3');
        expect(screen.getByTestId('framework-tagged-dora')).toHaveTextContent('not tagged');
        const [win] = getComplianceDashboard.mock.calls[0] as [{ from: string; to: string }];
        expect(Date.parse(win.to) - Date.parse(win.from)).toBe(30 * 86_400_000);
    });

    it('breaks the selected framework down by the tags the engine reports, with its guide', async () => {
        render(<FrameworksPage {...props()} />);
        await waitFor(() => expect(screen.getByTestId('framework-tagging-total')).toHaveTextContent('1,200'));
        const counts = screen.getByTestId('framework-tagging-counts');
        expect(counts).toHaveTextContent('eu_ai_act.high');
        expect(counts).toHaveTextContent('1,190');
        expect(screen.getByTestId('framework-tagging-guide')).toHaveTextContent('What gets tagged');
        expect(screen.getByTestId('framework-tagging-guide')).toHaveTextContent('Resource Groups → Governance → Compliance');
        // A management-system framework has no per-call export.
        expect(screen.getByTestId('framework-tagging-no-export')).toBeInTheDocument();
        expect(screen.queryByTestId('framework-tagging-export')).toBeNull();
        expect(screen.queryByTestId('framework-tagging-off')).toBeNull();
    });

    it('has exactly one enable switch per framework', async () => {
        render(<FrameworksPage {...props()} />);
        await screen.findByTestId('framework-tagging-total');
        expect(screen.getAllByRole('checkbox')).toHaveLength(FRAMEWORKS.length);
    });

    it('exports the auditor CSV for the window, and says when the framework is off', async () => {
        render(<FrameworksPage {...props()} framework="hipaa" />);
        await waitFor(() => expect(screen.getByTestId('framework-tagging-total')).toHaveTextContent('3'));
        expect(screen.getByTestId('framework-tagging-off')).toHaveTextContent('tagged while it was on');
        fireEvent.click(screen.getByTestId('framework-tagging-export'));
        await waitFor(() => expect(downloadBlob).toHaveBeenCalled());
        const [regime, win] = downloadComplianceExportCsv.mock.calls[0] as [string, { from: string; to: string }];
        expect(regime).toBe('hipaa/phi-access');
        expect(win).toEqual(getComplianceDashboard.mock.calls[0][0]);
        expect(String(downloadBlob.mock.calls[0][0])).toMatch(/^hipaa-\d{4}-\d\d-\d\d-\d{4}-\d\d-\d\d\.csv$/);
    });

    it('changing the window re-reads the counts', async () => {
        render(<FrameworksPage {...props()} />);
        await screen.findByTestId('framework-tagging-total');
        fireEvent.change(screen.getByTestId('tagging-window'), { target: { value: '90d' } });
        await waitFor(() => expect(getComplianceDashboard).toHaveBeenCalledTimes(2));
        const [win] = getComplianceDashboard.mock.calls[1] as [{ from: string; to: string }];
        expect(Date.parse(win.to) - Date.parse(win.from)).toBe(90 * 86_400_000);
    });

    it('a framework without a tag key says so and loads no breakdown', () => {
        render(<FrameworksPage {...props()} framework="dora" />);
        expect(screen.getByTestId('framework-tagging-none')).toBeInTheDocument();
        expect(screen.queryByTestId('tagging-window')).toBeNull();
    });

    it('a failed count load is an error, not a zero', async () => {
        getComplianceDashboard.mockRejectedValueOnce(new Error('dashboard down'));
        render(<FrameworksPage {...props()} />);
        expect(await screen.findByTestId('framework-tagging-error')).toHaveTextContent('dashboard down');
        expect(screen.getByTestId('framework-tagged-eu-ai-act')).toHaveTextContent('unavailable');
    });
});
