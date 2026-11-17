/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control card dashboard — the RG-card landing page.
 *
 * The load-bearing assertions mirror the API's two rules:
 *   * group cards render worst-of judgments ("Violations",
 *     "contains T(n·k)") — the UI must not soften them;
 *   * the window pills refetch measurements while the pinned-judgment
 *     framing stays on screen;
 * plus the navigation contract (drill-down vs groups:<id> vs
 * mission-control-metrics:<id>), the pseudo-card routes, the
 * server-fed explainer info boxes, and the alerts drawer.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react';

import type { MissionControlCard } from '../../../lib/missionControlCardTypes';
import type { SystemAlert } from '../../../lib/groupTypes';
import type { AlertActionSlotItem } from '../../../app/slots';

vi.mock('../../../hooks/useGateway', async () => (await import('../../../test/monitoring/missionControlDashboard')).useGatewayModule());

vi.mock('../../common/Toast', () => ({
    useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('../../common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));

import MissionControlDashboard from '../MissionControlDashboard';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { SlotProvider } from '../../../app/SlotProvider';
import {
    EXPLAINERS,
    IMPORTED_GROUP,
    ORG_CARD,
    PSEUDO_UNATTRIBUTED,
    card,
    dashboardLoaded,
    mockAcknowledgeSystemAlert,
    mockAcknowledgeUsageAlert,
    mockFetchAllUsageAlerts,
    mockFetchCards,
    mockFetchExplainers,
    mockFetchSystemAlerts,
    mockFetchTenantDriftFeed,
    mockInboxCounts,
    mockResolveSystemAlert,
    resetDashboard,
    response,
} from '../../../test/monitoring/missionControlDashboard';

// A leaf group that is NOT an AI System — the trial's Default group is the
// canonical example (group_type='custom', no children). Clicking it must
// NEVER open the AI Systems drilldown: that view can only render its
// "not an AI System" error for this card.
const CUSTOM_LEAF_CARD = card({
    id: 'grp-default',
    name: 'Default',
    group_type: 'custom',
    system_kind: null,
    is_group: false,
});

/** Rendered the way the application does, with the built-in slot items; `slots: false` contributes nothing. */
async function renderDashboard(setActiveTab = vi.fn(), { slots = true } = {}) {
    render(<MissionControlDashboard setActiveTab={setActiveTab} />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await dashboardLoaded();
    return setActiveTab;
}

beforeEach(resetDashboard);

// ─────────────────────────────────────────────────────────────────────────
// Rendering the estate
// ─────────────────────────────────────────────────────────────────────────

describe('card grid', () => {
    it('renders one tile per card from the API', async () => {
        await renderDashboard();
        expect(screen.getByTestId('mc-card-grp-org')).toBeInTheDocument();
        expect(screen.getByTestId('mc-card-sys-copilot')).toBeInTheDocument();
        expect(screen.getByTestId('mc-card-pseudo-unattributed')).toBeInTheDocument();
        expect(screen.getByTestId('mc-card-grp-imported')).toBeInTheDocument();
    });

    it('renders worst-of judgments on group cards without softening them', async () => {
        await renderDashboard();
        const org = screen.getByTestId('mc-card-grp-org');
        expect(org).toHaveTextContent('Violations');
        expect(org).toHaveTextContent('Drifting');
        // The subtree class reads "contains", never as the group's own.
        expect(screen.getByTestId('mc-tclass-grp-org')).toHaveTextContent('contains T(n·k)');
    });

    it('a system card owns its class outright', async () => {
        await renderDashboard();
        const chip = screen.getByTestId('mc-tclass-sys-copilot');
        expect(chip).toHaveTextContent('T(n)');
        expect(chip).not.toHaveTextContent('contains');
    });

    it('shows the open-findings badge with its count', async () => {
        await renderDashboard();
        expect(screen.getByTestId('mc-findings-grp-org')).toHaveTextContent('2 open findings');
    });

    it('marks a blended group cost per task as blended', async () => {
        await renderDashboard();
        expect(screen.getByTestId('mc-card-grp-org')).toHaveTextContent('(blended)');
    });

    it('renders a breached cost-per-task bound in the alarm style', async () => {
        mockFetchCards.mockResolvedValue(response([
            card({ cpt_bound: 0.005, cost_per_task: 0.01, cpt_breached: true }),
        ]));
        await renderDashboard();
        const tile = screen.getByTestId('mc-card-sys-copilot');
        expect(tile).toHaveTextContent('bound $0.0050');
        expect(tile.querySelector('.text-red-600')).not.toBeNull();
    });

    // Cost per task is a mean over completed runs: a system with none has no
    // mean, and the row says so instead of vanishing (it read as a bug).
    it('an AI System with no completed runs still shows its cost-per-task row', async () => {
        mockFetchCards.mockResolvedValue(response([
            card({ runs: 0, completed_runs: 0, spend: 0, cost_per_task: null, cpt_bound: 0.15 }),
        ]));
        await renderDashboard();
        const row = screen.getByTestId('mc-cpt-sys-copilot');
        expect(row).toHaveTextContent('no completed runs in window');
        expect(row).toHaveTextContent('bound $0.1500');
    });

    it('pseudo-cards carry their honesty note instead of judgments', async () => {
        await renderDashboard();
        const ua = screen.getByTestId('mc-card-pseudo-unattributed');
        expect(ua).toHaveTextContent('Governed traffic that matched no AI System.');
        expect(ua).not.toHaveTextContent('Assured');
    });

    it('an imported-usage group reports its spend, and says nothing enforced it', async () => {
        await renderDashboard();
        const c = screen.getByTestId('mc-card-grp-imported');
        expect(c).toHaveTextContent('$114.45');
        expect(screen.getByTestId('mc-observed-grp-imported')).toHaveTextContent('observed only');
    });

    it('a group with no runs and no systems does not claim "0 runs"', async () => {
        await renderDashboard();
        expect(screen.getByTestId('mc-card-grp-imported')).not.toHaveTextContent('0 runs');
        // A real AI System with no traffic in the window still says so.
        expect(screen.getByTestId('mc-card-sys-copilot')).toHaveTextContent('runs');
    });

    it('shows the empty state when a level has no groups', async () => {
        mockFetchCards.mockResolvedValue(response([]));
        await renderDashboard();
        expect(screen.getByTestId('mc-empty')).toBeInTheDocument();
    });

    it('shows the error state when the API fails', async () => {
        mockFetchCards.mockRejectedValue(new Error('boom'));
        render(<MissionControlDashboard />);
        await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument());
    });
});

// ─────────────────────────────────────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────────────────────────────────────

describe('navigation', () => {
    it('clicking a group card drills down via parent_id', async () => {
        await renderDashboard();
        mockFetchCards.mockResolvedValue(response([card()], {
            parent_id: 'grp-org',
            path: [{ id: 'grp-org', name: 'Acme AI' }],
        }));
        fireEvent.click(screen.getByTestId('mc-card-grp-org'));
        await waitFor(() =>
            expect(mockFetchCards).toHaveBeenLastCalledWith(
                expect.objectContaining({ parent_id: 'grp-org' }),
            ),
        );
        // Breadcrumb shows the path; Estate returns to root.
        const crumb = await screen.findByTestId('mc-breadcrumb');
        expect(crumb).toHaveTextContent('Acme AI');
        fireEvent.click(screen.getByRole('button', { name: /estate/i }));
        await waitFor(() =>
            expect(mockFetchCards).toHaveBeenLastCalledWith(
                expect.objectContaining({ parent_id: undefined }),
            ),
        );
    });

    it('clicking a system card opens the AI Systems drilldown on Signals', async () => {
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-card-sys-copilot'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-copilot:signals');
    });

    it('clicking a CUSTOM leaf group opens its Usage tab, never the AI Systems drilldown', async () => {
        mockFetchCards.mockResolvedValue(
            response([ORG_CARD, card(), CUSTOM_LEAF_CARD, PSEUDO_UNATTRIBUTED, IMPORTED_GROUP]),
        );
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-card-grp-default'));
        expect(setActiveTab).toHaveBeenCalledWith('groups:grp-default:usage');
        // And its primary action is labelled accordingly.
        const tile = screen.getByTestId('mc-card-grp-default');
        expect(tile.textContent).toContain('Open group');
        expect(tile.textContent).not.toContain('Open system');
    });

    it('the Usage action opens the resource-group Usage tab for both card kinds', async () => {
        const setActiveTab = await renderDashboard();
        // An AI System is a resource group — its usage lives on
        // the RG Usage tab; an org group's Usage opens aggregated there.
        fireEvent.click(screen.getByTestId('mc-usage-sys-copilot'));
        expect(setActiveTab).toHaveBeenCalledWith('groups:sys-copilot:usage');
        fireEvent.click(screen.getByTestId('mc-usage-grp-org'));
        expect(setActiveTab).toHaveBeenCalledWith('groups:grp-org:usage');
    });

    it('the Metrics action opens the classic view scoped to the group', async () => {
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-metrics-sys-copilot'));
        expect(setActiveTab).toHaveBeenCalledWith('mission-control-metrics:sys-copilot');
    });

    it('a system card’s findings badge deep-links to its Drift tab', async () => {
        mockFetchCards.mockResolvedValue(response([card({ findings: 3 })]));
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-findings-sys-copilot'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-copilot:drift');
        // The badge click must not double as a card click (which would
        // navigate to the resource group instead).
        expect(setActiveTab).not.toHaveBeenCalledWith('groups:sys-copilot');
    });

    it('a group card’s findings badge opens the alerts drawer, not a tab', async () => {
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-findings-grp-org'));
        expect(await screen.findByTestId('mc-alerts-drawer')).toBeInTheDocument();
        expect(setActiveTab).not.toHaveBeenCalled();
    });

    it('the unattributed card routes to its work queue', async () => {
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-card-pseudo-unattributed'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems');
    });

    it('an imported-usage group drills down like any other group', async () => {
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-card-grp-imported'));
        // is_group: the dashboard drills in place rather than leaving MC.
        expect(setActiveTab).not.toHaveBeenCalledWith('traffic-import');
    });
});

// ─────────────────────────────────────────────────────────────────────────
// Windows, search, sort
// ─────────────────────────────────────────────────────────────────────────

describe('toolbar', () => {
    it('window pills refetch measurements and state the pinned-judgment rule', async () => {
        await renderDashboard();
        expect(screen.getByText(/pinned judgments/i)).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('mc-window-24h'));
        await waitFor(() =>
            expect(mockFetchCards).toHaveBeenLastCalledWith(
                expect.objectContaining({ window: '24h' }),
            ),
        );
    });

    it('offers All time with the blended caveat', async () => {
        await renderDashboard();
        const all = screen.getByTestId('mc-window-all');
        expect(all.title).toMatch(/blended/i);
        fireEvent.click(all);
        await waitFor(() =>
            expect(mockFetchCards).toHaveBeenLastCalledWith(
                expect.objectContaining({ window: 'all' }),
            ),
        );
    });

    it('search debounces, flattens the hierarchy and drops parent_id', async () => {
        vi.useFakeTimers();
        try {
            render(<MissionControlDashboard />);
            await act(async () => {
                await vi.runOnlyPendingTimersAsync();
            });
            fireEvent.change(screen.getByLabelText('Search resource groups'), {
                target: { value: 'copilot' },
            });
            await act(async () => {
                await vi.advanceTimersByTimeAsync(350);
            });
            expect(mockFetchCards).toHaveBeenLastCalledWith(
                expect.objectContaining({ q: 'copilot', parent_id: undefined }),
            );
        } finally {
            vi.useRealTimers();
        }
    });

    it('changing sort refetches server-side', async () => {
        await renderDashboard();
        fireEvent.change(screen.getByLabelText('Sort cards'), { target: { value: 'spend' } });
        await waitFor(() =>
            expect(mockFetchCards).toHaveBeenLastCalledWith(
                expect.objectContaining({ sort: 'spend' }),
            ),
        );
    });
});

// ─────────────────────────────────────────────────────────────────────────
// Explainer info boxes — one definition, every surface
// ─────────────────────────────────────────────────────────────────────────

describe('info boxes', () => {
    it("the T-class dot explains the card's OWN class, not the generic ladder", async () => {
        await renderDashboard();
        // The org card carries T(n·k); its dot must open the
        // multiplicative explainer, not the T(n) one or the fallback.
        const orgChip = screen.getByTestId('mc-tclass-grp-org');
        fireEvent.click(within(orgChip).getByTestId('info-dot-t_class'));
        const pop = await screen.findByTestId('info-pop-t_class');
        expect(pop).toHaveTextContent('T(n·k) — Multiplicative');
        expect(pop).toHaveTextContent('k is the median');
        // The shared footer points the operator at the guide.
        expect(pop).toHaveTextContent('see the Workload classes guide');
    });

    it('a class without its own entry falls back to the generic explainer', async () => {
        mockFetchCards.mockResolvedValue(response([card({ t_class: 'T(1)' })]));
        await renderDashboard();
        fireEvent.click(screen.getAllByTestId('info-dot-t_class')[0]);
        const pop = await screen.findByTestId('info-pop-t_class');
        expect(pop).toHaveTextContent('Big-T workload class');
    });

    it('an info box closes from its close button', async () => {
        await renderDashboard();
        fireEvent.click(screen.getAllByTestId('info-dot-t_class')[0]);
        await screen.findByTestId('info-pop-t_class');
        fireEvent.click(screen.getByLabelText('Close explainer'));
        expect(screen.queryByTestId('info-pop-t_class')).toBeNull();
    });

    it('a missing explainers endpoint leaves the dashboard working', async () => {
        mockFetchExplainers.mockRejectedValue(new Error('offline'));
        await renderDashboard();
        expect(screen.getByTestId('mc-card-sys-copilot')).toBeInTheDocument();
        fireEvent.click(screen.getAllByTestId('info-dot-t_class')[0]);
        expect(screen.queryByTestId('info-pop-t_class')).toBeNull();
    });
});

// ─────────────────────────────────────────────────────────────────────────
// The workload-classes guide
// ─────────────────────────────────────────────────────────────────────────

describe('workload-classes guide', () => {
    it('the header button opens the full guide sub-page from the server', async () => {
        await renderDashboard();
        fireEvent.click(await screen.findByTestId('mc-guide-button'));
        const page = await screen.findByTestId('mc-guide-page');
        expect(page).toHaveTextContent('Workload classes — the T(n·k·a) guide');
        expect(page).toHaveTextContent('Tokenomics Foundation');
        // The whole industry ladder, including the classes Brutor never
        // asserts on a card.
        for (const cls of ['T(1)', 'T(log n)', 'T(n)', 'T(n·k)', 'T(n·k·a)', 'T(∞)']) {
            expect(page).toHaveTextContent(cls);
        }
        // The growth-curves chart ships with the page.
        expect(page.querySelector('img[alt*="Big-T"]')).not.toBeNull();
    });

    it('the guide returns to the dashboard from the back link', async () => {
        await renderDashboard();
        fireEvent.click(await screen.findByTestId('mc-guide-button'));
        await screen.findByTestId('mc-guide-page');
        fireEvent.click(screen.getByTestId('mc-guide-back'));
        expect(screen.queryByTestId('mc-guide-page')).toBeNull();
        expect(await screen.findByTestId('mission-control-dashboard')).toBeInTheDocument();
    });

    it('Escape returns to the dashboard', async () => {
        await renderDashboard();
        fireEvent.click(await screen.findByTestId('mc-guide-button'));
        await screen.findByTestId('mc-guide-page');
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(screen.queryByTestId('mc-guide-page')).toBeNull();
    });

    it('no guide payload means no button, not a broken one', async () => {
        mockFetchExplainers.mockResolvedValue({ explainers: EXPLAINERS.explainers });
        await renderDashboard();
        expect(screen.queryByTestId('mc-guide-button')).toBeNull();
    });
});

// ─────────────────────────────────────────────────────────────────────────
// Alerts drawer
// ─────────────────────────────────────────────────────────────────────────

describe('alerts', () => {
    const SYSTEM_ALERT = {
        id: 'al-1', severity: 'high', title: 'Provider down', message: 'OpenAI 5xx',
        status: 'active', category: 'downstream_provider', resource_type: null,
        resource_id: null, error_code: null, tenant_id: 't1',
    };
    const USAGE_ALERT = {
        id: 'ua-1', resource_type: 'llm', group_id: 'grp-org', group_name: 'Acme AI',
        alert_type: 'hard_stop', metric: 'monthly_cost', threshold_pct: 100,
        current_value: 100, limit_value: 100, utilization_pct: 104, period: 'monthly',
    };
    const DRIFT = {
        id: 'drift-1', ai_system_id: 'sys-copilot', drift_class: 'cost',
        metric_key: 'cost_per_run', severity: 'high', status: 'active',
    };

    it('the bell counts the ops feeds plus the findings waiting in the Inbox', async () => {
        mockInboxCounts.mockResolvedValue({ open: 1, acknowledged: 0, resolved: 0, open_critical: 1, open_warning: 0 });
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [SYSTEM_ALERT] });
        mockFetchAllUsageAlerts.mockResolvedValue({ alerts: [USAGE_ALERT] });
        mockFetchTenantDriftFeed.mockResolvedValue({ events: [DRIFT], total: 1, open_count: 1 });
        await renderDashboard();
        await waitFor(() => expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('3'));
    });

    it('resolved system alerts never count', async () => {
        mockFetchSystemAlerts.mockResolvedValue({
            alerts: [SYSTEM_ALERT, { ...SYSTEM_ALERT, id: 'al-2', status: 'resolved' }],
        });
        await renderDashboard();
        await waitFor(() => expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('1'));
    });

    it('the drawer is ops-only and points at the Inbox for findings', async () => {
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [SYSTEM_ALERT] });
        mockFetchAllUsageAlerts.mockResolvedValue({ alerts: [USAGE_ALERT] });
        mockFetchTenantDriftFeed.mockResolvedValue({ events: [DRIFT], total: 1, open_count: 1 });
        mockInboxCounts.mockResolvedValue({ open: 1, acknowledged: 0, resolved: 0, open_critical: 1, open_warning: 0 });
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        const drawer = await screen.findByTestId('mc-alerts-drawer');
        expect(drawer).toHaveTextContent('Provider down');
        expect(drawer).toHaveTextContent('Acme AI');
        expect(drawer).toHaveTextContent('Blocked');
        expect(drawer).not.toHaveTextContent('Metric alerts');
        // Drift findings are inbox findings: read there, not listed twice.
        expect(drawer).not.toHaveTextContent('cost · cost_per_run');
        const line = await screen.findByTestId('mc-drawer-inbox-line');
        expect(line).toHaveTextContent('1 open assurance finding');
        fireEvent.click(line);
        expect(setActiveTab).toHaveBeenCalledWith('inbox');
    });

    it('acknowledging a usage alert removes it from the drawer', async () => {
        mockFetchAllUsageAlerts.mockResolvedValue({ alerts: [USAGE_ALERT] });
        mockAcknowledgeUsageAlert.mockResolvedValue({});
        await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        await screen.findByTestId('mc-usage-alert-ua-1');
        fireEvent.click(screen.getByLabelText('Acknowledge usage alert'));
        await waitFor(() => expect(mockAcknowledgeUsageAlert).toHaveBeenCalledWith('ua-1'));
        await waitFor(() => expect(screen.queryByTestId('mc-usage-alert-ua-1')).toBeNull());
    });

    it('acknowledging a system alert marks ownership without removing it', async () => {
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [SYSTEM_ALERT] });
        mockAcknowledgeSystemAlert.mockResolvedValue({
            status: 'acknowledged', acknowledged_by: 'admin', acknowledged_at: 'now',
        });
        await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        await screen.findByTestId('mc-system-alert-al-1');
        fireEvent.click(screen.getByLabelText('Acknowledge system alert'));
        await waitFor(() => expect(mockAcknowledgeSystemAlert).toHaveBeenCalledWith('al-1'));
        expect(await screen.findByText(/acked by admin/)).toBeInTheDocument();
    });

    it('resolving a system alert removes it', async () => {
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [SYSTEM_ALERT] });
        mockResolveSystemAlert.mockResolvedValue({});
        await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        await screen.findByTestId('mc-system-alert-al-1');
        fireEvent.click(screen.getByLabelText('Resolve system alert'));
        await waitFor(() => expect(screen.queryByTestId('mc-system-alert-al-1')).toBeNull());
    });

    it('with nothing contributed, the alert keeps Acknowledge and Resolve and has no rebuild', async () => {
        mockFetchSystemAlerts.mockResolvedValue({
            alerts: [{ ...SYSTEM_ALERT, error_code: 'kb_vector_dim_mismatch' }],
        });
        await renderDashboard(vi.fn(), { slots: false });
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        await screen.findByTestId('mc-system-alert-al-1');
        expect(screen.queryByTestId('mc-rebuild-kb-al-1')).toBeNull();
        expect(screen.getByLabelText('Acknowledge system alert')).toBeInTheDocument();
        expect(screen.getByLabelText('Resolve system alert')).toBeInTheDocument();
    });

    it('a feed that fails is not "no alerts": the bell is marked in words and the drawer says which feed, beside the feeds that answered', async () => {
        mockFetchSystemAlerts.mockRejectedValue(new Error('feed down'));
        mockFetchAllUsageAlerts.mockResolvedValue({ alerts: [USAGE_ALERT] });
        await renderDashboard();
        await waitFor(() => expect(screen.getByTestId('mc-alerts-bell')).toHaveAccessibleName(/could not be loaded/));
        expect(screen.getByTestId('mc-alerts-unknown')).toHaveTextContent('!');
        expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('1');

        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        const drawer = await screen.findByTestId('mc-alerts-drawer');
        await waitFor(() => expect(drawer).toHaveTextContent('Failed to load the system alerts: feed down'));
        expect(drawer).not.toHaveTextContent('No active system alerts.');
        expect(drawer).toHaveTextContent('Acme AI');

        mockFetchSystemAlerts.mockResolvedValue({ alerts: [SYSTEM_ALERT] });
        fireEvent.click(within(drawer).getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(drawer).toHaveTextContent('Provider down'));
        expect(drawer).not.toHaveTextContent('Failed to load the system alerts');
    });

    it('a feed that fails on a later read keeps its alerts, marked as stale', async () => {
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [SYSTEM_ALERT] });
        await renderDashboard();
        await waitFor(() => expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('1'));

        mockFetchSystemAlerts.mockRejectedValue(new Error('timeout'));
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        const drawer = await screen.findByTestId('mc-alerts-drawer');
        await waitFor(() => expect(drawer).toHaveTextContent(/Could not refresh; showing what was loaded at \d\d:\d\d/));
        expect(drawer).toHaveTextContent('Provider down');
        expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('1');
    });

    it('a quiet estate shows an unbadged bell', async () => {
        await renderDashboard();
        expect(screen.queryByTestId('mc-alerts-count')).toBeNull();
    });
});

describe('alerts — what the drawer gives a contributed action', () => {
    const ALERT = {
        id: 'al-1', severity: 'high', title: 'Provider down', message: 'OpenAI 5xx',
        status: 'active', category: 'downstream_provider', resource_type: null,
        resource_id: null, error_code: null, tenant_id: 't1',
    };
    /** The alerts the action was asked about, and the alerts it was rendered for. */
    const asked: SystemAlert[] = [];
    const given: SystemAlert[] = [];
    /** An action offered for alerts that carry an error code: it starts, finishes, or reports the alert resolved. */
    const fakeAction: AlertActionSlotItem = {
        id: 'fake',
        appliesTo: (alert) => {
            asked.push(alert);
            return alert.error_code !== null;
        },
        Action: ({ alert, busy, setBusy, onResolved }) => {
            given.push(alert);
            return (
                <>
                    <button type="button" data-testid={`fake-start-${alert.id}`} disabled={busy} onClick={() => setBusy(true)}>
                        Start
                    </button>
                    <button type="button" data-testid={`fake-finish-${alert.id}`} onClick={() => setBusy(false)}>
                        Finish
                    </button>
                    <button type="button" data-testid={`fake-resolved-${alert.id}`} onClick={onResolved}>
                        Resolved
                    </button>
                </>
            );
        },
    };

    async function renderWithAction() {
        asked.length = 0;
        given.length = 0;
        render(
            <SlotProvider slots={{ 'mission-control.alert-actions': [fakeAction] }}>
                <MissionControlDashboard setActiveTab={vi.fn()} />
            </SlotProvider>,
        );
        await dashboardLoaded();
    }

    it('offers the action only on the system alerts it applies to, before Acknowledge and Resolve, and hands it the alert', async () => {
        const coded = { ...ALERT, id: 'al-2', error_code: 'some_code' };
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [ALERT, coded] });
        await renderWithAction();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        await screen.findByTestId('mc-system-alert-al-1');
        // The action is asked about every system alert, and rendered where it said yes.
        expect(new Set(asked.map((a) => a.id))).toEqual(new Set(['al-1', 'al-2']));
        expect(screen.queryByTestId('fake-start-al-1')).toBeNull();
        const start = screen.getByTestId('fake-start-al-2');
        expect(start).toBeEnabled();
        expect(given.length).toBeGreaterThan(0);
        expect(given.every((a) => a.id === 'al-2' && a.error_code === 'some_code' && a.tenant_id === 't1')).toBe(true);
        // It stands in the alert's own action row, before the page's Acknowledge and Resolve.
        const row = within(screen.getByTestId('mc-system-alert-al-2')).getAllByRole('button');
        const at = row.indexOf(start);
        expect(at).toBeGreaterThanOrEqual(0);
        expect(at).toBeLessThan(row.findIndex((b) => b.getAttribute('aria-label') === 'Acknowledge system alert'));
        expect(at).toBeLessThan(row.findIndex((b) => b.getAttribute('aria-label') === 'Resolve system alert'));
    });

    it('an action that reports the alert resolved takes it off the list and the bell, without the page resolving it again', async () => {
        const coded = { ...ALERT, error_code: 'some_code' };
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [coded, { ...coded, id: 'al-2' }] });
        await renderWithAction();
        await waitFor(() => expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('2'));
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        fireEvent.click(await screen.findByTestId('fake-resolved-al-1'));
        await waitFor(() => expect(screen.queryByTestId('mc-system-alert-al-1')).toBeNull());
        expect(screen.getByTestId('mc-system-alert-al-2')).toBeInTheDocument();
        expect(screen.getByTestId('mc-alerts-count')).toHaveTextContent('1');
        // Resolving is the action's to do: the page only drops the alert.
        expect(mockResolveSystemAlert).not.toHaveBeenCalled();
    });

    it('keeps an action busy per alert, also when the drawer is closed and reopened, until the action says it is done', async () => {
        const coded = { ...ALERT, error_code: 'some_code' };
        mockFetchSystemAlerts.mockResolvedValue({ alerts: [coded, { ...coded, id: 'al-2' }] });
        await renderWithAction();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        fireEvent.click(await screen.findByTestId('fake-start-al-1'));
        await waitFor(() => expect(screen.getByTestId('fake-start-al-1')).toBeDisabled());
        // Busy is of this action on this alert: the same action on another alert is not.
        expect(screen.getByTestId('fake-start-al-2')).toBeEnabled();

        fireEvent.click(screen.getByLabelText('Close alerts'));
        expect(screen.queryByTestId('fake-start-al-1')).toBeNull();
        fireEvent.click(screen.getByTestId('mc-alerts-bell'));
        expect(await screen.findByTestId('fake-start-al-1')).toBeDisabled();
        expect(screen.getByTestId('fake-start-al-2')).toBeEnabled();

        // The action is done without having resolved the alert: usable again, alert still listed.
        fireEvent.click(screen.getByTestId('fake-finish-al-1'));
        await waitFor(() => expect(screen.getByTestId('fake-start-al-1')).toBeEnabled());
        expect(screen.getByTestId('mc-system-alert-al-1')).toBeInTheDocument();
    });
});

describe('card outline severity ladder', () => {
    // The outline (all four edges) is worst-of health + verdict. The two honesty pins:
    // an ASSURED verdict earns green ONLY once health is healthy —
    // "assured while still learning" shows the learning blue, never
    // green — and learning is distinct from gray (never-computed).
    const cases: Array<[string, Partial<MissionControlCard>, string]> = [
        ['healthy + assured is green',
            { health_state: 'healthy', verdict_status: 'assured' }, 'border-green-500'],
        ['assured while learning is BLUE, not green',
            { health_state: 'learning', verdict_status: 'assured' }, 'border-blue-400'],
        ['learning alone is blue',
            { health_state: 'learning', verdict_status: 'insufficient_evidence' }, 'border-blue-400'],
        ['assured with exceptions is yellow even when healthy',
            { health_state: 'healthy', verdict_status: 'assured_with_exceptions' }, 'border-yellow-500'],
        ['silent is orange regardless of verdict',
            { health_state: 'silent', verdict_status: 'assured' }, 'border-orange-500'],
        ['violations are red regardless of health',
            { health_state: 'healthy', verdict_status: 'violations' }, 'border-red-500'],
        ['never-computed stays gray',
            { health_state: null, verdict_status: null }, 'border-gray-300'],
    ];

    for (const [label, overrides, expected] of cases) {
        it(label, async () => {
            mockFetchCards.mockResolvedValue({
                cards: [card({ id: 'sys-stripe', ...overrides })],
                total: 1, window: '30d', parent_id: null, path: [],
            });
            await renderDashboard();
            const el = await screen.findByTestId('mc-card-sys-stripe');
            expect(el.className).toContain(expected);
            expect(el.className).toContain('border-2');
        });
    }
});

describe('card display metrics (tokens + data transfer)', () => {
    it('hidden by default — the card shows only cost and runs', async () => {
        await renderDashboard();
        await screen.findByTestId('mc-card-sys-copilot');
        expect(screen.queryByTestId('card-tokens-sys-copilot')).toBeNull();
        expect(screen.queryByTestId('card-data-sys-copilot')).toBeNull();
    });

    it('toggling tokens shows the formatted value and a violet trend line', async () => {
        await renderDashboard();
        await screen.findByTestId('mc-card-sys-copilot');
        fireEvent.click(screen.getByTestId('mc-display-button'));
        fireEvent.click(screen.getByTestId('mc-toggle-tokens'));
        expect(screen.getByTestId('card-tokens-sys-copilot')).toHaveTextContent('125.0k tok');
        expect(document.querySelector('[data-testid="spark-tokens"]')).not.toBeNull();
        // Preference persists for the next visit.
        expect(JSON.parse(localStorage.getItem('mc-card-metrics') || '{}').tokens).toBe(true);
    });

    it('toggling data transfer shows the formatted bytes and a teal line', async () => {
        await renderDashboard();
        await screen.findByTestId('mc-card-sys-copilot');
        fireEvent.click(screen.getByTestId('mc-display-button'));
        fireEvent.click(screen.getByTestId('mc-toggle-data'));
        expect(screen.getByTestId('card-data-sys-copilot')).toHaveTextContent('2.4 MB');
        expect(document.querySelector('[data-testid="spark-bytes"]')).not.toBeNull();
    });
});

describe('How Brutor learns', () => {
    it('opens from the header button and explains the ladder', async () => {
        await renderDashboard();
        fireEvent.click(await screen.findByTestId('mc-learn-button'));
        const page = await screen.findByTestId('mc-learn-page');
        expect(page).toHaveTextContent('How Brutor learns');
        // The two rules operators actually ask about:
        expect(page).toHaveTextContent(/watched, not yet proven/i);
        expect(page).toHaveTextContent(/Absence of evidence is never green/i);
        // Every rung of the ladder is documented.
        for (const rung of ['Red', 'Orange', 'Yellow', 'Blue', 'Green', 'Gray']) {
            expect(page).toHaveTextContent(rung);
        }
        fireEvent.click(screen.getByTestId('mc-learn-back'));
        expect(screen.queryByTestId('mc-learn-page')).toBeNull();
    });
});

describe('MissionControlDashboard — estate rollup exclusions', () => {
    it('shows an excluded system as excluded, with its reason, not hidden', async () => {
        mockFetchCards.mockResolvedValue(response([
            card({ id: 'sys-excused', name: 'Excused', excluded: true,
                   exclusion_reason: 'no_evidence_acknowledged',
                   exclusion_expires_at: '2026-12-01T00:00:00Z',
                   health_state: 'silent', verdict_status: 'violations' }),
        ]));
        await renderDashboard();
        const tile = screen.getByTestId('mc-card-sys-excused');
        expect(screen.getByTestId('mc-excluded-sys-excused')).toHaveTextContent('Excluded from rollup · no evidence acknowledged');
        // Its own judgement is still on the card — the parent just does not read it.
        expect(tile).toHaveTextContent(/silent/i);
    });

    it('a group card says how many systems its rollup leaves out', async () => {
        mockFetchCards.mockResolvedValue(response([
            card({ id: 'grp-org', name: 'Org', group_type: 'custom', is_group: true,
                   systems: 3, excluded_systems: 2, health_state: 'healthy', verdict_status: 'assured' }),
        ]));
        await renderDashboard();
        expect(screen.getByTestId('mc-excluded-count-grp-org')).toHaveTextContent('2 systems excluded from this rollup');
    });
});

// ─────────────────────────────────────────────────────────────────────────
// Unattributed to-do list + system groups
// ─────────────────────────────────────────────────────────────────────────

describe('unattributed breakdown and system groups', () => {
    const UA_WITH_GROUPS = card({
        ...PSEUDO_UNATTRIBUTED,
        runs: 20,
        unattributed_by_group: [
            { group_id: 'grp-e2e', group_name: 'E2ETests', runs: 9, spend: 0.5 },
            { group_id: null, group_name: null, runs: 5, spend: 0.1 },
            { group_id: 'grp-sales', group_name: 'Sales', runs: 3, spend: 0.02 },
            { group_id: 'grp-ops', group_name: 'Ops', runs: 2, spend: 0.01 },
            { group_id: 'grp-bench', group_name: 'benchmark', runs: 1, spend: 0.0 },
        ],
    });

    it('names the top 3 groups and folds the rest into "+N more"', async () => {
        mockFetchCards.mockResolvedValue(response([ORG_CARD, UA_WITH_GROUPS]));
        await renderDashboard();
        const list = screen.getByTestId('mc-unattributed-breakdown');
        expect(within(list).getByText('E2ETests')).toBeInTheDocument();
        expect(within(list).getByText('unknown group')).toBeInTheDocument();
        expect(within(list).getByText('Sales')).toBeInTheDocument();
        expect(within(list).queryByText('Ops')).toBeNull();
        expect(screen.getByTestId('mc-unattributed-more').textContent).toBe('+2 more');
    });

    it('a group name opens that group, not the AI Systems page', async () => {
        mockFetchCards.mockResolvedValue(response([ORG_CARD, UA_WITH_GROUPS]));
        const setActiveTab = await renderDashboard();
        fireEvent.click(screen.getByTestId('mc-unattributed-group-grp-e2e'));
        expect(setActiveTab).toHaveBeenCalledWith('groups:grp-e2e');
        expect(setActiveTab).not.toHaveBeenCalledWith('ai-systems');
    });

    it('labels system groups "system" and shows a purpose when there is one', async () => {
        mockFetchCards.mockResolvedValue(response([
            card({ ...CUSTOM_LEAF_CARD, is_system: true }),
            card({
                ...CUSTOM_LEAF_CARD, id: 'grp-unbound', name: 'Unbound', is_system: true,
                purpose: 'Platform calls: health checks, judges, embeddings',
            }),
        ]));
        await renderDashboard();
        const def = screen.getByTestId('mc-card-grp-default');
        expect(def.textContent).toContain('system');
        expect(def.textContent).not.toContain('custom');
        expect(screen.queryByTestId('mc-purpose-grp-default')).toBeNull();
        expect(screen.getByTestId('mc-purpose-grp-unbound').textContent).toBe(
            'Platform calls: health checks, judges, embeddings',
        );
    });
});
