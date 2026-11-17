/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Assurance report of a system with dependencies:
 *
 * * the verdict names the critical dependencies that are not healthy, each
 *   linked to its own signals;
 * * the evidence carries the report's dependency section (tested vs
 *   assumed), and a shared service's served-call facts and basis;
 * * baselines learned on served calls say so ("per served call", "N served
 *   calls") and per-caller sub-baselines are counted;
 * * oversight on a shared service that raised no hold is "n/a", never 0 %.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
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
import {
    DEPENDENCY_HEALTH,
    SERVICE_FACTS,
    cappedRootHealth,
    serviceHealth,
} from './fixtures/dependencyHealthPayloads';

const setActiveTab = vi.fn();

const ROOT_REPORT = {
    not_covered: [],
    signal_basis: 'runs',
    service: null,
    dependency_health: DEPENDENCY_HEALTH,
    verdict: {
        status: 'assured_with_exceptions',
        conclusion: 'Assured with exceptions: critical dependency not healthy: Scoring Service.',
        basis: 'signed_envelope',
        envelope_terms_breached: 0,
        envelope_terms_insufficient: 0,
        exceptions_total: 0,
        open_drift_findings: 0,
        failing_dependencies: ['Scoring Service'],
    },
};

const SERVICE_REPORT = {
    not_covered: [],
    signal_basis: 'segments',
    service: SERVICE_FACTS,
    dependency_health: [],
    baselines: {
        ready: 2,
        learning: 0,
        metrics: [
            { metric_key: 'action_count', basis: 'segments', state: 'ready', sample_count: 128, median: 4, mad: 1, p90: 7, distribution: null },
            { metric_key: 'action_count', basis: 'runs', state: 'ready', sample_count: 60, median: 9, mad: 2, p90: 14, distribution: null },
        ],
        sub_baselines: { seasonal: 0, per_actor: 0, per_caller: 2 },
    },
};

beforeEach(() => {
    vi.clearAllMocks();
    mockHistory.mockResolvedValue({ reports: [], total: 0 });
});

describe('verdict', () => {
    it('names the failing critical dependency and links to it', async () => {
        mockHealth.mockResolvedValue(cappedRootHealth());
        mockReport.mockResolvedValue(ROOT_REPORT);
        render(<AssurancePanel aiSystemId="sys-demo" setActiveTab={setActiveTab} />);
        const failing = await screen.findByTestId('verdict-failing-dependencies');
        expect(failing).toHaveTextContent('Critical dependencies not healthy: Scoring Service');
        fireEvent.click(within(failing).getByRole('button', { name: 'Scoring Service' }));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-scoring:signals');
    });

    it('says nothing about dependencies on a report whose critical dependencies were all healthy', async () => {
        mockHealth.mockResolvedValue(cappedRootHealth({ dependency_cap: null, health_state: 'healthy' }));
        mockReport.mockResolvedValue({ not_covered: [], verdict: { ...ROOT_REPORT.verdict, failing_dependencies: [] } });
        render(<AssurancePanel aiSystemId="sys-demo" />);
        await screen.findByTestId('verdict-banner');
        expect(screen.queryByTestId('verdict-failing-dependencies')).toBeNull();
    });
});

describe('evidence', () => {
    it('shows the cap and the report’s dependency section, tested vs assumed', async () => {
        mockHealth.mockResolvedValue(cappedRootHealth());
        mockReport.mockResolvedValue(ROOT_REPORT);
        render(<AssurancePanel aiSystemId="sys-demo" initialSubTab="evidence" setActiveTab={setActiveTab} />);
        const block = await screen.findByTestId('health-block');
        expect(within(block).getByTestId('dependency-cap-banner')).toHaveTextContent('Scoring Service');
        const section = screen.getByTestId('report-dependency-health');
        expect(within(section).getByTestId('dependency-health-dep-scoring')).toHaveAttribute('data-basis', 'tested');
        expect(within(section).getByTestId('dependency-health-dep-bureau')).toHaveAttribute('data-basis', 'assumed');
        expect(within(section).queryByTestId('dependency-status-dep-bureau')).toBeNull();
    });

    it('states a shared service’s basis and what it served', async () => {
        mockHealth.mockResolvedValue(serviceHealth());
        mockReport.mockResolvedValue(SERVICE_REPORT);
        render(<AssurancePanel aiSystemId="sys-scoring" initialSubTab="evidence" />);
        const block = await screen.findByTestId('health-block');
        expect(within(block).getByTestId('signal-basis-note')).toHaveTextContent('Assured from 128 served calls');
        expect(screen.getByTestId('service-served-calls')).toHaveTextContent('128');
        expect(screen.getByTestId('service-outcomes')).toHaveTextContent('120 answered · 5 failed · 1 rejected · 2 timed out');
        expect(screen.queryByTestId('report-dependency-health')).toBeNull();
    });

    it('renders oversight as n/a and advisory liveness with its score', async () => {
        mockHealth.mockResolvedValue(serviceHealth());
        mockReport.mockResolvedValue(SERVICE_REPORT);
        render(<AssurancePanel aiSystemId="sys-scoring" initialSubTab="evidence" />);
        const oversight = await screen.findByTestId('component-oversight_score');
        expect(oversight).toHaveTextContent('n/a');
        expect(oversight).not.toHaveTextContent('0%');
        expect(oversight).toHaveAttribute('title', 'Not applicable: raised no approval holds');
        expect(screen.getByTestId('component-liveness_score')).toHaveTextContent('75%');
        expect(screen.getByTestId('component-liveness_score-advisory')).toBeInTheDocument();
    });

    it('keeps served-call and own-run baselines apart and counts per-caller rows', async () => {
        mockHealth.mockResolvedValue(serviceHealth());
        mockReport.mockResolvedValue(SERVICE_REPORT);
        render(<AssurancePanel aiSystemId="sys-scoring" initialSubTab="evidence" />);
        const segments = await screen.findByTestId('baseline-segments-action_count');
        expect(segments).toHaveTextContent('Actions per served call');
        expect(segments).toHaveTextContent('128 served calls');
        const runs = screen.getByTestId('baseline-action_count');
        expect(runs).toHaveTextContent('Actions per run');
        expect(runs).toHaveTextContent('60 runs');
        expect(screen.getByTestId('sub-baselines-per-caller')).toHaveTextContent('2 per-caller sub-baseline(s)');
    });
});
