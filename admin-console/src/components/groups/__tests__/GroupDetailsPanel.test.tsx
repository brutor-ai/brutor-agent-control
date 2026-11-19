/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The group detail panel — seven top-level tabs, two of which are parents
 * with their own strips (Resources and Governance).
 *
 * The behaviours worth defending:
 *
 *   - THE REMEMBERED TAB. The tab and the two sub-tabs are remembered
 *     each under its own key; a stored id that names no tab of the panel
 *     is ignored, and the panel opens on General.
 *   - A DEEP LINK BEATS THE PERSISTED TAB. "Open usage details" from Mission
 *     Control has to land on Usage even when the operator last left the
 *     panel on Members.
 *   - THE ALERT BADGE. A hard stop means traffic is already being refused;
 *     it must outrank warnings in the badge rather than being summed with
 *     them into one ambiguous count.
 *   - THE POLICY BANNER. A group under a policy bundle can still be edited
 *     here, and the operator has to be told the bundle will need re-syncing.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../test/groups/groupDetailsPanel'));
vi.mock('../../../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../../../hooks/useAuth', async () => (await shared()).useAuthModule());
vi.mock('../../../context/TenantThemeContext', async () => (await shared()).tenantThemeModule());
vi.mock('../subtabs/GroupGeneralTab', async () => (await shared()).generalTabModule());
vi.mock('../subtabs/GroupUsageTab', async () => (await shared()).paneModule('usage'));
vi.mock('../subtabs/GroupMembersTab', async () => (await shared()).paneModule('members'));
vi.mock('../subtabs/GroupAPIKeysTab', async () => (await shared()).paneModule('api-keys'));
vi.mock('../subtabs/GroupComplianceTab', async () => (await shared()).paneModule('compliance'));
vi.mock('../subtabs/GroupGuardrailsPanel', async () => (await shared()).paneModule('guardrails'));
vi.mock('../subtabs/GroupResourceLimitsTab', async () => (await shared()).paneModule('limits'));
vi.mock('../subtabs/GroupResourceGovernanceTab', async () => (await shared()).paneModule('policy'));
vi.mock('../subtabs/GroupIdentityTab', async () => (await shared()).paneModule('identity'));
vi.mock('../subtabs/GroupLLModelsTab', async () => (await shared()).paneModule('models'));
vi.mock('../subtabs/GroupMCPServersTab', async () => (await shared()).paneModule('mcp'));
vi.mock('../subtabs/GroupAgentSkillsTab', async () => (await shared()).paneModule('skills'));
vi.mock('../subtabs/GroupAgentsTab', async () => (await shared()).paneModule('agents'));

import GroupDetailsPanel from '../GroupDetailsPanel';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import {
    group,
    mockAcknowledgeUsageAlert,
    mockFetchGroupAlerts,
    mockListPolicyBundles,
    resetGroupDetailsPanel,
} from '../../../test/groups/groupDetailsPanel';
import { SlotProvider } from '../../../app/SlotProvider';
import type { SlotRegistry } from '../../../app/slots';
import type { ResourceGroup, UsageAlertInfo } from '../../../lib/groupTypes';

function alert(overrides: Partial<UsageAlertInfo> = {}): UsageAlertInfo {
    return {
        id: 'al-1',
        alert_type: 'warning',
        resource_type: 'llm',
        metric: 'daily_budget_usd',
        utilization_pct: 82,
        period: 'today',
        ...overrides,
    } as UsageAlertInfo;
}

function renderPanel(props: Partial<React.ComponentProps<typeof GroupDetailsPanel>> = {}) {
    const onUpdate = vi.fn();
    const onDelete = vi.fn();
    const view = render(
        <GroupDetailsPanel group={group()} onUpdate={onUpdate} onDelete={onDelete} {...props} />,
        { wrapper: WithBuiltinSlots },
    );
    return { onUpdate, onDelete, ...view };
}

beforeEach(resetGroupDetailsPanel);

describe('GroupDetailsPanel — the header', () => {
    it('names the group with its type and its place in the estate', () => {
        renderPanel();
        expect(screen.getByText(/Resource Group - Engineering/)).toBeInTheDocument();
        expect(screen.getByText('(Department)')).toBeInTheDocument();
        expect(screen.getByText('acme/engineering')).toBeInTheDocument();
    });

    it('states whether the group is active', () => {
        const { rerender } = renderPanel();
        expect(screen.getByText('Active')).toBeInTheDocument();
        rerender(
            <GroupDetailsPanel
                group={group({ is_active: false })}
                onUpdate={vi.fn()}
                onDelete={vi.fn()}
            />,
        );
        expect(screen.getByText('Inactive')).toBeInTheDocument();
    });
});

describe('GroupDetailsPanel — tab navigation', () => {

    it('switches panes and remembers the choice for next time', () => {
        const { unmount } = renderPanel();
        fireEvent.click(screen.getByText('Members'));
        expect(screen.getByTestId('pane-members')).toBeInTheDocument();
        expect(screen.queryByTestId('pane-general')).toBeNull();
        unmount();

        renderPanel();
        expect(screen.getByTestId('pane-members')).toBeInTheDocument();
    });

    it('a deep link beats the tab the operator left the panel on', () => {
        localStorage.setItem('groupDetailsActiveTab', 'members');
        renderPanel({ initialTab: 'usage' });
        expect(screen.getByTestId('pane-usage')).toBeInTheDocument();
    });

    it('passes a General sub-section deep link straight through', () => {
        renderPanel({ initialGeneralSection: 'ownership' });
        expect(screen.getByTestId('pane-general')).toHaveTextContent('ownership');
    });

    it('a persisted tab we no longer offer falls back to General', () => {
        localStorage.setItem('groupDetailsActiveTab', 'atlantis');
        renderPanel();
        expect(screen.getByTestId('pane-general')).toBeInTheDocument();
    });
});

describe('GroupDetailsPanel — the Governance strip', () => {
    it('opens on Compliance and offers the posture pipeline in order', () => {
        renderPanel();
        fireEvent.click(screen.getByText('Governance'));
        expect(screen.getByTestId('pane-compliance')).toBeInTheDocument();
        for (const label of ['Compliance', 'Guardrails', 'Limits', 'Policy']) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
    });

    it('switches between the four governance panes', () => {
        renderPanel();
        fireEvent.click(screen.getByText('Governance'));
        fireEvent.click(screen.getByText('Guardrails'));
        expect(screen.getByTestId('pane-guardrails')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Limits'));
        expect(screen.getByTestId('pane-limits')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Policy'));
        expect(screen.getByTestId('pane-policy')).toBeInTheDocument();
    });

    it('offers Identity: what credential a caller must present', () => {
        renderPanel();
        fireEvent.click(screen.getByText('Governance'));
        fireEvent.click(screen.getByText('Identity'));
        expect(screen.getByTestId('pane-identity')).toBeInTheDocument();
    });

    it('opens Governance on the remembered sub-tab', () => {
        localStorage.setItem('groupDetailsActiveTab', 'governance');
        localStorage.setItem('groupDetailsGovernanceSubTab', 'usage-limits');
        renderPanel();
        expect(screen.getByTestId('pane-limits')).toBeInTheDocument();
    });

    it.each(['guardrails', 'resource-limits', 'resource-governance', 'usage-limits'])(
        'a stored tab "%s" names no tab of the panel: General, and Compliance under Governance',
        (stored) => {
            localStorage.setItem('groupDetailsActiveTab', stored);
            renderPanel();
            expect(screen.getByTestId('pane-general')).toBeInTheDocument();
            fireEvent.click(screen.getByText('Governance'));
            expect(screen.getByTestId('pane-compliance')).toBeInTheDocument();
        },
    );
});

describe('GroupDetailsPanel — the Resources strip', () => {

    it('opens Resources on the remembered sub-tab', () => {
        localStorage.setItem('groupDetailsActiveTab', 'resources');
        localStorage.setItem('groupDetailsResourceSubTab', 'skills');
        renderPanel();
        expect(screen.getByTestId('pane-skills')).toBeInTheDocument();
    });

    it.each(['ai-models', 'mcp-servers', 'agent-skills', 'agents', 'mcp'])(
        'a stored tab "%s" names no tab of the panel: General, and AI Models under Resources',
        (stored) => {
            localStorage.setItem('groupDetailsActiveTab', stored);
            renderPanel();
            expect(screen.getByTestId('pane-general')).toBeInTheDocument();
            fireEvent.click(screen.getByText('Resources'));
            expect(screen.getByTestId('pane-models')).toBeInTheDocument();
        },
    );
});

describe('GroupDetailsPanel — usage alerts', () => {
    it('shows no badge when nothing is alerting', async () => {
        renderPanel();
        await waitFor(() => expect(mockFetchGroupAlerts).toHaveBeenCalledWith('rg-1', { status: 'active' }));
        expect(screen.queryByText(/warning/)).toBeNull();
        expect(screen.queryByText(/blocked/)).toBeNull();
    });

    it('counts warnings when nothing is blocked', async () => {
        mockFetchGroupAlerts.mockResolvedValue({
            alerts: [alert(), alert({ id: 'al-2' })],
        });
        renderPanel();
        expect(await screen.findByText('2 warnings')).toBeInTheDocument();
    });

    it('a hard stop outranks the warnings — traffic is already being refused', async () => {
        mockFetchGroupAlerts.mockResolvedValue({
            alerts: [alert(), alert({ id: 'al-2', alert_type: 'hard_stop', utilization_pct: 100 })],
        });
        renderPanel();
        expect(await screen.findByText('1 blocked')).toBeInTheDocument();
        expect(screen.queryByText(/warning/)).toBeNull();
    });

    it('the dropdown spells out each alert and how far over it is', async () => {
        mockFetchGroupAlerts.mockResolvedValue({ alerts: [alert()] });
        renderPanel();
        fireEvent.click(await screen.findByText('1 warning'));
        expect(screen.getByText('Usage Alerts')).toBeInTheDocument();
        expect(screen.getByText('WARNING')).toBeInTheDocument();
        expect(screen.getByText('daily budget usd — 82% used')).toBeInTheDocument();
        expect(screen.getByText('today')).toBeInTheDocument();
    });

    it('acknowledging an alert removes it from the badge', async () => {
        mockFetchGroupAlerts.mockResolvedValue({
            alerts: [alert(), alert({ id: 'al-2' })],
        });
        renderPanel();
        fireEvent.click(await screen.findByText('2 warnings'));
        fireEvent.click(screen.getAllByTitle('Acknowledge')[0]);

        await waitFor(() => expect(mockAcknowledgeUsageAlert).toHaveBeenCalledWith('al-1'));
        await waitFor(() => expect(screen.getByText('1 warning')).toBeInTheDocument());
    });

    it('an alerts endpoint that fails is not "nothing is alerting": the header says the alerts could not be loaded', async () => {
        mockFetchGroupAlerts.mockRejectedValue(new Error('offline'));
        renderPanel();
        expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the alerts of this group: offline');
        expect(screen.getByText(/Resource Group - Engineering/)).toBeInTheDocument();
        expect(screen.queryByText(/warning/)).toBeNull();

        mockFetchGroupAlerts.mockResolvedValue({ alerts: [] });
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    });
});

// ── The `resource-group.tabs` and `resource-group.resource-tabs` slots ───────

/** The labels of the tab strip that holds `label`, in order. */
function stripOf(label: string): string[] {
    const strip = screen.getByText(label).closest('button')!.parentElement!;
    return Array.from(strip.querySelectorAll('button')).map((b) => b.textContent ?? '');
}

describe('GroupDetailsPanel — with nothing contributed', () => {
    const renderBare = () =>
        render(<GroupDetailsPanel group={group()} onUpdate={vi.fn()} onDelete={vi.fn()} />);

    it('shows its own tabs and works', () => {
        renderBare();
        expect(stripOf('General')).toEqual(['General', 'Usage', 'Resources', 'Members', 'API Keys', 'Governance']);
        fireEvent.click(screen.getByText('Resources'));
        expect(stripOf('AI Models')).toEqual(['AI Models', 'MCP Servers', 'Skills', 'Agents']);
        expect(screen.getByTestId('pane-models')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Members'));
        expect(screen.getByTestId('pane-members')).toBeInTheDocument();
    });

    it('asks nothing about policy bundles and shows no policy banner', async () => {
        mockListPolicyBundles.mockResolvedValue({ bundles: [{ id: 'b-1', name: 'acme-baseline', version: '3' }] });
        renderBare();
        await waitFor(() => expect(mockFetchGroupAlerts).toHaveBeenCalled());
        expect(mockListPolicyBundles).not.toHaveBeenCalled();
        expect(screen.queryByText(/Managed by policy/)).toBeNull();
    });

    it('a saved tab that nothing contributes any more falls back to General', () => {
        localStorage.setItem('groupDetailsActiveTab', 'cache');
        renderBare();
        expect(screen.getByTestId('pane-general')).toBeInTheDocument();
    });

    it('a saved sub-tab that nothing contributes is an unknown id: AI Models under Resources', () => {
        localStorage.setItem('groupDetailsResourceSubTab', 'kb');
        renderBare();
        fireEvent.click(screen.getByText('Resources'));
        expect(screen.getByTestId('pane-models')).toBeInTheDocument();
    });
});

// The panel's own part of what `enterprise/src/host/.../GroupDetailsPanel.test.tsx`
// shows with the Enterprise tabs on it (Cache stats, Knowledge Base, the
// policy banner), here with stand-ins the test provides itself.
describe('GroupDetailsPanel — with stand-in items in its slots', () => {
    const Icon = () => null;
    /** What each stand-in was last rendered with, by its id. */
    const given = new Map<string, Record<string, unknown>>();
    const pane = (id: string) => (props: Record<string, unknown>) => {
        given.set(id, props);
        return <div data-testid={`pane-${id}`} />;
    };
    const SLOTS: SlotRegistry = {
        'resource-group.tabs': [
            { id: 'fake-last', label: 'Fake last', icon: Icon, render: pane('fake-last') },
            { id: 'fake-mid', label: 'Fake mid', icon: Icon, after: 'resources', render: pane('fake-mid') },
        ],
        'resource-group.resource-tabs': [
            { id: 'fake-sub', label: 'Fake sub', icon: Icon, render: pane('fake-sub') },
            { id: 'fake-sub-mid', label: 'Fake sub mid', icon: Icon, after: 'models', render: pane('fake-sub-mid') },
        ],
        'resource-group.header': [
            {
                id: 'fake-banner',
                render: ({ group: shown }: { group: ResourceGroup }) => <div data-testid="fake-banner">{shown.id}</div>,
            },
        ],
    };

    function renderWithStandIns(props: Partial<React.ComponentProps<typeof GroupDetailsPanel>> = {}) {
        const shown = group();
        const onUpdate = vi.fn();
        const view = render(
            <SlotProvider slots={SLOTS}>
                <GroupDetailsPanel group={shown} onUpdate={onUpdate} onDelete={vi.fn()} {...props} />
            </SlotProvider>,
        );
        return { shown, onUpdate, ...view };
    }

    beforeEach(() => given.clear());

    it('places a contributed tab after the tab it names, or last, in both strips', () => {
        renderWithStandIns();
        // Still opens on General, with its own tabs where they were.
        expect(screen.getByTestId('pane-general')).toBeInTheDocument();
        expect(stripOf('General')).toEqual([
            'General', 'Usage', 'Resources', 'Fake mid', 'Members', 'API Keys', 'Governance', 'Fake last',
        ]);
        fireEvent.click(screen.getByText('Resources'));
        // Resources still opens on AI Models.
        expect(screen.getByTestId('pane-models')).toBeInTheDocument();
        expect(stripOf('AI Models')).toEqual(['AI Models', 'Fake sub mid', 'MCP Servers', 'Skills', 'Agents', 'Fake sub']);
    });

    it('a contributed top-level tab replaces the pane shown and is given the group and the update callback', () => {
        const { shown, onUpdate } = renderWithStandIns();
        expect(given.has('fake-mid')).toBe(false);
        fireEvent.click(screen.getByText('Fake mid'));
        expect(screen.getByTestId('pane-fake-mid')).toBeInTheDocument();
        expect(screen.queryByTestId('pane-general')).toBeNull();
        const props = given.get('fake-mid')!;
        expect(Object.keys(props).sort()).toEqual(['group', 'onUpdate']);
        expect(props.group).toBe(shown);
        expect(props.onUpdate).toBe(onUpdate);
        // The other contributed tab was not rendered.
        expect(given.has('fake-last')).toBe(false);
    });

    it('a contributed Resources sub-tab is given the group, and nothing else', () => {
        const { shown } = renderWithStandIns();
        fireEvent.click(screen.getByText('Resources'));
        fireEvent.click(screen.getByText('Fake sub'));
        expect(screen.getByTestId('pane-fake-sub')).toBeInTheDocument();
        expect(screen.queryByTestId('pane-models')).toBeNull();
        const props = given.get('fake-sub')!;
        expect(Object.keys(props)).toEqual(['group']);
        expect(props.group).toBe(shown);
    });

    it('remembers a contributed top-level tab for next time', () => {
        const { unmount } = renderWithStandIns();
        fireEvent.click(screen.getByText('Fake last'));
        expect(localStorage.getItem('groupDetailsActiveTab')).toBe('fake-last');
        unmount();

        renderWithStandIns();
        expect(screen.getByTestId('pane-fake-last')).toBeInTheDocument();
    });

    it('remembers a contributed Resources sub-tab for next time', () => {
        const { unmount } = renderWithStandIns();
        fireEvent.click(screen.getByText('Resources'));
        fireEvent.click(screen.getByText('Fake sub'));
        expect(screen.getByTestId('pane-fake-sub')).toBeInTheDocument();
        unmount();

        renderWithStandIns();
        fireEvent.click(screen.getByText('Resources'));
        expect(screen.getByTestId('pane-fake-sub')).toBeInTheDocument();
    });

    it('a deep link to a contributed tab beats the tab the operator left the panel on', () => {
        localStorage.setItem('groupDetailsActiveTab', 'members');
        renderWithStandIns({ initialTab: 'fake-mid' });
        expect(screen.getByTestId('pane-fake-mid')).toBeInTheDocument();
    });

    it('shows a contributed header item between the header and the tab strip, given the group', () => {
        renderWithStandIns();
        const banner = screen.getByTestId('fake-banner');
        expect(banner).toHaveTextContent('rg-1');
        const title = screen.getByText(/Resource Group - Engineering/);
        const firstTab = screen.getByText('General');
        expect(title.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(banner.compareDocumentPosition(firstTab) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        // It stays whatever tab is shown.
        fireEvent.click(screen.getByText('Members'));
        expect(screen.getByTestId('fake-banner')).toBeInTheDocument();
    });
});
