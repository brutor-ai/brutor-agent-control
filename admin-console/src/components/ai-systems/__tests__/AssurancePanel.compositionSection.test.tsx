/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Assurance report's "Composition and dependencies" section in the report view: each edge with role, status
 * (tested vs assumed — never a tested status for an external agent),
 * contract, supplier and supplier-information gap; separability claims;
 * callers with enforcement; open findings. A report without the section
 * shows none.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const mockHealth = vi.fn();
const mockReport = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    captureAssuranceReport: vi.fn(),
    fetchAssuranceHistory: vi.fn().mockResolvedValue({ reports: [], total: 0 }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchSystemHealth: mockHealth,
        fetchAssuranceReport: mockReport,
        captureAssuranceReport: stableGateway.captureAssuranceReport,
        fetchAssuranceHistory: stableGateway.fetchAssuranceHistory,
    }),
}));

import AssurancePanel from '../AssurancePanel';
import { COMPOSITION_SECTION } from './fixtures/compositionReportSection';
import { cappedRootHealth } from './fixtures/dependencyHealthPayloads';
import { callsWhileIdle } from '../../../test/idle';

beforeEach(() => {
    vi.clearAllMocks();
    mockHealth.mockResolvedValue(cappedRootHealth());
});

async function renderEvidence(report: Record<string, unknown>) {
    mockReport.mockResolvedValue(report);
    render(<AssurancePanel aiSystemId="sys-demo" initialSubTab="evidence" />);
    await screen.findByTestId('health-block');
}

describe('report — Composition and dependencies', () => {
    it('renders every edge with its status, contract and supplier', async () => {
        await renderEvidence({ not_covered: [], composition: COMPOSITION_SECTION });
        const section = screen.getByTestId('report-composition');
        expect(section).toHaveTextContent('Composition and dependencies');
        expect(within(section).getByTestId('report-composition-tier')).toHaveTextContent('limited → high via Lending Decision Engine');
        expect(within(section).getByTestId('report-composition-enforcement')).toHaveTextContent('enforced');

        const fraud = within(section).getByTestId('report-edge-dep-fraud');
        expect(fraud).toHaveTextContent('tested · Degraded (failing on reliability) · caps this system');
        expect(fraud).toHaveTextContent('pinned v2 · abcdef01');

        const bureau = within(section).getByTestId('report-edge-dep-bureau');
        expect(within(bureau).getByTestId('report-edge-status-dep-bureau')).toHaveTextContent(/^assumed, not tested/);
        expect(bureau).toHaveTextContent('task states: 12 completed, 2 failed, 1 input required');
        expect(within(bureau).getByTestId('report-edge-supplier-dep-bureau')).toHaveTextContent(
            'Nordic Credit Bureau AB · supplier information on file (1)',
        );

        const address = within(section).getByTestId('report-edge-dep-address');
        const sealedOn = '2026-09-21';
        expect(address).toHaveTextContent(`separable · sealed ${sealedOn} as rec-sep-address-`);
        expect(within(address).getByTestId('report-edge-supplier-dep-address')).toHaveTextContent(
            'Group Address Services Ltd · supplier information MISSING',
        );
        expect(within(address).getByTestId('report-edge-supplier-dep-address').className).toContain('text-amber-800');
    });

    it('lists separability claims, callers with enforcement, and open findings', async () => {
        await renderEvidence({ not_covered: [], composition: COMPOSITION_SECTION });
        expect(screen.getByTestId('report-composition-separability')).toHaveTextContent(
            'Declares Address Validator separable — sealed as rec-sep-address-0001',
        );
        const callers = screen.getByTestId('report-composition-callers');
        expect(callers).toHaveTextContent('Broker Portal Assistant — accepted · 40 calls');
        expect(callers).toHaveTextContent('Shadow Batch Job — NOT accepted (refused at the gateway) · 2 calls');
        expect(screen.getByTestId('report-composition-findings')).toHaveTextContent(
            'Unaccepted caller: Enforced: Shadow Batch Job is not an accepted caller; 2 calls refused.',
        );
    });

    it('a report without the composition section shows none', async () => {
        await renderEvidence({ not_covered: [] });
        expect(screen.queryByTestId('report-composition')).toBeNull();
    });
});

// Report spec 1.7 — member agents' releases in the section.
describe('report — member agent releases', () => {
    const AGENTS = [
        {
            id: 'agent-1', name: 'Screening', implementation_name: 'screening-agent',
            approved_versions: ['1.4.*'], require_release: false,
            release: { name: 'screening-agent', version: '1.4.2', build: null, trust: 'verified' as const, approved: true, last_seen_at: null },
        },
        { id: 'agent-2', name: 'Fraud', implementation_name: null, approved_versions: [], require_release: false, release: null },
    ];

    it('lists each member agent’s release with trust and approval, and the agent findings', async () => {
        await renderEvidence({
            not_covered: [],
            composition: {
                ...COMPOSITION_SECTION,
                agents: AGENTS,
                agent_findings: [
                    { code: 'agent.undeclared_change', severity: 'high', agent_id: 'agent-1', agent_name: 'Screening', message: 'Screening still declares release screening-agent@1.4.2 …' },
                ],
            },
        });
        const block = screen.getByTestId('report-composition-agents');
        const screening = within(block).getByTestId('report-agent-agent-1');
        expect(screening).toHaveTextContent('screening-agent@1.4.2');
        expect(within(screening).getByTestId('trust-badge')).toHaveAttribute('data-trust', 'verified');
        expect(within(screening).getByTestId('approval-badge')).toHaveAttribute('data-approval', 'approved');
        expect(screening).toHaveAttribute('aria-label', 'Screening: screening-agent@1.4.2 · verified · approved');
        expect(within(block).getByTestId('report-agent-agent-2')).toHaveTextContent('no release declared');
        expect(within(block).getByTestId('report-agent-findings')).toHaveTextContent(
            'Undeclared implementation change: Screening still declares release',
        );
    });

    it('a section that names no agents shows no agent releases', async () => {
        await renderEvidence({ not_covered: [], composition: COMPOSITION_SECTION });
        expect(screen.getByTestId('report-composition')).toBeInTheDocument();
        expect(screen.queryByTestId('report-composition-agents')).toBeNull();
    });
});

describe('AssurancePanel — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        await renderEvidence({ not_covered: [], composition: COMPOSITION_SECTION });
        expect(await callsWhileIdle([mockHealth, mockReport])).toBe(0);
    });
});
