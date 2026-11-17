/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for AISystemsFleetTab.
 *
 * Coverage:
 *   - Mount fetches the fleet and renders one row per AI System
 *   - Governance gaps render as chips; a fully documented system
 *     renders the "Documented" badge instead
 *   - Unset governance attributes render an explicit "not set"
 *     rather than an empty cell — the board's whole job before the
 *     run ledger exists is showing what is missing
 *   - Filters are passed through to the API
 *   - The empty state distinguishes "nothing here" from "nothing
 *     matches your filters"
 *   - API failure surfaces an error instead of an empty table that
 *     would read as "you have no AI Systems"
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetchAISystems = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ fetchAISystems: mockFetchAISystems }),
}));

// The drilldown embeds the full runs tab, which fans out its own
// fetches — stub it and assert on the props the deep-link must deliver.
//
// The stub RENDERS `identity`, because the real component does: the
// breadcrumb and system name are passed down so they can share a row with
// the status verdicts. A stub that swallowed the slot would make the system
// name disappear from every drilldown assertion in this file.
vi.mock('../AISystemRunsTab', () => ({
    default: ({
        aiSystemId,
        initialSection,
        identity,
    }: {
        aiSystemId: string;
        initialSection?: string;
        identity?: React.ReactNode;
    }) => (
        <div data-testid="runs-tab-stub" data-system={aiSystemId} data-section={initialSection ?? 'signals'}>
            {identity}
        </div>
    ),
}));

import AISystemsFleetTab from '../AISystemsFleetTab';
import type { AISystemCardFacts, AISystemSummary } from '../../../lib/aiSystemTypes';
import { SystemKind } from '../../../lib/groupTypes';

const documentedSystem: AISystemSummary = {
    id: 'group-refund',
    name: 'refund-agent',
    display_name: 'Refund Agent',
    system_kind: SystemKind.AGENT,
    owner: 'payments-team',
    intended_use: 'Issues customer refunds under policy',
    intended_client_labels: ['Internal Portal'],
    lifecycle_stage: 'active',
    eu_ai_act_risk_tier: 'high',
    hierarchy_level: 1,
    hierarchy_path: '/acme/payments/refund-agent',
    is_active: true,
    composition: {
        members: { agents: 4, models: 2, mcp_servers: 3, skills: 1 },
        depends_on: 0,
        worst_dependency_health: null,
        used_by_declared: 0,
        used_by_observed: 0,
        idle: false,
        child_groups: 0,
    },
    role: 'root',
    governance_gaps: [],
};

const undocumentedSystem: AISystemSummary = {
    id: 'group-nightly',
    name: 'nightly-sync',
    display_name: 'Nightly Sync',
    system_kind: SystemKind.INTEGRATION,
    owner: null,
    intended_use: null,
    lifecycle_stage: null,
    eu_ai_act_risk_tier: null,
    hierarchy_level: 1,
    hierarchy_path: '/acme/nightly-sync',
    is_active: true,
    composition: {
        members: { agents: 0, models: 1, mcp_servers: 0, skills: 0 },
        depends_on: 0,
        worst_dependency_health: null,
        used_by_declared: 0,
        used_by_observed: 0,
        idle: false,
        child_groups: 0,
    },
    governance_gaps: ['owner', 'intended_use', 'lifecycle_stage', 'eu_ai_act_risk_tier'],
};

function fleet(systems: AISystemSummary[]) {
    return {
        systems,
        stats: {
            total: systems.length,
            by_kind: {},
            by_lifecycle_stage: {},
            by_risk_tier: {},
            without_owner: systems.filter((s) => !s.owner).length,
            without_intended_use: systems.filter((s) => !s.intended_use).length,
            fully_documented: systems.filter((s) => s.governance_gaps.length === 0).length,
        },
        total: systems.length,
        limit: 100,
        offset: 0,
    };
}

// Gallery is the default layout; the table-row suites below pin the table.
// The 'gallery view' suite clears this in its own beforeEach.
beforeEach(() => {
    localStorage.setItem('aiSystemsFleetView', 'table');
});

describe('AISystemsFleetTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem, undocumentedSystem]));
    });

    // ── The queue nobody remembers to check ─────────────────────
    it('flags a deprecated system that has gone quiet as ready to retire', async () => {
        mockFetchAISystems.mockResolvedValue(
            fleet([
                {
                    ...documentedSystem,
                    id: 'sys-old',
                    lifecycle_stage: 'deprecated',
                    retirable: true,
                    retirable_at: '2026-07-01T00:00:00Z',
                },
            ])
        );
        render(<AISystemsFleetTab />);

        expect(await screen.findByTestId('retirable-sys-old')).toHaveTextContent(
            /Ready to retire/i
        );
    });

    it('does not flag a deprecated system still inside its quiet window', async () => {
        mockFetchAISystems.mockResolvedValue(
            fleet([
                {
                    ...documentedSystem,
                    id: 'sys-old',
                    lifecycle_stage: 'deprecated',
                    retirable: false,
                    retirable_at: '2026-09-22T00:00:00Z',
                },
            ])
        );
        render(<AISystemsFleetTab />);

        await screen.findByTestId('ai-system-row-sys-old');
        expect(screen.queryByTestId('retirable-sys-old')).toBeNull();
    });

    it('fetches on mount and renders one row per AI System', async () => {
        render(<AISystemsFleetTab />);

        await waitFor(() => {
            expect(screen.getByTestId('ai-system-row-group-refund')).toBeInTheDocument();
        });
        expect(screen.getByTestId('ai-system-row-group-nightly')).toBeInTheDocument();
        expect(screen.getByText('Refund Agent')).toBeInTheDocument();
        expect(screen.getByText('Nightly Sync')).toBeInTheDocument();
        expect(mockFetchAISystems).toHaveBeenCalledTimes(1);
    });

    it('renders the system kind label, not the raw value', async () => {
        render(<AISystemsFleetTab />);

        // Scoped to the rows: the same labels also populate the kind filter's
        // <option> list, so an unscoped query is ambiguous by construction.
        const agentRow = await screen.findByTestId('ai-system-row-group-refund');
        expect(agentRow).toHaveTextContent('Agent');
        expect(agentRow).not.toHaveTextContent('agent-kind-raw');

        const integrationRow = screen.getByTestId('ai-system-row-group-nightly');
        expect(integrationRow).toHaveTextContent('Integration');
    });

    it('shows a Documented badge for a system with no governance gaps', async () => {
        render(<AISystemsFleetTab />);

        await waitFor(() => {
            expect(screen.getByText('Documented')).toBeInTheDocument();
        });
        expect(screen.queryByTestId('gaps-group-refund')).not.toBeInTheDocument();
    });

    it('renders every governance gap as its own labelled chip', async () => {
        render(<AISystemsFleetTab />);

        const gaps = await screen.findByTestId('gaps-group-nightly');
        expect(gaps).toHaveTextContent('Owner');
        expect(gaps).toHaveTextContent('Intended use');
        expect(gaps).toHaveTextContent('Lifecycle stage');
        expect(gaps).toHaveTextContent('Risk tier');
    });

    it('marks unset governance attributes explicitly rather than leaving cells blank', async () => {
        render(<AISystemsFleetTab />);

        await waitFor(() => {
            expect(screen.getByTestId('ai-system-row-group-nightly')).toBeInTheDocument();
        });
        // owner / lifecycle / risk are all unset on the nightly sync row.
        expect(screen.getAllByText('not set').length).toBeGreaterThanOrEqual(3);
    });

    it('passes the kind filter through to the API', async () => {
        render(<AISystemsFleetTab />);
        await waitFor(() => expect(mockFetchAISystems).toHaveBeenCalledTimes(1));

        fireEvent.change(screen.getByLabelText('Filter by kind'), {
            target: { value: 'integration' },
        });

        await waitFor(() => {
            expect(mockFetchAISystems).toHaveBeenLastCalledWith(
                expect.objectContaining({ system_kind: 'integration' })
            );
        });
    });

    it('passes the undocumented-only filter through to the API', async () => {
        render(<AISystemsFleetTab />);
        await waitFor(() => expect(mockFetchAISystems).toHaveBeenCalledTimes(1));

        fireEvent.click(screen.getByLabelText(/governance gaps/i));

        await waitFor(() => {
            expect(mockFetchAISystems).toHaveBeenLastCalledWith(
                expect.objectContaining({ undocumented_only: true })
            );
        });
    });

    it('shows the onboarding empty state when there are no AI Systems at all', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([]));
        render(<AISystemsFleetTab />);

        await waitFor(() => {
            expect(screen.getByText('No AI Systems yet')).toBeInTheDocument();
        });
    });

    it('shows a filter-specific empty state rather than the onboarding copy', async () => {
        render(<AISystemsFleetTab />);
        await waitFor(() => expect(mockFetchAISystems).toHaveBeenCalledTimes(1));

        mockFetchAISystems.mockResolvedValue(fleet([]));
        fireEvent.change(screen.getByLabelText('Filter by kind'), {
            target: { value: 'workflow' },
        });

        await waitFor(() => {
            expect(screen.getByText('No AI Systems match these filters.')).toBeInTheDocument();
        });
        // Telling an operator with 40 systems that they have none would be a lie.
        expect(screen.queryByText('No AI Systems yet')).not.toBeInTheDocument();
    });

    it('surfaces API failure instead of rendering an empty fleet', async () => {
        mockFetchAISystems.mockRejectedValue(new Error('control plane unreachable'));
        render(<AISystemsFleetTab />);

        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
        expect(screen.queryByText('No AI Systems yet')).not.toBeInTheDocument();
    });

    it('clicking a row opens the system drilldown (Signals, Runs, Drift…)', async () => {
        render(<AISystemsFleetTab />);

        await waitFor(() => {
            expect(screen.getByText('Refund Agent')).toBeInTheDocument();
        });
        fireEvent.click(screen.getByTestId('ai-system-row-group-refund'));
        const stub = screen.getByTestId('runs-tab-stub');
        expect(stub).toHaveAttribute('data-system', 'group-refund');
    });

    it('fetches the fleet with the health rollup attached', async () => {
        render(<AISystemsFleetTab />);
        await waitFor(() => {
            expect(mockFetchAISystems).toHaveBeenCalledWith(
                expect.objectContaining({ include_health: true }),
            );
        });
    });
});

describe('assurance stripe', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('green is earned twice: healthy AND assured', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([
            { ...documentedSystem, health_state: 'healthy', verdict_status: 'assured' },
        ]));
        render(<AISystemsFleetTab />);
        const stripe = await screen.findByTestId('stripe-group-refund');
        expect(stripe.className).toContain('bg-green-500');
        expect(stripe).toHaveAttribute('title', 'Health: healthy · Verdict: assured');
    });

    it('drifting reads orange and violations read red, whatever else looks fine', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([
            { ...documentedSystem, health_state: 'drifting', verdict_status: 'assured' },
            {
                ...undocumentedSystem,
                id: 'group-viol',
                display_name: 'Violator',
                health_state: 'healthy',
                verdict_status: 'violations',
            },
        ]));
        render(<AISystemsFleetTab />);
        expect((await screen.findByTestId('stripe-group-refund')).className).toContain('bg-orange-500');
        expect(screen.getByTestId('stripe-group-viol').className).toContain('bg-red-500');
    });

    it('a silent system reads orange — stopped is never quiet-fine', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([
            { ...documentedSystem, health_state: 'silent', verdict_status: 'assured' },
        ]));
        render(<AISystemsFleetTab />);
        expect((await screen.findByTestId('stripe-group-refund')).className).toContain('bg-orange-500');
    });

    it('no judgment renders gray, never green', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem]));
        render(<AISystemsFleetTab />);
        const stripe = await screen.findByTestId('stripe-group-refund');
        expect(stripe.className).toContain('bg-gray-300');
        expect(stripe).toHaveAttribute('title', 'Health: not yet judged · Verdict: no verdict yet');
    });
});

describe('deep-link drilldown (Mission Control findings badge)', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem, undocumentedSystem]));
    });

    it('initialDrilldownId opens the drilldown directly on the requested section', async () => {
        render(
            <AISystemsFleetTab
                initialDrilldownId="group-refund"
                initialSection="drift"
            />
        );
        // Drilldown renders immediately — no fleet-table detour.
        const stub = screen.getByTestId('runs-tab-stub');
        expect(stub).toHaveAttribute('data-system', 'group-refund');
        expect(stub).toHaveAttribute('data-section', 'drift');
        // The header starts blank and fills with the system's display
        // name once the fleet response arrives.
        await waitFor(() => {
            expect(screen.getByText('Refund Agent')).toBeInTheDocument();
        });
    });

    it('without initialSection the drilldown keeps the default view', async () => {
        render(<AISystemsFleetTab initialDrilldownId="group-nightly" />);
        expect(screen.getByTestId('runs-tab-stub')).toHaveAttribute('data-section', 'signals');
    });
});

describe('column explainers', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem, undocumentedSystem]));
    });

    it('Risk header opens a popover declaring operational impact', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-row-group-refund');
        fireEvent.click(screen.getByTestId('th-info-risk'));
        const pop = await screen.findByTestId('th-info-popover-risk');
        expect(pop).toHaveTextContent('Operational impact');
        expect(pop).toHaveTextContent(/obligations/i);
    });

    it('Kind header says it is informational, not enforced', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-row-group-refund');
        fireEvent.click(screen.getByTestId('th-info-kind'));
        const pop = await screen.findByTestId('th-info-popover-kind');
        expect(pop).toHaveTextContent('Informational');
        expect(pop).toHaveTextContent(/never changes enforcement/i);
    });

    it('Escape closes an open header popover', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-row-group-refund');
        fireEvent.click(screen.getByTestId('th-info-lifecycle'));
        await screen.findByTestId('th-info-popover-lifecycle');
        fireEvent.keyDown(document, { key: 'Escape' });
        await waitFor(() => {
            expect(screen.queryByTestId('th-info-popover-lifecycle')).toBeNull();
        });
    });
});

/**
 * The kind is what tells you how to read every signal below it — an
 * assistant is deliberately not scored on liveness and only advisory on
 * behavioural drift, an agent is enforced on both. The fleet list showed it
 * in its own column; the drilldown header showed only the name, so the one
 * fact that changes the meaning of the page disappeared on the way in.
 */
describe('AISystemsFleetTab — system kind in the drilldown header', () => {
    it('shows the kind when a row is clicked', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem, undocumentedSystem]));
        render(<AISystemsFleetTab />);
        fireEvent.click(await screen.findByTestId('ai-system-row-group-refund'));

        const chip = await screen.findByTestId('drilldown-system-kind');
        expect(chip.textContent).toMatch(/agent/i);
    });

    it('shows the right kind for a different system', async () => {
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem, undocumentedSystem]));
        render(<AISystemsFleetTab />);
        fireEvent.click(await screen.findByTestId('ai-system-row-group-nightly'));

        const chip = await screen.findByTestId('drilldown-system-kind');
        expect(chip.textContent).toMatch(/integration/i);
    });

    it('fills the kind in when the drilldown arrives by deep link', async () => {
        // A deep link carries only the id; name and kind are filled in once
        // the fleet payload lands. The kind must ride along with the name on
        // that fill-in path.
        mockFetchAISystems.mockResolvedValue(fleet([documentedSystem, undocumentedSystem]));
        render(<AISystemsFleetTab initialDrilldownId="group-refund" />);

        const chip = await screen.findByTestId('drilldown-system-kind');
        expect(chip.textContent).toMatch(/agent/i);
    });
});



// ---------------------------------------------------------------------------
// The list reads the estate's composition correctly before
// anyone opens a system.
// ---------------------------------------------------------------------------

/** The Brutor Demo System: a root that depends on a shared service. */
const demoRoot: AISystemSummary = {
    ...documentedSystem,
    id: 'sys-demo',
    name: 'brutor-demo',
    display_name: 'Brutor Demo System',
    eu_ai_act_risk_tier: 'high',
    risk_tier_effective: 'high',
    effective_tier_sources: [],
    role: 'root',
    has_external_dependencies: true,
    composition: {
        members: { agents: 3, models: 2, mcp_servers: 1, skills: 1 },
        depends_on: 2,
        worst_dependency_health: 'degraded',
        used_by_declared: 0,
        used_by_observed: 0,
        idle: false,
        child_groups: 0,
    },
    composition_finding_count: 0,
};

/** The fraud screener: a shared service whose effective tier is lifted. */
const fraudService: AISystemSummary = {
    ...documentedSystem,
    id: 'sys-fraud',
    name: 'fraud-screener',
    display_name: 'Fraud Screener',
    eu_ai_act_risk_tier: 'minimal',
    risk_tier_effective: 'high',
    effective_tier_sources: [{ system_id: 'sys-demo', system_name: 'Brutor Demo System', tier: 'high' }],
    role: 'shared_service',
    has_external_dependencies: false,
    composition: {
        members: { agents: 1, models: 1, mcp_servers: 0, skills: 0 },
        depends_on: 0,
        worst_dependency_health: null,
        used_by_declared: 1,
        used_by_observed: 2,
        idle: false,
        child_groups: 0,
    },
    composition_finding_count: 1,
};

/** A shared service nobody called in the window. */
const idleService: AISystemSummary = {
    ...fraudService,
    id: 'sys-idle',
    display_name: 'Address Validator',
    eu_ai_act_risk_tier: 'limited',
    risk_tier_effective: 'limited',
    effective_tier_sources: [],
    composition: {
        members: { agents: 1, models: 1, mcp_servers: 0, skills: 0 },
        depends_on: 0,
        worst_dependency_health: null,
        used_by_declared: 1,
        used_by_observed: 0,
        idle: true,
        child_groups: 0,
    },
    liveness: { state: 'idle', reason: 'No inbound calls in the window — on-demand, so not a fault.' },
    composition_finding_count: 0,
};

describe('composition, risk and role', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchAISystems.mockResolvedValue(fleet([demoRoot, fraudService, idleService]));
    });

    it('Composition shows members, depends-on with worst health, and used-by declared vs observed', async () => {
        render(<AISystemsFleetTab />);
        const root = await screen.findByTestId('composition-sys-demo');
        expect(root).toHaveTextContent('3 agents · 2 models · 1 MCP · 1 skills');
        expect(screen.getByTestId('depends-on-sys-demo')).toHaveTextContent('depends on 2');
        expect(screen.getByTestId('depends-on-sys-demo')).toHaveTextContent('Degraded');
        // The badge is the dependency's health, labelled as such.
        expect(screen.getByTestId('depends-on-sys-demo')).toHaveTextContent('depends on 2, the worst Degraded');
        expect(screen.getByTestId('external-sys-demo')).toBeInTheDocument();
        expect(screen.getByTestId('role-sys-demo')).toHaveTextContent('Root');

        expect(screen.getByTestId('used-by-sys-fraud')).toHaveTextContent('used by 1 declared / 2 observed');
        expect(screen.getByTestId('role-sys-fraud')).toHaveTextContent('Shared service');
    });

    it('a shared service with no callers in the window reads "Idle" (the liveness verdict), never an empty cell', async () => {
        render(<AISystemsFleetTab />);
        const badge = await screen.findByTestId('liveness-sys-idle');
        expect(badge).toHaveTextContent('Idle');
        expect(badge).toHaveAttribute('data-state', 'idle');
        expect(badge).toHaveAttribute('title', 'No inbound calls in the window — on-demand, so not a fault.');
        expect(screen.getByTestId('used-by-sys-idle')).toHaveTextContent('used by 1 declared / 0 observed');
    });

    it.each([
        ['alive', 'Alive'],
        ['silent', 'Silent'],
        ['idle', 'Idle'],
        ['unstarted', 'Unstarted'],
        ['not_configured', 'Not configured'],
    ] as const)('renders the list row liveness state %s as "%s" from the list payload', async (state, label) => {
        mockFetchAISystems.mockResolvedValue(
            fleet([{ ...fraudService, liveness: { state, reason: `because ${state}` } }])
        );
        render(<AISystemsFleetTab />);
        const badge = await screen.findByTestId('liveness-sys-fraud');
        expect(badge).toHaveTextContent(label);
        expect(badge).toHaveAttribute('title', `because ${state}`);
    });

    it('Risk shows the effective tier and where it comes from when a caller lifts it', async () => {
        render(<AISystemsFleetTab />);
        const eff = await screen.findByTestId('risk-effective-sys-fraud');
        expect(eff).toHaveTextContent('high');
        expect(eff).toHaveTextContent('minimal → high via Brutor Demo System');
        // Declared == effective: no second line.
        expect(screen.queryByTestId('risk-effective-sys-demo')).toBeNull();
        expect(screen.getByTestId('risk-sys-demo')).toHaveTextContent('high');
    });

    it('Risk marks a declared separability rationale, and only where one is declared', async () => {
        mockFetchAISystems.mockResolvedValue(
            fleet([demoRoot, { ...fraudService, separability_declared: true }, idleService]),
        );
        render(<AISystemsFleetTab />);
        expect(await screen.findByTestId('risk-separability-sys-fraud')).toHaveTextContent('separability declared');
        expect(screen.queryByTestId('risk-separability-sys-demo')).toBeNull();
    });

    // The claim is only accepted once sealed; the list
    // says which.
    it('Risk says whether the separability claim is sealed', async () => {
        mockFetchAISystems.mockResolvedValue(
            fleet([
                { ...demoRoot, separability_declared: true, separability_sealed: false },
                { ...fraudService, separability_declared: true, separability_sealed: true },
                idleService,
            ]),
        );
        render(<AISystemsFleetTab />);
        const sealed = await screen.findByTestId('risk-separability-sys-fraud');
        expect(sealed).toHaveTextContent('separability declared · sealed');
        expect(sealed).toHaveAttribute('data-sealed', 'true');
        const unsealed = screen.getByTestId('risk-separability-sys-demo');
        expect(unsealed).toHaveTextContent('separability declared · not sealed');
        expect(unsealed).toHaveAttribute('data-sealed', 'false');
        expect(unsealed.className).toContain('text-amber-800');
    });

    it('marks a service that enforces its accepted callers, and only that one', async () => {
        mockFetchAISystems.mockResolvedValue(
            fleet([demoRoot, { ...fraudService, enforce_accepted_callers: true }, idleService]),
        );
        render(<AISystemsFleetTab />);
        expect(await screen.findByTestId('enforced-callers-sys-fraud')).toHaveTextContent('listed callers only');
        expect(screen.queryByTestId('enforced-callers-sys-demo')).toBeNull();
    });

    it('composition findings count as a governance gap', async () => {
        render(<AISystemsFleetTab />);
        expect(await screen.findByTestId('composition-findings-sys-fraud')).toHaveTextContent(
            '1 composition finding',
        );
        // No findings and no documentation gaps → Documented.
        expect(screen.queryByTestId('gaps-sys-demo')).toBeNull();
    });

    it('the Role filter sits beside kind and stage and passes role= to the API', async () => {
        render(<AISystemsFleetTab />);
        await waitFor(() => expect(mockFetchAISystems).toHaveBeenCalledTimes(1));
        const role = screen.getByLabelText('Filter by role');
        const options = Array.from(role.querySelectorAll('option')).map((o) => o.getAttribute('value'));
        expect(options).toEqual(['', 'root', 'shared_service', 'external_deps']);

        for (const value of ['root', 'shared_service', 'external_deps']) {
            fireEvent.change(role, { target: { value } });
            await waitFor(() =>
                expect(mockFetchAISystems).toHaveBeenLastCalledWith(expect.objectContaining({ role: value })),
            );
        }
        fireEvent.change(role, { target: { value: '' } });
        await waitFor(() =>
            expect(mockFetchAISystems).toHaveBeenLastCalledWith(expect.objectContaining({ role: undefined })),
        );
    });

    it('the Composition column help opens in the shared anchored popover', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-row-sys-demo');
        fireEvent.click(screen.getByTestId('th-info-composition'));
        const pop = await screen.findByTestId('th-info-popover-composition');
        expect(pop).toHaveTextContent(/idle/);
        expect(pop).toHaveTextContent(/depends on/);
    });

    it('a deep link can land on the Composition tab', () => {
        render(<AISystemsFleetTab initialDrilldownId="sys-demo" initialSection="composition" />);
        expect(screen.getByTestId('runs-tab-stub')).toHaveAttribute('data-section', 'composition');
    });
});

const CARD_FACTS: AISystemCardFacts = {
    agent: {
        identity_id: 'agent-01ABC', identity_name: 'refund-worker', identity_count: 1,
        impl_name: 'refund-agent', impl_version: '1.4.2', impl_build: null, impl_trust: 'verified',
        declared_outcomes: 3, contract_version: 3, contract_status: 'active',
    },
    gateway: { policies: 4, guardrails: 2, limits: 6, blocks: 1 },
    resources: { models: 2, mcp_servers: 3, skills: 1, delegate_agents: 1 },
    kpis: {
        window_hours: 168, basis: 'runs', traces: 146, spans: 1314, success_rate: 0.97, failed: 2,
        p50_latency_ms: 1840, p95_latency_ms: 6200, cost_usd: 2.21, tokens: 272800,
        input_tokens: 210000, output_tokens: 62800, eval_suites: 0, last_eval_pass_rate: null, last_eval_at: null,
    },
};

/** Expand a gallery card (collapsed is the default). */
function expandCard(id: string) {
    fireEvent.click(screen.getByTestId(`card-toggle-${id}`));
}

describe('gallery view', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        localStorage.clear();
        mockFetchAISystems.mockResolvedValue(fleet([
            {
                ...documentedSystem,
                hierarchy_path: 'borealis-consumer-finance/brutor-demo-system',
                health_state: 'healthy',
                verdict_status: 'assured',
                card: CARD_FACTS,
            },
            { ...undocumentedSystem, health_state: 'silent', verdict_status: 'assured_with_exceptions' },
        ]));
    });

    it('defaults to cards (asking for card facts) and remembers a switch to the table', async () => {
        const { unmount } = render(<AISystemsFleetTab />);
        expect(await screen.findByTestId('fleet-gallery')).toBeInTheDocument();
        expect(mockFetchAISystems).toHaveBeenCalledWith(expect.objectContaining({ include_card: true }));

        fireEvent.click(screen.getByTestId('fleet-view-table'));
        expect(await screen.findByTestId('ai-system-row-group-refund')).toBeInTheDocument();
        expect(screen.queryByTestId('fleet-gallery')).not.toBeInTheDocument();
        unmount();

        render(<AISystemsFleetTab />);
        expect(await screen.findByTestId('ai-system-row-group-refund')).toBeInTheDocument();
    });

    it('outlines each card with the shared ladder: green earned twice', async () => {
        render(<AISystemsFleetTab />);
        const green = await screen.findByTestId('ai-system-card-group-refund');
        expect(green.className).toContain('border-green-500');
        expect(screen.getByTestId('card-summary-group-refund')).toHaveTextContent('Within policy');
        const orange = screen.getByTestId(`ai-system-card-${undocumentedSystem.id}`);
        expect(orange.className).toContain('border-orange-500');
    });

    it('shows the path on one line and the full verdict', async () => {
        render(<AISystemsFleetTab />);
        const path = await screen.findByTestId('card-path-group-refund');
        expandCard(undocumentedSystem.id);
        expect(path).toHaveTextContent('borealis-consumer-finance/brutor-demo-system');
        // One line, never wrapped; the full path is on hover if ever clipped.
        expect(path.className).toContain('whitespace-nowrap');
        expect(path).toHaveAttribute('title', 'borealis-consumer-finance/brutor-demo-system');
        const verdict = screen.getByTestId(`card-verdict-${undocumentedSystem.id}`);
        expect(verdict).toHaveTextContent('Assured w/ exceptions');
        expect(verdict.innerHTML).not.toContain('truncate');
    });

    it('fills the agent, gateway and resources boxes from the card facts', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-card-group-refund');
        expandCard('group-refund');
        const agent = screen.getByTestId('card-node-agent-group-refund');
        expect(agent).toHaveTextContent('refund-agent v1.4.2');
        expect(agent).toHaveTextContent('ID agent-01ABC');
        expect(agent).toHaveTextContent('3 outcomes');
        expect(agent).toHaveTextContent('contract v3 · active');
        expect(agent).not.toHaveTextContent('agents');

        const gw = screen.getByTestId('card-node-gateway-group-refund');
        expect(gw).toHaveTextContent('Gateway');
        expect(gw).toHaveTextContent('4 policies · 6 limits');
        expect(gw).toHaveTextContent('2 guardrails');

        const res = screen.getByTestId('card-node-resources-group-refund');
        expect(res).toHaveTextContent('Resources');
        expect(res).toHaveTextContent('2 models · 3 MCP');
        expect(res).toHaveTextContent('1 skill · 1 delegate');
    });

    it('shows observability and evaluation — and says when there is no offline eval', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-card-group-refund');
        expandCard('group-refund');
        expandCard(undocumentedSystem.id);
        const panel = screen.getByTestId('card-observability-group-refund');
        expect(panel).toHaveTextContent('146');
        expect(panel).toHaveTextContent('97%');
        expect(screen.getByTestId('card-latency-group-refund')).toHaveTextContent('1.8s');
        expect(screen.getByTestId('card-latency-group-refund')).toHaveTextContent('p95 6.2s');
        expect(screen.getByTestId('card-evals-group-refund')).toHaveTextContent('no replay suite');
        // Tokens have their own field, split in / out; cost shows per-trace.
        expect(screen.getByTestId('card-tokens-group-refund')).toHaveTextContent('272.8k');
        expect(screen.getByTestId('card-tokens-group-refund')).toHaveTextContent('in 210.0k · out 62.8k');
        expect(screen.getByTestId('card-cost-group-refund')).toHaveTextContent('$2.21');
        expect(screen.getByTestId('card-cost-group-refund')).toHaveTextContent('$0.015 / trace');
        expect(screen.getByTestId('card-cost-group-refund')).not.toHaveTextContent('tokens');
        expect(panel).not.toHaveTextContent('Feedback');
        // A system without card facts gets no panel rather than invented zeros.
        expect(screen.queryByTestId(`card-observability-${undocumentedSystem.id}`)).not.toBeInTheDocument();
    });

    it('explains every observability field and Risk / Health / Verdict behind an (i), without collapsing the card', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-card-group-refund');
        expandCard('group-refund');
        const card = screen.getByTestId('ai-system-card-group-refund');
        const marks = [
            'traces', 'success', 'latency-p50', 'cost', 'tokens', 'offline-evals',
            'risk', 'health', 'verdict',
        ];
        for (const slug of marks) {
            expect(within(card).getByTestId(`card-info-${slug}`)).toBeInTheDocument();
        }

        fireEvent.click(within(card).getByTestId('card-info-traces'));
        const popover = await screen.findByTestId('card-info-traces-popover');
        expect(popover).toHaveTextContent('one run is one task end to end');
        expect(popover).toHaveTextContent('replays are never counted');
        // Opening help is not toggling the card.
        expect(screen.getByTestId('card-toggle-group-refund')).toHaveAttribute('aria-expanded', 'true');

        fireEvent.click(within(card).getByTestId('card-info-verdict'));
        expect(await screen.findByTestId('card-info-verdict-popover')).toHaveTextContent('operating envelope');
    });

    it('labels a shared service’s figures as served calls, not traces', async () => {
        // A service called over A2A roots no runs; its card must not read
        // "Traces 0 · $0" while it does real work inside its callers' runs.
        mockFetchAISystems.mockResolvedValue(fleet([
            {
                ...documentedSystem,
                health_state: 'healthy',
                verdict_status: 'assured',
                card: { ...CARD_FACTS, kpis: { ...CARD_FACTS.kpis, basis: 'served_calls', traces: 105, cost_usd: 0.0863 } },
            },
        ]));
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-card-group-refund');
        expandCard('group-refund');
        expect(screen.getByTestId('card-observability-group-refund')).toHaveAttribute('data-basis', 'served_calls');
        expect(screen.getByTestId('card-served-basis-group-refund')).toHaveTextContent('calls it served');
        expect(screen.getByTestId('card-traces-group-refund')).toHaveTextContent('Served calls');
        expect(screen.getByTestId('card-traces-group-refund')).toHaveTextContent('105');
        expect(screen.getByTestId('card-cost-group-refund')).toHaveTextContent('/ call');
        expect(screen.getByTestId('card-info-served-calls')).toBeInTheDocument();
    });

    it('cards start collapsed; clicking one expands only that one, and again collapses it', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-card-group-refund');
        // Collapsed: identity, verdict pill and owner only.
        expect(screen.getByTestId('card-summary-group-refund')).toBeInTheDocument();
        expect(screen.getByTestId('ai-system-card-group-refund')).toHaveTextContent('payments-team');
        expect(screen.queryByTestId('card-node-agent-group-refund')).not.toBeInTheDocument();
        expect(screen.getByTestId('card-toggle-group-refund')).toHaveAttribute('aria-expanded', 'false');

        expandCard('group-refund');
        expect(screen.getByTestId('card-node-agent-group-refund')).toBeInTheDocument();
        expect(screen.getByTestId('card-toggle-group-refund')).toHaveAttribute('aria-expanded', 'true');
        // The neighbour stays collapsed.
        expect(screen.queryByTestId(`card-node-agent-${undocumentedSystem.id}`)).not.toBeInTheDocument();
        // Expanding is not navigating.
        expect(screen.queryByTestId('runs-tab-stub')).not.toBeInTheDocument();

        expandCard('group-refund');
        expect(screen.queryByTestId('card-node-agent-group-refund')).not.toBeInTheDocument();
    });

    it('names governance gaps on the card, and opens the drilldown from its open button', async () => {
        render(<AISystemsFleetTab />);
        await screen.findByTestId('ai-system-card-group-refund');
        expandCard('group-refund');
        expandCard(undocumentedSystem.id);
        expect(screen.getByTestId('card-governance-group-refund')).toHaveTextContent('Governance documented');
        expect(screen.getByTestId(`card-governance-${undocumentedSystem.id}`)).toHaveTextContent('governance gap');

        fireEvent.click(screen.getByTestId('card-open-group-refund'));
        expect(screen.getByTestId('runs-tab-stub')).toHaveAttribute('data-system', 'group-refund');
    });
});
