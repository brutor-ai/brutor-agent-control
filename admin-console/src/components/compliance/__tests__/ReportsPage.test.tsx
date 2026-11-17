/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Reports: the list per framework / scope / cadence, generation with its
 * seal and bundle error, and a report's rows grouped by basis — never
 * merged — with human agreement beside judged rows, not_covered apart,
 * stated limits, the catalog version, countersignature status, permalink
 * and the bundle download with the digest the CP states.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const listComplianceReports = vi.fn();
const getComplianceReport = vi.fn();
const generateComplianceReport = vi.fn();
const getComplianceReportBundle = vi.fn();
const fetchAISystems = vi.fn();

const gateway = {
    gatewayClient: { listComplianceReports, getComplianceReport, generateComplianceReport, getComplianceReportBundle },
    fetchAISystems,
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string }) => <div data-testid="record-dialog-stub">{p.recordId}</div>,
}));
vi.mock('../complianceFormat', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../complianceFormat')>();
    return { ...actual, downloadJson: vi.fn() };
});

import ReportsPage from '../pages/ReportsPage';
import { downloadJson } from '../complianceFormat';
import type { ComplianceFramework } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS = [{ id: 'eu-ai-act', title: 'EU AI Act', enabled: true }] as unknown as ComplianceFramework[];

const SUMMARY_ROW = {
    id: 'rep-1', tenant_id: 'default', framework_id: 'eu-ai-act', ai_system_id: null, cadence: 'monthly', period_start: '2026-08-01', period_end: '2026-08-31',
    catalog_version: '2026-09', status: 'ready', summary: {}, error: null, record_id: 'rec-rep-1', bundle_id: 'b-1', countersignatures: [],
    countersignature_status: 'awaiting_countersignature', permalink: '/v1/admin/compliance/reports/rep-1/bundle', generated_by: 'scheduler', created_at: '2026-09-01T02:00:00Z',
};
const DAILY_ROW = { ...SUMMARY_ROW, id: 'rep-2', cadence: 'daily', period_start: '2026-09-19', period_end: '2026-09-19', ai_system_id: 'sys-1', record_id: null, bundle_id: null, countersignature_status: 'none', permalink: null };

const REPORT = {
    ...SUMMARY_ROW,
    summary: {
        obligations_in_scope: 3, statements: 4, recomputed: { met: 1, not_met: 1 }, judged: { met: 1 }, indicator: {}, declared: { declared: 1 },
        not_covered: [{ ai_system_id: 'sys-1', obligation_key: 'art49' }], stated_limits: ['Organisational obligations are evidenced by pointer only.'], runs_sealed: 120, head_grade: 'witnessed',
    },
    rows: [
        { ai_system_id: 'sys-1', ai_system_name: 'Copilot', obligation_key: 'art12', obligation_title: 'Record-keeping', reference: 'Art 12', statement_key: 'logs', statement: 'Every governed action sealed', basis: 'recomputed', status: 'met', n: 120, m: 120, citations: ['rec-1'], catalog_version: '2026-09', obligation_limit: 'Completeness is a scope census, not a proof.' },
        { ai_system_id: 'sys-1', ai_system_name: 'Copilot', obligation_key: 'art50', obligation_title: 'Transparency', reference: 'Art 50', statement_key: 'notice.shown', statement: 'Notice shown', basis: 'recomputed', status: 'not_met', n: 40, m: 60 },
        { ai_system_id: 'sys-1', ai_system_name: 'Copilot', obligation_key: 'art50', obligation_title: 'Transparency', reference: 'Art 50', statement_key: 'notice.clear', statement: 'Notice text clear', basis: 'judged', status: 'met', n: 57, m: 60, human_agreement: { n: 34, observed_agreement: 0.92, ci95: [0.83, 0.97], blind: true } },
        { ai_system_id: 'sys-1', ai_system_name: 'Copilot', obligation_key: 'art17', obligation_title: 'QMS', reference: 'Art 17', statement_key: 'qms', statement: 'QMS declared', basis: 'declared', status: 'declared' },
        { ai_system_id: 'sys-1', ai_system_name: 'Copilot', obligation_key: 'art49', obligation_title: 'Registration', reference: 'Art 49', statement_key: null, basis: null, status: 'not_covered', detail: 'No evidence statement in the catalog for this obligation.' },
    ],
};

const props = () => ({ framework: 'eu-ai-act', frameworks: FRAMEWORKS, registry: FRAMEWORKS, refreshFrameworks: vi.fn(), onTabChange: vi.fn() });

beforeEach(() => {
    vi.clearAllMocks();
    listComplianceReports.mockResolvedValue({ items: [SUMMARY_ROW, DAILY_ROW], total: 2 });
    getComplianceReport.mockResolvedValue(REPORT);
    generateComplianceReport.mockResolvedValue({ report: { ...REPORT, id: 'rep-3', period_start: '2026-09-01', period_end: '2026-09-30' }, created: true, regenerated: false, seal: { sealed: true, record_id: 'rec-rep-3' }, bundle_error: null });
    getComplianceReportBundle.mockResolvedValue({ bundle: { type: 'ai.brutor/evidence-bundle/v1' }, digest: 'd'.repeat(64) });
    fetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'copilot', display_name: 'Copilot', governance_gaps: [] }], total: 1 });
});

describe('ReportsPage', () => {
    it('lists reports for the framework with status, catalog, seal and countersignature; filters by scope and cadence', async () => {
        render(<ReportsPage {...props()} />);
        await screen.findByTestId('report-row-rep-1');
        expect(listComplianceReports).toHaveBeenCalledWith({ framework: 'eu-ai-act', ai_system_id: undefined, cadence: undefined, limit: 200 });
        const row = screen.getByTestId('report-row-rep-1');
        expect(row).toHaveTextContent('2026-08-01 – 2026-08-31');
        expect(row).toHaveTextContent('estate');
        expect(row).toHaveTextContent('2026-09');
        expect(row).toHaveTextContent('awaiting countersignature');
        expect(screen.getByTestId('report-row-rep-2')).toHaveTextContent('not sealed');
        expect(screen.getByTestId('report-row-rep-2')).toHaveTextContent('no bundle');
        await waitFor(() => expect(screen.getByTestId('report-row-rep-2')).toHaveTextContent('Copilot'));
        fireEvent.change(screen.getByTestId('reports-scope'), { target: { value: 'estate' } });
        await waitFor(() => expect(listComplianceReports).toHaveBeenLastCalledWith(expect.objectContaining({ ai_system_id: 'estate' })));
        fireEvent.change(screen.getByTestId('reports-cadence'), { target: { value: 'weekly' } });
        await waitFor(() => expect(listComplianceReports).toHaveBeenLastCalledWith(expect.objectContaining({ cadence: 'weekly' })));
    });

    it('generates a report with the chosen cadence, scope, period and force, and shows its seal', async () => {
        render(<ReportsPage {...props()} />);
        await screen.findByTestId('report-row-rep-1');
        fireEvent.change(screen.getByTestId('gen-cadence'), { target: { value: 'monthly' } });
        fireEvent.change(await screen.findByTestId('gen-system'), { target: { value: 'sys-1' } });
        fireEvent.change(screen.getByTestId('gen-period'), { target: { value: '2026-09-15' } });
        fireEvent.click(screen.getByTestId('gen-force'));
        fireEvent.click(screen.getByTestId('gen-run'));
        await screen.findByTestId('gen-result');
        expect(generateComplianceReport).toHaveBeenCalledWith({ framework_id: 'eu-ai-act', cadence: 'monthly', ai_system_id: 'sys-1', period_start: '2026-09-15', force: true });
        const [periodStart, periodEnd] = ['2026-09-01', '2026-09-30'];
        expect(screen.getByTestId('gen-result')).toHaveTextContent(`Generated: monthly ${periodStart} – ${periodEnd} · status ready · catalog 2026-09`);
        expect(screen.getByTestId('gen-seal')).toHaveTextContent('sealed as record');
        // The generated report opens.
        await waitFor(() => expect(getComplianceReport).toHaveBeenCalledWith('rep-3'));
        await waitFor(() => expect(listComplianceReports).toHaveBeenCalledTimes(2));
    });

    it('shows an unsealed generation and a bundle error honestly', async () => {
        generateComplianceReport.mockResolvedValueOnce({
            report: { ...REPORT, id: 'rep-4', record_id: null, bundle_id: null }, created: true, regenerated: false,
            seal: { sealed: false, seal_error: 'evidence_disabled' }, bundle_error: 'core unreachable',
        });
        render(<ReportsPage {...props()} />);
        await screen.findByTestId('report-row-rep-1');
        fireEvent.click(screen.getByTestId('gen-run'));
        await screen.findByTestId('gen-result');
        expect(screen.getByTestId('gen-seal')).toHaveTextContent('saved but not sealed (evidence_disabled)');
        expect(screen.getByTestId('gen-bundle-error')).toHaveTextContent('Bundle not built: core unreachable');

        generateComplianceReport.mockRejectedValueOnce(new Error('422 no catalog'));
        fireEvent.click(screen.getByTestId('gen-run'));
        expect(await screen.findByTestId('gen-error')).toHaveTextContent('422 no catalog');
    });

    it('opens a report: rows grouped by basis, agreement beside judged rows, not covered apart, limits, catalog, permalink', async () => {
        render(<ReportsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('report-row-rep-1'));
        await screen.findByTestId('report-detail');
        expect(getComplianceReport).toHaveBeenCalledWith('rep-1');
        const detail = screen.getByTestId('report-detail');
        expect(detail).toHaveTextContent('catalog 2026-09');
        expect(detail).toHaveTextContent('Not legal advice');
        expect(screen.getByTestId('report-countersignature')).toHaveTextContent('awaiting countersignature');
        expect(screen.getByTestId('report-permalink')).toHaveTextContent('/v1/admin/compliance/reports/rep-1/bundle');
        expect(screen.getByTestId('report-facts')).toHaveTextContent('120');
        expect(screen.getByTestId('report-facts')).toHaveTextContent('witnessed');
        expect(screen.getByTestId('report-basis-counts')).toHaveTextContent('recomputed met 1 · not met 1');
        expect(screen.getByTestId('report-basis-counts')).toHaveTextContent('not covered 1');

        const recomputed = screen.getByTestId('report-group-recomputed');
        expect(within(recomputed).getAllByRole('listitem')).toHaveLength(2);
        expect(recomputed).toHaveTextContent('Obligation limit: Completeness is a scope census, not a proof.');
        const judged = screen.getByTestId('report-group-judged');
        expect(within(judged).getAllByRole('listitem')).toHaveLength(1);
        expect(judged).toHaveTextContent('JUDGED · met 57 of 60 · human agreement 0.92 [0.83–0.97], n = 34, blind');
        expect(screen.getByTestId('report-group-declared')).toHaveTextContent('QMS declared');
        expect(screen.getByTestId('report-not-covered-sys-1-art49')).toHaveTextContent('Copilot · Registration Art 49 — No evidence statement in the catalog');
        expect(screen.getByTestId('report-limits')).toHaveTextContent('Organisational obligations are evidenced by pointer only.');
        expect(detail.textContent).not.toMatch(/\d+ ?%/);

        fireEvent.click(within(recomputed).getByTestId('record-link-rec-1'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-1');
    });

    it('downloads the bundle and shows the digest the control plane stated', async () => {
        render(<ReportsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('report-row-rep-1'));
        fireEvent.click(await screen.findByTestId('report-bundle-download'));
        await screen.findByTestId('report-bundle-note');
        expect(getComplianceReportBundle).toHaveBeenCalledWith('rep-1');
        expect(downloadJson).toHaveBeenCalledWith({ type: 'ai.brutor/evidence-bundle/v1' }, 'evidence-report-rep-1.json');
        expect(screen.getByTestId('report-bundle-note')).toHaveTextContent(`digest ${'d'.repeat(64)}`);
        expect(screen.getByTestId('report-bundle-note')).toHaveTextContent('Verify offline');
    });

    it('a daily report has no bundle and says so; empty and error states', async () => {
        getComplianceReport.mockResolvedValueOnce({ ...DAILY_ROW, rows: [] });
        render(<ReportsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('report-row-rep-2'));
        expect(await screen.findByTestId('report-no-bundle')).toHaveTextContent('bundles are built for monthly reports');
        expect(screen.getByTestId('report-no-rows')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('report-close'));
        expect(screen.queryByTestId('report-detail')).toBeNull();

        listComplianceReports.mockResolvedValueOnce({ items: [], total: 0 });
        fireEvent.change(screen.getByTestId('reports-cadence'), { target: { value: 'daily' } });
        expect(await screen.findByTestId('reports-empty')).toHaveTextContent('No Evidence Report for EU AI Act yet');

        listComplianceReports.mockRejectedValueOnce(new Error('reports down'));
        fireEvent.change(screen.getByTestId('reports-cadence'), { target: { value: 'weekly' } });
        expect(await screen.findByTestId('reports-error')).toHaveTextContent('reports down');
    });
});
