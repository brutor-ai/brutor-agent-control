/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Assurance report (spec 1.8): the "Excluded from
 * judgement" section lists every incident window (who, why, runs excluded,
 * sealed record) and every baseline epoch, coverage states the excluded
 * period, a baseline an accepted change reset says it is re-learning — and
 * a report without the section shows nothing invented.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const mockHealth = vi.fn();
const mockReport = vi.fn();
const mockHistory = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchSystemHealth: mockHealth,
        fetchAssuranceReport: mockReport,
        captureAssuranceReport: vi.fn(),
        fetchAssuranceHistory: mockHistory,
    }),
}));

import AssurancePanel from '../AssurancePanel';

const STATEMENT =
    '1 declared incident window(s) covering 9.67 h of this period; 14 run(s) inside them were recorded and sealed but excluded from judgement — that period is unmeasured, not passed.';

const REPORT = {
    not_covered: [STATEMENT],
    baselines: {
        ready: 1,
        learning: 1,
        metrics: [
            { metric_key: 'action_count', basis: 'runs', state: 'learning', sample_count: 12, median: 3, mad: 1, p90: 5, distribution: null },
            { metric_key: 'cost_per_run', basis: 'runs', state: 'ready', sample_count: 90, median: 0.2, mad: 0.01, p90: 0.3, distribution: null },
        ],
    },
    judgement_exclusions: {
        incident_windows: [
            {
                id: 'iw-1',
                ai_system_id: null,
                tenant_wide: true,
                starts_at: '2026-09-27T21:00:00+00:00',
                ends_at: '2026-09-28T06:40:00+00:00',
                cause_category: 'provider_outage',
                reason: 'provider credits ran out',
                drift_event_id: 'drift-2',
                declared_by: 'alice',
                declared_at: '2026-09-28T06:45:00+00:00',
                record_id: 'rec-9',
                runs_excluded: 14,
            },
        ],
        baseline_epochs: [
            {
                id: 'ep-1',
                ai_system_id: 'sys-1',
                metric_key: 'action_count',
                basis: 'runs',
                caller_system_id: 'all',
                starts_at: '2026-09-26T06:06:00+00:00',
                kind: 'accepted_change',
                drift_event_id: 'drift-1',
                reason: 'the demo retune',
                declared_by: 'bob',
                record_id: null,
            },
        ],
        excluded_hours: 9.67,
        excluded_runs: 14,
    },
    coverage: {
        basis: 'x',
        coverage_pct: 1,
        statement: 's',
        excluded_period: { incident_windows: 1, excluded_hours: 9.67, excluded_runs: 14, statement: STATEMENT },
    },
};

beforeEach(() => {
    vi.clearAllMocks();
    mockHealth.mockResolvedValue({
        ai_system_id: 'sys-1', health_state: 'healthy', runs_24h: 3, open_drift_events: 0, reasons: [], is_trustworthy: true,
    });
    mockHistory.mockResolvedValue({ reports: [], total: 0 });
});

describe('Excluded from judgement', () => {
    it('lists every window with who, why, runs excluded and the sealed record, and the coverage statement', async () => {
        mockReport.mockResolvedValue(REPORT);
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const section = await screen.findByTestId('report-judgement-exclusions');
        expect(within(section).getByTestId('report-excluded-period')).toHaveTextContent('unmeasured, not passed');
        const w = within(section).getByTestId('report-iw-iw-1');
        expect(w).toHaveTextContent('Provider outage');
        expect(w).toHaveTextContent('Every AI System in the tenant');
        expect(w).toHaveTextContent('provider credits ran out');
        expect(w).toHaveTextContent('by alice');
        expect(w).toHaveTextContent('14 run(s) excluded');
        expect(w).toHaveTextContent('sealed record rec-9');
        const e = within(section).getByTestId('report-epoch-ep-1');
        expect(e).toHaveTextContent('accepted change by bob');
        expect(e).toHaveTextContent('not sealed');
    });

    it('states the approval holds raised inside the windows (spec 1.10) and none when absent', async () => {
        mockReport.mockResolvedValue({
            ...REPORT,
            judgement_exclusions: { ...REPORT.judgement_exclusions, excluded_approval_requests: 250 },
        });
        const { unmount } = render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const holds = await screen.findByTestId('report-excluded-holds');
        expect(holds).toHaveTextContent('250 approval request(s) raised inside the windows: recorded, not judged by the oversight rate.');
        unmount();
        mockReport.mockResolvedValue(REPORT);
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('report-judgement-exclusions');
        expect(screen.queryByTestId('report-excluded-holds')).toBeNull();
    });

    it('a baseline an accepted change reset says it is re-learning; its state stays its own', async () => {
        mockReport.mockResolvedValue(REPORT);
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const row = await screen.findByTestId('baseline-action_count');
        expect(within(row).getByTestId('baseline-epoch-action_count')).toHaveTextContent(
            `re-learning since ${new Date('2026-09-26T06:06:00+00:00').toLocaleString()} — accepted change by bob`,
        );
        expect(row).toHaveTextContent('learning');
        expect(within(screen.getByTestId('baseline-cost_per_run')).queryByTestId('baseline-epoch-cost_per_run')).toBeNull();
    });

    it('a report without the section shows none: the report schema makes it optional', async () => {
        const { judgement_exclusions: _j, coverage: _c, ...without } = REPORT;
        mockReport.mockResolvedValue(without);
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('baselines-block');
        expect(screen.queryByTestId('report-judgement-exclusions')).toBeNull();
        expect(screen.queryByTestId('baseline-epoch-action_count')).toBeNull();
    });
});
