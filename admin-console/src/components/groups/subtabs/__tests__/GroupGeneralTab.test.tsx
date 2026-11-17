/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The General tab's sub-tab structure: Identity /
 * Ownership & Purpose / Hierarchy & Inheritance / Audit Info.
 *
 * The load-bearing assertions:
 *   * every field is reachable under one of the sub-tabs;
 *   * the audit sub-tab is the audit panel WITH the version field;
 *   * edit mode spans the form sub-tabs — a draft survives switching
 *     sub-tabs and Save writes the whole form;
 *   * the audit sub-tab hides the form's Edit/Save (it has its own
 *     "Save version").
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockUpdateResourceGroup = vi.fn();
const mockDeleteResourceGroup = vi.fn();
const mockFetchAISystems = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({
        updateResourceGroup: mockUpdateResourceGroup,
        deleteResourceGroup: mockDeleteResourceGroup,
        fetchAISystems: mockFetchAISystems,
    }),
}));
vi.mock('../../../../hooks/useTheme', () => ({
    useTheme: () => ({ theme: 'light' }),
}));
vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));
vi.mock('../../../common/LucideIconPicker', () => ({
    default: () => <div data-testid="icon-picker" />,
}));
vi.mock('../../../common/IconColorPicker', () => ({
    default: () => <div data-testid="color-picker" />,
}));
vi.mock('../MoveGroupModal', () => ({
    default: () => <div data-testid="move-modal" />,
}));

import GroupGeneralTab from '../GroupGeneralTab';
import { GroupType, SystemKind, ResourceGroup } from '../../../../lib/groupTypes';

function group(overrides: Partial<ResourceGroup> = {}): ResourceGroup {
    return {
        id: 'group-1',
        tenant_id: 't1',
        name: 'acme-revenue-copilot',
        display_name: 'ACME Revenue Copilot',
        description: 'The Revenue Copilot agent system.',
        group_type: GroupType.AI_SYSTEM,
        system_kind: SystemKind.AGENT,
        is_active: true,
        hierarchy_level: 1,
        parent_group_id: 'group-parent',
        parent_group_name: 'acme-corp',
        hierarchy_path: 'acme-corp/acme-revenue-copilot',
        inherit_resources: false,
        owner: 'Maria Lindqvist',
        lifecycle_stage: 'active',
        intended_use: 'Answers revenue questions for the sales org.',
        intended_client_labels: ['Goose', 'Portal'],
        intended_client_systems: ['sys-demo'],
        eu_ai_act_role: 'provider_and_deployer',
        version: '2.1',
        created_at: '2026-08-14T07:19:00Z',
        created_by: 'tenant-admin-1',
        updated_at: '2026-08-14T07:32:00Z',
        updated_by: 'admin',
        ...overrides,
    } as unknown as ResourceGroup;
}

const renderTab = (g = group()) => {
    const onUpdate = vi.fn();
    const onDelete = vi.fn();
    render(<GroupGeneralTab group={g} onUpdate={onUpdate} onDelete={onDelete} />);
    return { onUpdate, onDelete };
};

/** The PATCH body from the first save of this test. */
const savedPayload = (): Record<string, unknown> =>
    mockUpdateResourceGroup.mock.calls[0][1] as Record<string, unknown>;

beforeEach(() => {
    vi.clearAllMocks();
    mockUpdateResourceGroup.mockResolvedValue({});
    mockFetchAISystems.mockResolvedValue({
        systems: [
            { id: 'group-1', name: 'acme-revenue-copilot', display_name: 'ACME Revenue Copilot' },
            { id: 'sys-demo', name: 'brutor-demo', display_name: 'Brutor Demo System' },
            { id: 'sys-intake', name: 'claims-intake', display_name: 'Claims Intake' },
        ],
        stats: {},
        total: 3,
        limit: 500,
        offset: 0,
    });
});

describe('sub-tab structure', () => {
    it('renders four sub-tabs with Identity first', () => {
        renderTab();
        const tabs = screen.getAllByRole('tab');
        expect(tabs.map((t) => t.textContent)).toEqual([
            'Identity',
            'Ownership & Purpose',
            'Hierarchy & Inheritance',
            'Audit Info',
        ]);
        expect(screen.getByTestId('general-subtab-identity')).toHaveAttribute('aria-selected', 'true');
    });

    it('Identity carries id, type, kind, name and status', () => {
        renderTab();
        expect(screen.getByDisplayValue('group-1')).toBeInTheDocument();
        expect(screen.getByText('AI System')).toBeInTheDocument();
        expect(screen.getByTestId('system-kind-field')).toBeInTheDocument();
        expect(screen.getByText('acme-revenue-copilot')).toBeInTheDocument();
        expect(screen.getByText('Active')).toBeInTheDocument();
        // Ownership fields do NOT bleed into Identity.
        expect(screen.queryByText('Maria Lindqvist')).toBeNull();
    });

    it('Ownership & Purpose carries the governance declarations', () => {
        renderTab();
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByText('Maria Lindqvist')).toBeInTheDocument();
        expect(screen.getByText('Answers revenue questions for the sales org.')).toBeInTheDocument();
        expect(screen.getByText('Goose, Portal')).toBeInTheDocument();
    });

    it('Hierarchy & Inheritance carries level, path and the inheritance state', () => {
        renderTab();
        fireEvent.click(screen.getByTestId('general-subtab-hierarchy'));
        expect(screen.getByText('Level 1')).toBeInTheDocument();
        expect(screen.getByText('acme-corp/acme-revenue-copilot')).toBeInTheDocument();
        expect(screen.getByText('Disabled')).toBeInTheDocument();
        expect(screen.getByText('Governance always inherits')).toBeInTheDocument();
        expect(screen.queryByText('Root Group')).toBeNull();
    });

    it('a root group shows the Root Group card instead of a breadcrumb', () => {
        renderTab(group({ parent_group_id: undefined, parent_group_name: undefined, hierarchy_level: 0 }));
        fireEvent.click(screen.getByTestId('general-subtab-hierarchy'));
        expect(screen.getByText('Level 0')).toBeInTheDocument();
        expect(screen.getByText('Root Group')).toBeInTheDocument();
    });
});

describe('audit sub-tab', () => {
    it('shows provenance AND the version field from the shared panel', () => {
        renderTab();
        fireEvent.click(screen.getByTestId('general-subtab-audit'));
        expect(screen.getByTestId('group-audit-tab')).toBeInTheDocument();
        expect(screen.getByDisplayValue('2.1')).toBeInTheDocument();
        expect(screen.getByTestId('group-audit-save')).toBeInTheDocument();
    });

    it('hides the form Edit/Save actions — version has its own save', () => {
        renderTab();
        expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('general-subtab-audit'));
        expect(screen.queryByRole('button', { name: /^edit$/i })).toBeNull();
        expect(screen.queryByRole('button', { name: /delete/i })).toBeNull();
    });

    it('saving a version edit PATCHes the group', async () => {
        const { onUpdate } = renderTab();
        fireEvent.click(screen.getByTestId('general-subtab-audit'));
        fireEvent.change(screen.getByDisplayValue('2.1'), { target: { value: '2.2' } });
        fireEvent.click(screen.getByTestId('group-audit-save'));
        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('group-1', { version: '2.2' }),
        );
        expect(onUpdate).toHaveBeenCalled();
    });
});

describe('edit mode across sub-tabs', () => {
    it('a draft survives switching sub-tabs and Save writes the whole form', async () => {
        const { onUpdate } = renderTab();
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));

        // Edit the name on Identity…
        fireEvent.change(screen.getByDisplayValue('acme-revenue-copilot'), {
            target: { value: 'revenue-copilot-v2' },
        });

        // …switch to Ownership, edit the owner…
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        fireEvent.change(screen.getByDisplayValue('Maria Lindqvist'), {
            target: { value: 'Revenue Ops' },
        });

        // …back to Identity: the draft name is still there.
        fireEvent.click(screen.getByTestId('general-subtab-identity'));
        expect(screen.getByDisplayValue('revenue-copilot-v2')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload()).toMatchObject({
            name: 'revenue-copilot-v2',
            owner: 'Revenue Ops',
        });
        expect(onUpdate).toHaveBeenCalled();
    });

    it('cancel restores the group values', () => {
        renderTab();
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.change(screen.getByDisplayValue('acme-revenue-copilot'), {
            target: { value: 'scratch' },
        });
        fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
        expect(screen.getByText('acme-revenue-copilot')).toBeInTheDocument();
    });
});


describe('EU AI Act role', () => {
    it('Ownership & Purpose shows the declared regulated role', () => {
        renderTab();
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByTestId('eu-ai-act-role-field')).toHaveTextContent('Provider & Deployer');
    });

    it('an undeclared role reads as a dash, never a default', () => {
        renderTab(group({ eu_ai_act_role: null }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByTestId('eu-ai-act-role-field')).toHaveTextContent('-');
    });

    it('saving a role change PATCHes it and Art 25 guidance shows while editing', async () => {
        renderTab();
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByText(/Art 25/)).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('EU AI Act Role'), {
            target: { value: 'deployer' },
        });
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload().eu_ai_act_role).toBe('deployer');
    });

    it('clearing the role sends an explicit null', async () => {
        renderTab();
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        fireEvent.change(screen.getByLabelText('EU AI Act Role'), {
            target: { value: '' },
        });
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload().eu_ai_act_role).toBeNull();
    });
});

describe('GroupGeneralTab — estate rollup exclusion', () => {
    it('reads back an existing exclusion with its reason and expiry', () => {
        renderTab(group({
            rollup_exclusion: {
                reason: 'no_evidence_acknowledged', note: 'onboarding Q4',
                expires_at: '2026-12-01T00:00:00Z', by: 'admin', at: '2026-09-01T00:00:00Z',
            },
        }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        const block = screen.getByTestId('rollup-exclusion');
        expect(block).toHaveTextContent('Excluded — No evidence yet — acknowledged');
        expect(block).toHaveTextContent('onboarding Q4');
    });

    it('sends the exclusion with the save, and null when switched off', async () => {
        renderTab(group({ rollup_exclusion: null }));
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        fireEvent.click(screen.getByTestId('rollup-exclusion-toggle'));
        fireEvent.change(screen.getByTestId('rollup-exclusion-reason'), { target: { value: 'scheduled_offline' } });
        fireEvent.change(screen.getByTestId('rollup-exclusion-note'), { target: { value: 'DC move' } });
        fireEvent.change(screen.getByTestId('rollup-exclusion-expires'), { target: { value: '2026-10-15' } });
        // Temporary reasons say so on the face of the form.
        expect(screen.getByTestId('rollup-exclusion')).toHaveTextContent('Expiry required');

        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload().rollup_exclusion).toEqual({
            reason: 'scheduled_offline', note: 'DC move', expires_at: '2026-10-15T00:00:00Z',
        });

        // Off again → explicit null on the wire (model_fields_set semantics).
        // Save leaves edit mode, so re-enter it first.
        mockUpdateResourceGroup.mockClear();
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        fireEvent.click(screen.getByTestId('rollup-exclusion-toggle'));
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload().rollup_exclusion).toBeNull();
    });

    it('is not offered on a plain group', () => {
        renderTab(group({ group_type: GroupType.CUSTOM }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.queryByTestId('rollup-exclusion')).toBeNull();
    });
});


// Intended clients are typed: labels for non-system callers,
// AI System ids for systems that may call this one as a shared service. There
// is no `intended_clients` key (no alias), so a save must never send it.
describe('GroupGeneralTab — intended clients', () => {
    it('reads back labels and resolves client system ids to names', async () => {
        renderTab();
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByTestId('intended-client-labels-field')).toHaveTextContent('Goose, Portal');
        await waitFor(() =>
            expect(screen.getByTestId('intended-client-systems-value')).toHaveTextContent('Brutor Demo System'),
        );
    });

    it('the picker lists other AI Systems (not itself) and saves both typed fields', async () => {
        renderTab();
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        await screen.findByTestId('intended-client-system-sys-intake');
        expect(screen.queryByTestId('intended-client-system-group-1')).toBeNull();
        expect(screen.getByTestId('intended-client-system-sys-demo')).toBeChecked();

        fireEvent.click(screen.getByTestId('intended-client-system-sys-intake'));
        fireEvent.change(screen.getByLabelText('Intended Client Labels'), {
            target: { value: 'Goose,  Claude Code ' },
        });
        fireEvent.click(screen.getByRole('button', { name: /save/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());

        const body = savedPayload();
        expect(body.intended_client_labels).toEqual(['Goose', 'Claude Code']);
        expect(body.intended_client_systems).toEqual(['sys-demo', 'sys-intake']);
        expect(body).not.toHaveProperty('intended_clients');
    });

    it('a non-AI-System group has no systems picker and clears the field', async () => {
        renderTab(group({ group_type: GroupType.CUSTOM, intended_client_systems: null }));
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.queryByTestId('intended-client-systems-field')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: /save/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload().intended_client_systems).toBeNull();
    });
});

// Per service, the accepted-caller list can be
// ENFORCED at the gateway. AI Systems only: the control plane 422s the flag
// on any other group type, so a save must never send it there.
describe('GroupGeneralTab — enforce accepted callers', () => {
    it('reads back enforced vs advisory, next to the client systems picker', () => {
        renderTab(group({ enforce_accepted_callers: true }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByTestId('enforce-accepted-callers-value')).toHaveTextContent('Enforced — only listed callers');
        expect(screen.getByTestId('enforce-accepted-callers-field')).toHaveTextContent('HTTP 403 (unaccepted_caller)');
    });

    it('an unset flag reads advisory — never enforced by default', () => {
        renderTab(group());
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByTestId('enforce-accepted-callers-value')).toHaveTextContent('Advisory');
    });

    it('toggling it on sends enforce_accepted_callers=true with the save', async () => {
        renderTab(group({ enforce_accepted_callers: false }));
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        const box = screen.getByTestId('enforce-accepted-callers');
        expect(box).not.toBeChecked();
        fireEvent.click(box);
        fireEvent.click(screen.getByRole('button', { name: /save/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload().enforce_accepted_callers).toBe(true);
    });

    it('warns when enforcement is on with no listed callers (every caller refused)', () => {
        renderTab(group({ enforce_accepted_callers: true, intended_client_systems: [] }));
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.getByTestId('enforce-accepted-callers-empty')).toBeInTheDocument();
    });

    it('a non-AI-System group has no toggle and never sends the flag', async () => {
        renderTab(group({ group_type: GroupType.CUSTOM, intended_client_systems: null }));
        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByTestId('general-subtab-ownership'));
        expect(screen.queryByTestId('enforce-accepted-callers-field')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: /save/i }));
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(savedPayload()).not.toHaveProperty('enforce_accepted_callers');
    });
});
