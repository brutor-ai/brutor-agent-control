/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Lifecycle tab. The gate shows its whole checklist before anything is
 * clicked; the stage rail marks where the system is; every unmet requirement
 * with one fix offers the button that performs it — and the buttons that
 * hand off elsewhere say where they went.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockLifecycle = vi.fn();
const mockContracts = vi.fn();
const mockMint = vi.fn();
const mockApprove = vi.fn();
const mockTransition = vi.fn();
const mockUpdateGroup = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemLifecycle: mockLifecycle,
        fetchAISystemContracts: mockContracts,
        mintAISystemContract: mockMint,
        approveContract: mockApprove,
        transitionAISystemLifecycle: mockTransition,
        updateResourceGroup: mockUpdateGroup,
    }),
}));

vi.mock('../../../hooks/useAdminUsername', () => ({
    useAdminUsername: () => 'admin',
}));

import LifecyclePanel from '../LifecyclePanel';
import { GatewayAPIError } from '../../../lib/gatewayApi/base';
import type { LifecycleResponse, TransitionOption } from '../../../lib/aiSystemTypes';

function req(key: string, label: string, satisfied: boolean, detail?: string) {
    return { key, label, satisfied, detail };
}

function option(to_stage: string, requirements: ReturnType<typeof req>[]): TransitionOption {
    const unmet = requirements.filter((r) => !r.satisfied);
    return { to_stage, allowed: unmet.length === 0, requirements, unmet };
}

function lifecycle(overrides: Partial<LifecycleResponse> = {}): LifecycleResponse {
    return {
        ai_system_id: 'sys-1',
        current_stage: 'proposed',
        transitions: [
            option('approved', [
                req('contract_minted', 'A contract has been minted', true),
                req('owner_set', 'An accountable owner is named', false, 'No owner set on this AI System'),
                req('intended_use_set', 'Intended use is described', false),
            ]),
        ],
        ...overrides,
    };
}

describe('LifecyclePanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockLifecycle.mockResolvedValue(lifecycle());
        mockContracts.mockResolvedValue({ contracts: [], total: 0, active_version: null, config_drifted: false });
        mockMint.mockResolvedValue({ created: true, contract: { id: 'ctr-2', version: 2 } });
        mockApprove.mockResolvedValue({});
        mockTransition.mockResolvedValue({});
        mockUpdateGroup.mockResolvedValue({});
    });

    // -- the rail -----------------------------------------------------------

    it('marks the stage the system is in, and only that one', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await screen.findByTestId('stage-diagram');
        expect(screen.getByTestId('stage-proposed')).toHaveAttribute('data-current', 'true');
        for (const s of ['approved', 'active', 'deprecated', 'retired']) {
            expect(screen.getByTestId(`stage-${s}`)).toHaveAttribute('data-current', 'false');
        }
        expect(screen.getByTestId('gate-approved')).toHaveAttribute('data-live', 'true');
        expect(screen.getByTestId('gate-active')).toHaveAttribute('data-live', 'false');
    });

    it('shows the live gate with each requirement met or unmet, and a button per fix', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        const panel = await screen.findByTestId('gate-panel-approved');
        expect(panel).toHaveTextContent('2 requirements outstanding');
        expect(screen.getByTestId('gate-req-contract_minted')).toBeInTheDocument();
        // A met requirement offers nothing to do.
        expect(screen.queryByTestId('gate-action-contract_minted')).not.toBeInTheDocument();
        expect(screen.getByTestId('gate-action-owner_set')).toHaveTextContent('Set owner');
        expect(screen.getByTestId('gate-action-intended_use_set')).toHaveTextContent('Describe intended use');
    });

    it('draws the EU AI Act floor as applying on a high-risk system', async () => {
        mockLifecycle.mockResolvedValue(lifecycle({ eu_ai_act_risk_tier: 'high' }));
        render(<LifecyclePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('high-risk-floor')).toHaveAttribute('data-applies', 'true');
        expect(screen.getByTestId('high-risk-badge')).toBeInTheDocument();
    });

    // -- the operations that open a gate -----------------------------------

    it('mints the contract from the rail and refreshes the checklist', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                transitions: [option('approved', [req('contract_minted', 'A contract has been minted', false)])],
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-action-contract_minted'));
        await waitFor(() => expect(mockMint).toHaveBeenCalledWith('sys-1'));
        await waitFor(() => expect(mockLifecycle).toHaveBeenCalledTimes(2));
        expect(await screen.findByTestId('lifecycle-notice')).toHaveTextContent(/minted/i);
    });

    it('sets the owner inline and saves it on the resource group', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-action-owner_set'));
        const editor = await screen.findByTestId('declare-owner');
        expect(editor).toBeInTheDocument();
        expect(screen.getByTestId('declare-save')).toBeDisabled();
        fireEvent.change(screen.getByTestId('declare-input'), { target: { value: 'claims-ops@nordica' } });
        fireEvent.click(screen.getByTestId('declare-save'));
        await waitFor(() =>
            expect(mockUpdateGroup).toHaveBeenCalledWith('sys-1', { owner: 'claims-ops@nordica' }),
        );
        await waitFor(() => expect(screen.queryByTestId('declare-owner')).not.toBeInTheDocument());
    });

    it('describes the intended use inline', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-action-intended_use_set'));
        fireEvent.change(screen.getByTestId('declare-input'), {
            target: { value: 'Triage inbound claims for adjusters' },
        });
        fireEvent.click(screen.getByTestId('declare-save'));
        await waitFor(() =>
            expect(mockUpdateGroup).toHaveBeenCalledWith('sys-1', {
                intended_use: 'Triage inbound claims for adjusters',
            }),
        );
    });

    it('approves the newest unapproved contract when the gate wants an approver', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'approved',
                transitions: [
                    option('active', [
                        req('contract_minted', 'A contract has been minted', true),
                        req('approver_recorded', 'An approver is recorded on the contract', false),
                    ]),
                ],
            }),
        );
        mockContracts.mockResolvedValue({
            contracts: [
                { id: 'ctr-3', version: 3, status: 'draft', is_approved: false },
                { id: 'ctr-2', version: 2, status: 'active', is_approved: true },
            ],
            total: 2,
            active_version: 2,
            config_drifted: false,
        });
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-action-approver_recorded'));
        await waitFor(() => expect(mockApprove).toHaveBeenCalledWith('ctr-3'));
        expect(await screen.findByTestId('lifecycle-notice')).toHaveTextContent('v3');
    });

    it('says so instead of failing when there is no contract to approve', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'approved',
                transitions: [option('active', [req('approver_recorded', 'An approver is recorded', false)])],
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-action-approver_recorded'));
        expect(await screen.findByTestId('lifecycle-notice')).toHaveTextContent(/mint a contract first/i);
        expect(mockApprove).not.toHaveBeenCalled();
    });

    it('hands liveness off to the Signals tab and evidence to the Contract tab', async () => {
        const onOpen = vi.fn();
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'approved',
                eu_ai_act_risk_tier: 'high',
                transitions: [
                    option('active', [
                        req('liveness_expectation_set', 'A liveness expectation is set', false),
                        req('impact_assessment_on_file', 'An unexpired impact assessment is attached', false),
                    ]),
                ],
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" onOpen={onOpen} />);
        fireEvent.click(await screen.findByTestId('gate-action-liveness_expectation_set'));
        expect(onOpen).toHaveBeenCalledWith({ section: 'signals' });
        fireEvent.click(screen.getByTestId('gate-action-impact_assessment_on_file'));
        expect(onOpen).toHaveBeenCalledWith({ section: 'contract', sub: 'evidence' });
    });

    it('offers no button for a requirement only time can satisfy', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'deprecated',
                transitions: [option('retired', [req('no_recent_runs', 'No runs for the quiet period', false)])],
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await screen.findByTestId('gate-panel-retired');
        expect(screen.getByTestId('gate-req-no_recent_runs')).toBeInTheDocument();
        expect(screen.queryByTestId('gate-action-no_recent_runs')).not.toBeInTheDocument();
    });

    it('moves back a stage from the rail — always allowed', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'approved',
                transitions: [
                    option('active', [req('replay_passed', 'The bound replay suite passed', false)]),
                    option('proposed', []),
                ],
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-back-proposed'));
        await waitFor(() =>
            expect(mockTransition).toHaveBeenCalledWith('sys-1', { to_stage: 'proposed' }),
        );
    });

    // -- the transitions ---------------------------------------------------

    it('shows the whole checklist before anything is clicked', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await screen.findByTestId('lifecycle-gate');
        expect(screen.getByTestId('req-approved-contract_minted')).toBeInTheDocument();
        expect(screen.getByTestId('req-approved-owner_set')).toHaveTextContent('No owner set');
    });

    it('does not offer a transition that skips a stage', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await screen.findByTestId('lifecycle-gate');
        expect(screen.getByTestId('transition-approved')).toBeInTheDocument();
        expect(screen.queryByTestId('transition-retired')).not.toBeInTheDocument();
    });

    it('blocks a move whose evidence is missing', async () => {
        render(<LifecyclePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('go-approved')).toBeDisabled();
    });

    it('allows a move once every requirement is met', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({ transitions: [option('approved', [req('owner_set', 'An accountable owner is named', true)])] }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        const go = await screen.findByTestId('go-approved');
        expect(go).not.toBeDisabled();
        fireEvent.click(go);
        await waitFor(() => expect(mockTransition).toHaveBeenCalledWith('sys-1', { to_stage: 'approved' }));
    });

    it('counts down to the retirable date instead of only saying "not yet"', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'deprecated',
                succession: { successor_system_id: 'sys-2', successor_name: 'Claims Triage v2' },
                retirement: {
                    quiet_days_required: 30,
                    last_run_at: '2026-08-23T00:00:00Z',
                    retirable_at: '2026-09-22T00:00:00Z',
                    retirable: false,
                    days_remaining: 26,
                },
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        const box = await screen.findByTestId('retirement-readiness');
        expect(box).toHaveTextContent(/Retirable in 26 days/i);
        expect(screen.getByTestId('succeeded-by')).toHaveTextContent(/Claims Triage v2/);
    });

    it('says plainly when a deprecated system is ready to retire', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'deprecated',
                succession: { no_successor_declared: true },
                retirement: {
                    quiet_days_required: 30,
                    last_run_at: '2026-06-01T00:00:00Z',
                    retirable_at: '2026-07-01T00:00:00Z',
                    retirable: true,
                    days_remaining: 0,
                },
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('retirement-readiness')).toHaveTextContent(/Ready to retire/i);
        expect(screen.getByTestId('succeeded-by')).toHaveTextContent(/withdrawn/i);
    });

    const deprecateLifecycle = () =>
        lifecycle({
            current_stage: 'active',
            candidate_successors: [{ id: 'sys-2', name: 'Claims Triage v2', lifecycle_stage: 'active' }],
            transitions: [
                option('deprecated', [
                    req('successor_named', "A successor is named, or 'no successor' is stated explicitly", false),
                ]),
            ],
        });

    it('will not deprecate until the succession question is answered', async () => {
        mockLifecycle.mockResolvedValue(deprecateLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" />);
        const go = await screen.findByTestId('go-deprecated');
        expect((go as HTMLButtonElement).disabled).toBe(true);
        expect(mockTransition).not.toHaveBeenCalled();
    });

    it('records an explicit withdrawal when there is no successor', async () => {
        mockLifecycle.mockResolvedValue(deprecateLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('succession-none-deprecated'));
        fireEvent.click(screen.getByTestId('go-deprecated'));
        await waitFor(() =>
            expect(mockTransition).toHaveBeenCalledWith('sys-1', { to_stage: 'deprecated', no_successor: true }),
        );
    });

    it('records the superseding system when one is chosen', async () => {
        mockLifecycle.mockResolvedValue(deprecateLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('succession-superseded-deprecated'));
        expect((screen.getByTestId('go-deprecated'))).toBeDisabled();
        fireEvent.change(screen.getByTestId('successor-select-deprecated'), { target: { value: 'sys-2' } });
        fireEvent.click(screen.getByTestId('go-deprecated'));
        await waitFor(() =>
            expect(mockTransition).toHaveBeenCalledWith('sys-1', { to_stage: 'deprecated', successor_id: 'sys-2' }),
        );
    });

    const approverLifecycle = () =>
        lifecycle({
            eu_ai_act_risk_tier: 'high',
            transitions: [
                option('approved', [
                    req('human_approval', 'A human has approved this transition', false, 'This transition must be approved by a named person'),
                ]),
            ],
        });

    it('the logged-in admin is the named approver by default', async () => {
        // Typing your own name when you are the one clicking is theatre.
        mockLifecycle.mockResolvedValue(approverLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('high-risk-badge')).toBeInTheDocument();
        expect(screen.getByTestId('approver-self-approved')).toHaveTextContent('approving as admin');
        expect(screen.queryByTestId('approver-approved')).not.toBeInTheDocument();
        fireEvent.click(screen.getByTestId('go-approved'));
        await waitFor(() =>
            expect(mockTransition).toHaveBeenCalledWith('sys-1', { to_stage: 'approved', approved_by: 'admin' }),
        );
    });

    it('can approve on behalf of someone who is not logged in', async () => {
        mockLifecycle.mockResolvedValue(approverLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('approver-other-approved'));
        // Until a name is given there is no approver to record.
        expect(screen.getByTestId('go-approved')).toBeDisabled();
        fireEvent.change(screen.getByTestId('approver-approved'), { target: { value: 'alice' } });
        fireEvent.click(screen.getByTestId('go-approved'));
        await waitFor(() =>
            expect(mockTransition).toHaveBeenCalledWith('sys-1', { to_stage: 'approved', approved_by: 'alice' }),
        );
    });

    it('shows the transitions on record — who approved, who recorded, when', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                current_stage: 'active',
                transitions: [],
                history: [
                    { id: 'e2', ai_system_id: 'sys-1', from_stage: 'approved', to_stage: 'active', approved_by: 'Risk Owner Alice', recorded_by: 'admin', at: '2026-09-02T16:09:49Z' },
                    { id: 'e1', ai_system_id: 'sys-1', from_stage: 'proposed', to_stage: 'approved', approved_by: 'admin', recorded_by: 'admin', at: '2026-09-01T10:00:00Z' },
                ],
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        const hist = await screen.findByTestId('lifecycle-history');
        expect(screen.getByTestId('lifecycle-event-e2')).toHaveTextContent('approved → active');
        expect(screen.getByTestId('lifecycle-event-e2')).toHaveTextContent('approved by Risk Owner Alice');
        expect(screen.getByTestId('lifecycle-event-e2')).toHaveTextContent('recorded by admin');
        expect(screen.getByTestId('lifecycle-event-e1')).not.toHaveTextContent('recorded by');
        expect(hist).toBeInTheDocument();
        expect(screen.getByTestId('last-transition')).toHaveTextContent('approved by Risk Owner Alice');
    });

    it('"Name successor" on the rail lands on the succession question', async () => {
        mockLifecycle.mockResolvedValue(deprecateLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('gate-action-successor_named'));
        await waitFor(() =>
            expect(screen.getByTestId('transition-deprecated')).toHaveAttribute('data-highlighted', 'true'),
        );
        expect(document.activeElement).toBe(screen.getByTestId('succession-superseded-deprecated'));
    });

    it('surfaces an API failure instead of an empty panel', async () => {
        mockLifecycle.mockRejectedValue(new Error('control plane unreachable'));
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByText('control plane unreachable')).toBeInTheDocument());
        expect(screen.queryByTestId('stage-diagram')).not.toBeInTheDocument();
    });

    // -- fix links (the fields live on the resource group) -------

    const gateLifecycle = () =>
        lifecycle({
            transitions: [
                {
                    to_stage: 'approved',
                    allowed: false,
                    requirements: [
                        req('contract_minted', 'A contract has been minted', true),
                        req('owner_set', 'An accountable owner is named', false),
                        req('intended_use_set', 'Intended use is described', false),
                        // No single field to send anyone to — must stay plain text.
                        req('no_unapproved_widening', 'No unapproved widening', false),
                    ],
                    unmet: [],
                },
            ],
        });

    it('links an unmet owner / intended-use gate to the group Ownership section', async () => {
        const setActiveTab = vi.fn();
        mockLifecycle.mockResolvedValue(gateLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" setActiveTab={setActiveTab} />);
        fireEvent.click(await screen.findByTestId('req-fix-owner_set'));
        expect(setActiveTab).toHaveBeenCalledWith('groups:sys-1:general:ownership');
        fireEvent.click(screen.getByTestId('req-fix-intended_use_set'));
        expect(setActiveTab).toHaveBeenLastCalledWith('groups:sys-1:general:ownership');
    });

    it('does not link a requirement with no single field to fix', async () => {
        mockLifecycle.mockResolvedValue(gateLifecycle());
        render(<LifecyclePanel aiSystemId="sys-1" setActiveTab={vi.fn()} />);
        await screen.findByTestId('req-fix-owner_set');
        expect(screen.queryByTestId('req-fix-no_unapproved_widening')).toBeNull();
        // The rail offers "Review the contract" for it instead of a field link.
        expect(screen.getByTestId('gate-action-no_unapproved_widening')).toBeInTheDocument();
    });

    it('shows no link once the requirement is satisfied', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({ transitions: [option('approved', [req('owner_set', 'An accountable owner is named', true)])] }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" setActiveTab={vi.fn()} />);
        await screen.findByTestId('lifecycle-gate');
        expect(screen.queryByTestId('req-fix-owner_set')).toBeNull();
    });
});

describe('LifecyclePanel — the workload identity requirement', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockContracts.mockResolvedValue({ contracts: [], total: 0, active_version: null, config_drifted: false });
    });

    it('lists the offending credentials under the requirement in the transition\'s checklist', async () => {
        const requirement = {
            ...req('workload_identity_enforced', 'Machine callers authenticate with workload identity', false, '1 credential(s) reached this system below workload'),
            offending_credentials: [
                { credential_ref: 'api_key:k-9', credential_kind: 'api_key', credential_assurance: 'key', calls: 3, last_seen: '2026-11-17T08:00:00Z' },
            ],
            offending_total: 1,
        };
        mockLifecycle.mockResolvedValue(lifecycle({ current_stage: 'approved', transitions: [option('active', [requirement])] }));
        render(<LifecyclePanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('transition-active');
        const list = within(row).getByTestId('offending-active');
        expect(list).toHaveTextContent('api_key:k-9');
        expect(list).toHaveTextContent('3 calls');
        expect(list).not.toHaveTextContent('more not listed');
    });

    it('lists the offending credentials the refusal of a move carries', async () => {
        // The checklist said the move was open; by the time it was sent a
        // credential below the level had called. The 409 carries the gate's
        // decision, and the panel lists what it names.
        mockLifecycle.mockResolvedValue(
            lifecycle({ current_stage: 'approved', transitions: [option('active', [req('workload_identity_enforced', 'Machine callers authenticate with workload identity', true)])] }),
        );
        const unmet = {
            ...req('workload_identity_enforced', 'Machine callers authenticate with workload identity', false, '2 credential(s) reached this system below workload'),
            offending_credentials: [
                { credential_ref: 'api_key:k-7', credential_kind: 'api_key', credential_assurance: 'key', calls: 5, last_seen: '2026-11-18T08:00:00Z' },
                { credential_ref: null, credential_kind: 'anonymous', credential_assurance: null, calls: 1, last_seen: null },
            ],
            offending_total: 4,
        };
        mockTransition.mockRejectedValue(
            new GatewayAPIError('Cannot move from approved to active: Machine callers authenticate with workload identity', 409, undefined, {
                detail: {
                    message: 'Cannot move from approved to active: Machine callers authenticate with workload identity',
                    decision: { from_stage: 'approved', to_stage: 'active', allowed: false, requirements: [unmet], unmet: [unmet] },
                },
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('go-active'));
        const list = await screen.findByTestId('refused-offending-workload_identity_enforced');
        expect(screen.getByText(/Cannot move from approved to active/)).toBeInTheDocument();
        expect(list).toHaveTextContent('api_key:k-7');
        expect(list).toHaveTextContent('5 calls');
        expect(list).toHaveTextContent('no reference');
        expect(list).toHaveTextContent('2 more not listed');
    });

    it('lists nothing more for a refusal that carries no offending credentials', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({ current_stage: 'approved', transitions: [option('active', [req('owner_set', 'An accountable owner is named', true)])] }),
        );
        const unmet = req('owner_set', 'An accountable owner is named', false, 'No owner set');
        mockTransition.mockRejectedValue(
            new GatewayAPIError('Cannot move from approved to active: An accountable owner is named', 409, undefined, {
                detail: { message: 'Cannot move', decision: { allowed: false, requirements: [unmet], unmet: [unmet] } },
            }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('go-active'));
        await screen.findByText(/Cannot move from approved to active/);
        expect(screen.queryByTestId(/^refused-offending-/)).not.toBeInTheDocument();
    });

    it('lists none when the requirement is met', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({ current_stage: 'approved', transitions: [option('active', [req('workload_identity_enforced', 'Machine callers authenticate with workload identity', true)])] }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await screen.findByTestId('transition-active');
        expect(screen.queryByTestId('offending-active')).not.toBeInTheDocument();
    });
});

// agent.new_release events on the Lifecycle tab.
describe('LifecyclePanel — agent releases', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockContracts.mockResolvedValue({ contracts: [], total: 0, active_version: null, config_drifted: false });
    });

    const newRelease = (id: string, version: string, trust: string, approved: boolean | null) => ({
        id,
        ai_system_id: 'sys-1',
        event_type: 'agent.new_release',
        subject_id: `rel-${id}`,
        details: { agent_id: 'agent-1', agent_name: 'Screening', release_id: `rel-${id}`, name: 'screening-agent', version, build: null, trust, approved },
        from_stage: null,
        to_stage: null,
        recorded_by: 'system:agent-releases',
        at: '2026-09-27T10:00:00Z',
    });

    it('lists each new release with its trust and approval, linking to the identity', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({
                history: [],
                events: [newRelease('e2', '1.5.0', 'verified', false), newRelease('e1', '1.4.2', 'declared', true)],
            }),
        );
        const setActiveTab = vi.fn();
        render(<LifecyclePanel aiSystemId="sys-1" setActiveTab={setActiveTab} />);
        const block = await screen.findByTestId('lifecycle-agent-releases');
        const latest = screen.getByTestId('lifecycle-release-e2');
        expect(latest).toHaveTextContent('New release');
        expect(latest).toHaveTextContent('screening-agent@1.5.0');
        expect(latest.querySelector('[data-testid="trust-badge"]')).toHaveAttribute('data-trust', 'verified');
        expect(latest.querySelector('[data-testid="approval-badge"]')).toHaveAttribute('data-approval', 'unapproved');
        expect(screen.getByTestId('lifecycle-release-e1')).toHaveTextContent('screening-agent@1.4.2');
        expect(block.textContent?.indexOf('1.5.0')).toBeLessThan(block.textContent?.indexOf('1.4.2') ?? -1);
        fireEvent.click(screen.getByTestId('lifecycle-release-agent-e2'));
        expect(setActiveTab).toHaveBeenCalledWith('a2a-identities:agent-1:implementation');
        // A release is not a stage transition.
        expect(screen.queryByTestId('lifecycle-history')).toBeNull();
    });

    it('shows no release block when there are none, and ignores events it does not know', async () => {
        mockLifecycle.mockResolvedValue(
            lifecycle({ events: [{ ...newRelease('x', '1', 'declared', null), event_type: 'something.else' }] }),
        );
        render(<LifecyclePanel aiSystemId="sys-1" />);
        await screen.findByTestId('stage-diagram');
        expect(screen.queryByTestId('lifecycle-agent-releases')).toBeNull();
    });
});
