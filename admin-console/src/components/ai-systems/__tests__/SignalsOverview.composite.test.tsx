/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Signals → Overview for a system with dependencies:
 *
 * * a critical dependency that is not healthy caps the verdict — the banner
 *   names the dependency and its failing signal and links to it, and no
 *   signal of THIS system is badged as driving the verdict;
 * * dependency health keeps tested and assumed visibly apart (an external
 *   agent never gets a status chip), with note, fallback check, boundary
 *   metrics and assumptions;
 * * a shared service states its basis ("assured from N served calls"),
 *   renders oversight as not applicable — never 0 % — and shows an
 *   advisory signal's score with its marker.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const fetchSystemHealth = vi.fn();
const fetchAssuranceHistory = vi.fn();
const fetchAssuranceReport = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ fetchSystemHealth, fetchAssuranceHistory, fetchAssuranceReport }),
}));

import SignalsOverview from '../SignalsOverview';
import { SERVICE_FACTS, cappedRootHealth, serviceHealth } from './fixtures/dependencyHealthPayloads';

const setActiveTab = vi.fn();

beforeEach(() => {
    vi.clearAllMocks();
    fetchAssuranceHistory.mockResolvedValue({ reports: [], total: 0 });
    fetchAssuranceReport.mockResolvedValue({ service: SERVICE_FACTS });
});

async function renderOverview() {
    render(<SignalsOverview aiSystemId="sys-demo" setActiveTab={setActiveTab} />);
    return screen.findByTestId('signals-overview');
}

describe('dependency cap', () => {
    it('names the capping dependency and its failing signal, and links to it', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        const banner = screen.getByTestId('dependency-cap-banner');
        expect(banner).toHaveTextContent('critical dependency Scoring Service is not healthy (failing on liveness)');
        fireEvent.click(within(banner).getByTestId('dependency-cap-link'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-scoring:signals');
    });

    it('does not blame one of the system’s own signals', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        expect(screen.getByTestId('overview-driver')).toHaveTextContent('A dependency drives the verdict');
        expect(screen.getByTestId('overview-driver')).not.toHaveTextContent(/Reliability drives/);
        expect(screen.queryByTestId('overview-drives-reliability')).toBeNull();
        expect(screen.queryByTestId('overview-no-driver')).toBeNull();
    });

    it('shows no banner when nothing caps the verdict', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth({ health_state: 'healthy', dependency_cap: null }));
        await renderOverview();
        expect(screen.queryByTestId('dependency-cap-banner')).toBeNull();
    });
});

describe('dependency health', () => {
    it('keeps tested and assumed distinct', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        const tested = screen.getByTestId('dependency-health-dep-scoring');
        const assumed = screen.getByTestId('dependency-health-dep-bureau');
        expect(tested).toHaveAttribute('data-basis', 'tested');
        expect(within(tested).getByTestId('dependency-status-dep-scoring')).toHaveTextContent('Degraded');
        expect(within(tested).getByTestId('dependency-failing-dep-scoring')).toHaveTextContent(
            'failing on liveness — caps this system at degraded',
        );
        // External: no status chip at all — only "assumed, not tested".
        expect(assumed).toHaveAttribute('data-basis', 'assumed');
        expect(within(assumed).queryByTestId('dependency-status-dep-bureau')).toBeNull();
        expect(within(assumed).getByTestId('dependency-basis-dep-bureau')).toHaveTextContent('Assumed, not tested');
        expect(assumed.className).toContain('border-dashed');
        expect(tested.className).not.toContain('border-dashed');
    });

    it('shows the external agent’s boundary metrics and declared assumptions', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        expect(screen.getByTestId('dependency-boundary-dep-bureau')).toHaveTextContent('20 calls · 2 errors (10%) · p95 840 ms');
        const assumptions = screen.getByTestId('dependency-assumptions-dep-bureau');
        expect(assumptions).toHaveTextContent('Expected availability');
        expect(assumptions).toHaveTextContent('99.5%');
        expect(assumptions).toHaveTextContent('Data classes shared');
        expect(assumptions).toHaveTextContent('personal_data');
    });

    it('shows a degradable dependency’s note and a fallback that did not hold', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        expect(screen.getByTestId('dependency-note-dep-address')).toHaveTextContent('declared fallback');
        const fallback = screen.getByTestId('dependency-fallback-dep-address');
        expect(fallback).toHaveAttribute('data-held', 'false');
        expect(fallback).toHaveTextContent('Fallback did NOT hold: 1 of 3');
        expect(screen.getByTestId('dependency-failing-dep-address')).toHaveTextContent('via Geo Lookup');
    });

    it('links each dependency to where it is worked', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        fireEvent.click(screen.getByTestId('dependency-health-link-dep-address'));
        expect(setActiveTab).toHaveBeenLastCalledWith('ai-systems:sys-address:signals');
        fireEvent.click(screen.getByTestId('dependency-health-link-dep-bureau'));
        expect(setActiveTab).toHaveBeenLastCalledWith('a2a-agent-cards');
    });

    it('renders no section for a system without dependencies', async () => {
        fetchSystemHealth.mockResolvedValue(serviceHealth());
        await renderOverview();
        expect(screen.queryByTestId('dependency-health')).toBeNull();
    });
});

describe('a shared service', () => {
    it('says it is assured from the calls it served, with the count', async () => {
        fetchSystemHealth.mockResolvedValue(serviceHealth());
        await renderOverview();
        expect(await screen.findByText(/Assured from 128 served calls as a shared service/)).toBeInTheDocument();
        expect(fetchAssuranceReport).toHaveBeenCalledWith('sys-demo');
    });

    it('never reads the report for a system on its own runs', async () => {
        fetchSystemHealth.mockResolvedValue(cappedRootHealth());
        await renderOverview();
        expect(screen.queryByTestId('signal-basis-note')).toBeNull();
        expect(fetchAssuranceReport).not.toHaveBeenCalled();
    });

    it('renders oversight as not applicable, never 0 %', async () => {
        fetchSystemHealth.mockResolvedValue(serviceHealth());
        await renderOverview();
        expect(screen.getByTestId('overview-signal-score-oversight')).toHaveTextContent('n/a');
        expect(screen.getByTestId('overview-signal-row-oversight')).not.toHaveTextContent('0%');
        expect(screen.getByTestId('overview-signal-standing-oversight')).toHaveTextContent(
            'Not applicable: raised no approval holds',
        );
    });

    it('shows an advisory signal with its score and the advisory marker', async () => {
        fetchSystemHealth.mockResolvedValue(serviceHealth());
        await renderOverview();
        expect(screen.getByTestId('overview-signal-score-liveness')).toHaveTextContent('75%');
        expect(screen.getByTestId('overview-signal-advisory-liveness')).toHaveTextContent('advisory');
        // Advisory: shown, but never the verdict's driver.
        expect(screen.queryByTestId('overview-drives-liveness')).toBeNull();
    });
});
