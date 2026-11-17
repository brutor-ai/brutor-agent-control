/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for ResponsePolicyPanel.
 *
 * This panel arms a control that can restrict production systems with no
 * human in the loop, so the properties worth defending are all about what
 * the operator is shown *before* the click:
 *
 *   - the blast radius is visible before the policy is armed, and it
 *     refreshes when the draft changes;
 *   - arming is not offered until a preview has come back;
 *   - suspend needs a second, explicit acknowledgement;
 *   - refusals are as visible as actions, because "nothing matched" and
 *     "the loop is down" otherwise look identical;
 *   - an inherited policy cannot be disarmed from the wrong system.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockPolicies = vi.fn();
const mockHistory = vi.fn();
const mockPreview = vi.fn();
const mockCreate = vi.fn();
const mockDelete = vi.fn();
const mockRestore = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchResponsePolicies: mockPolicies,
        fetchResponseHistory: mockHistory,
        previewResponsePolicy: mockPreview,
        createResponsePolicy: mockCreate,
        deleteResponsePolicy: mockDelete,
        restoreAutonomy: mockRestore,
    }),
}));

import ResponsePolicyPanel from '../ResponsePolicyPanel';
import type {
    PolicyPreview,
    ResponseHistory,
    ResponsePolicy,
} from '../../../lib/aiSystemTypes';

function policy(overrides: Partial<ResponsePolicy> = {}): ResponsePolicy {
    return {
        id: 'rsp-1',
        ai_system_id: 'sys-1',
        name: 'tighten-on-drift',
        enabled: true,
        signal_kind: 'drift',
        drift_class: null,
        severity_at_least: 'high',
        actions: [{ kind: 'set_autonomy', level: 'restricted' }],
        allow_suspend: false,
        require_human_to_restore: true,
        response_cooldown_seconds: 3600,
        fire_count: 0,
        is_actuating: true,
        target_autonomy: 'restricted',
        ...overrides,
    };
}

function preview(overrides: Partial<PolicyPreview> = {}): PolicyPreview {
    return {
        ai_system_id: 'sys-1',
        current_autonomy: 'autonomous',
        blast_radius: {
            target_id: 'sys-1',
            inherited_by: [],
            total: 1,
            is_wide: false,
        },
        would_do: [{ kind: 'set_autonomy', params: { level: 'restricted' } }],
        would_refuse: [],
        warnings: [],
        ...overrides,
    };
}

function history(overrides: Partial<ResponseHistory> = {}): ResponseHistory {
    return {
        responses: [],
        total: 0,
        acted_count: 0,
        pending_count: 0,
        ...overrides,
    };
}

describe('ResponsePolicyPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockPolicies.mockResolvedValue({ policies: [], total: 0, inherited_count: 0 });
        mockHistory.mockResolvedValue(history());
        mockPreview.mockResolvedValue(preview());
        mockCreate.mockResolvedValue(policy());
        mockDelete.mockResolvedValue({});
        mockRestore.mockResolvedValue({});
    });

    // -- the preview ------------------------------------------------------

    it('shows the blast radius before the policy is armed', async () => {
        // The affected set must be visible before arming, not after.
        mockPreview.mockResolvedValue(
            preview({
                blast_radius: {
                    target_id: 'sys-1',
                    inherited_by: ['child-a', 'child-b'],
                    total: 3,
                    is_wide: true,
                },
            })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));

        const radius = await screen.findByTestId('blast-radius');
        expect(radius).toHaveTextContent('Affects');
        expect(radius).toHaveTextContent('3');
        expect(radius).toHaveTextContent('child-a, child-b');
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('refreshes the preview when the draft changes', async () => {
        // An operator must be looking at the radius for the policy they are
        // about to arm, not a stale one.
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(1));

        fireEvent.change(screen.getByTestId('level-select'), {
            target: { value: 'approval_required' },
        });
        await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(2));
        expect(mockPreview.mock.calls[1][1]).toMatchObject({
            actions: [{ kind: 'set_autonomy', level: 'approval_required' }],
        });
    });

    it('shows what would happen', async () => {
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        expect(await screen.findByTestId('would-do')).toHaveTextContent(
            'autonomous → restricted'
        );
    });

    it('says plainly when the policy would do nothing', async () => {
        mockPreview.mockResolvedValue(
            preview({
                would_do: [],
                would_refuse: [{ code: 'no_change_needed', detail: 'already restricted' }],
            })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));

        expect(await screen.findByTestId('would-do-nothing')).toBeInTheDocument();
        expect(screen.getByTestId('would-refuse')).toHaveTextContent(
            'Already at the target level'
        );
    });

    it('surfaces the API warnings verbatim', async () => {
        mockPreview.mockResolvedValue(
            preview({ warnings: ['This policy acts on 4 systems.'] })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        expect(await screen.findByTestId('preview-warning')).toHaveTextContent(
            'acts on 4 systems'
        );
    });

    it('does not offer to arm before a preview has come back', async () => {
        // Arming without having seen the consequences is the exact failure
        // this panel exists to prevent.
        let resolvePreview: (v: PolicyPreview) => void = () => {};
        mockPreview.mockImplementation(
            () => new Promise<PolicyPreview>((r) => (resolvePreview = r))
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        fireEvent.change(screen.getByTestId('policy-name'), {
            target: { value: 'p' },
        });

        expect(screen.getByTestId('arm-button')).toBeDisabled();
        resolvePreview(preview());
        await waitFor(() =>
            expect(screen.getByTestId('arm-button')).not.toBeDisabled()
        );
    });

    it('surfaces a preview failure rather than arming blind', async () => {
        mockPreview.mockRejectedValue(new Error('control plane unreachable'));
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        fireEvent.change(screen.getByTestId('policy-name'), {
            target: { value: 'p' },
        });

        expect(await screen.findByTestId('preview-error')).toHaveTextContent(
            'control plane unreachable'
        );
        expect(screen.getByTestId('arm-button')).toBeDisabled();
    });

    // -- arming -----------------------------------------------------------

    it('arms the policy that was previewed', async () => {
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        fireEvent.change(screen.getByTestId('policy-name'), {
            target: { value: 'tighten' },
        });
        await screen.findByTestId('policy-preview');
        fireEvent.click(screen.getByTestId('arm-button'));

        await waitFor(() =>
            expect(mockCreate).toHaveBeenCalledWith('sys-1', {
                name: 'tighten',
                signal_kind: 'drift',
                drift_class: null,
                severity_at_least: 'high',
                actions: [{ kind: 'set_autonomy', level: 'restricted' }],
                allow_suspend: false,
                require_human_to_restore: true,
            })
        );
    });

    it('will not arm an unnamed policy', async () => {
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        await screen.findByTestId('policy-preview');
        expect(screen.getByTestId('arm-button')).toBeDisabled();
    });

    it('requires an explicit acknowledgement to arm a suspend policy', async () => {
        // Suspend denies every call including read-only ones; a dropdown
        // selection alone is not enough of a decision.
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        fireEvent.change(screen.getByTestId('policy-name'), {
            target: { value: 'kill' },
        });
        fireEvent.change(screen.getByTestId('level-select'), {
            target: { value: 'suspended' },
        });

        await screen.findByTestId('suspend-optin');
        await waitFor(() =>
            expect(screen.getByTestId('arm-button')).toBeDisabled()
        );

        fireEvent.click(screen.getByTestId('suspend-checkbox'));
        await waitFor(() =>
            expect(screen.getByTestId('arm-button')).not.toBeDisabled()
        );
    });

    it('does not show the suspend acknowledgement for milder levels', async () => {
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('add-policy'));
        await screen.findByTestId('policy-preview');
        expect(screen.queryByTestId('suspend-optin')).not.toBeInTheDocument();
    });

    // -- existing policies ------------------------------------------------

    it('says plainly when nothing is armed', async () => {
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        expect(await screen.findByText('No automatic response')).toBeInTheDocument();
        expect(
            screen.getByText(/nothing happens without a human/)
        ).toBeInTheDocument();
    });

    it('marks an inherited policy and refuses to disarm it here', async () => {
        // It has to be disarmed on the system it is attached to, or an
        // operator would think they had turned it off.
        mockPolicies.mockResolvedValue({
            policies: [policy({ ai_system_id: 'parent' })],
            total: 1,
            inherited_count: 1,
        });
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('policy-rsp-1');
        expect(within(row).getByTestId('inherited-badge')).toHaveTextContent('parent');
        expect(within(row).getByTestId('disarm-button')).toBeDisabled();
    });

    it('disarms a policy attached to this system', async () => {
        mockPolicies.mockResolvedValue({
            policies: [policy()],
            total: 1,
            inherited_count: 0,
        });
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('disarm-button'));
        await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('rsp-1'));
    });

    it('flags a policy that may suspend', async () => {
        mockPolicies.mockResolvedValue({
            policies: [policy({ allow_suspend: true })],
            total: 1,
            inherited_count: 0,
        });
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('suspend-badge')).toHaveTextContent(
            'may suspend'
        );
    });

    // -- history ----------------------------------------------------------

    it('shows refusals as prominently as actions', async () => {
        // Without this, "no policy matched" and "the loop is down" look the
        // same to an operator staring at an empty list.
        mockHistory.mockResolvedValue(
            history({
                total: 1,
                responses: [
                    {
                        drift_event_id: 'd1',
                        drift_class: 'behavioural',
                        severity: 'critical',
                        acted: false,
                        actions: [],
                        refusals: [{ code: 'cooldown_active', detail: '' }],
                    },
                ],
            })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('response-refusal')).toHaveTextContent(
            'A response already fired recently'
        );
    });

    it('says what an action that was taken does not cover', async () => {
        // An approval hold does not stop plain API keys or people acting
        // without a run scope; an operator must not read "acted" as "covered".
        mockHistory.mockResolvedValue(
            history({
                total: 1,
                acted_count: 1,
                responses: [
                    {
                        drift_event_id: 'd1',
                        drift_class: 'behavioural',
                        severity: 'high',
                        acted: true,
                        policy_name: 'hold',
                        actions: [{ kind: 'set_autonomy', params: { level: 'approval_required' } }],
                        refusals: [],
                        limitations: [
                            {
                                code: 'approval_bypassed_by_unbound_keys',
                                detail: '3 active API keys are not held by approval_required.',
                                unbound_active_keys: 3,
                                recent_runs: 40,
                                unbound_recent_runs: 12,
                                recent_days: 7,
                            },
                        ],
                    },
                ],
            })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('response-limitation')).toHaveTextContent(
            '3 active API keys are not held by approval_required.'
        );
    });

    it('renders no limitation line for a record without limitations', async () => {
        mockHistory.mockResolvedValue(
            history({
                total: 1,
                responses: [
                    {
                        drift_event_id: 'd1',
                        drift_class: 'behavioural',
                        severity: 'critical',
                        acted: false,
                        actions: [],
                        refusals: [{ code: 'cooldown_active', detail: '' }],
                    },
                ],
            })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        await screen.findByTestId('response-refusal');
        expect(screen.queryByTestId('response-limitation')).not.toBeInTheDocument();
    });

    it('reports findings the loop has not considered', async () => {
        // A number that stays high is how an operator notices the closed
        // loop has stopped running.
        mockHistory.mockResolvedValue(history({ pending_count: 4 }));
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('pending-count')).toHaveTextContent(
            '4 finding(s) not yet considered'
        );
    });

    it('offers restore only once something has acted', async () => {
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        await screen.findByText('No automatic response');
        expect(screen.queryByTestId('restore-button')).not.toBeInTheDocument();
    });

    it('restores by clearing the override rather than guessing a level', async () => {
        mockHistory.mockResolvedValue(
            history({
                total: 1,
                acted_count: 1,
                responses: [
                    {
                        drift_event_id: 'd1',
                        drift_class: 'behavioural',
                        severity: 'critical',
                        acted: true,
                        policy_name: 'tighten',
                        actions: [{ kind: 'set_autonomy', params: { level: 'restricted' } }],
                        refusals: [],
                    },
                ],
            })
        );
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('restore-button'));
        await waitFor(() =>
            expect(mockRestore).toHaveBeenCalledWith('sys-1', { to_level: null })
        );
    });

    // -- failure ----------------------------------------------------------

    it('surfaces an API failure instead of an empty panel', async () => {
        mockPolicies.mockRejectedValue(new Error('control plane unreachable'));
        mockHistory.mockRejectedValue(new Error('control plane unreachable'));
        render(<ResponsePolicyPanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
        expect(screen.queryByText('No automatic response')).not.toBeInTheDocument();
    });
});
