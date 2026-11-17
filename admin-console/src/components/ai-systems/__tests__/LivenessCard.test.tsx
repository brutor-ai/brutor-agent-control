/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for LivenessCard.
 *
 * Three states must stay distinguishable, because collapsing any two of
 * them is how this card would mislead:
 *
 *   - **silent** — ran before, has now stopped. A finding.
 *   - **never run** — registered, never wired up. Not a finding.
 *   - **never checked** — the detector has not evaluated it. Not health
 *     information at all, and must not read as green.
 *
 * The fourth behaviour worth pinning is the refusal: with too little
 * history the card must not offer a one-click "use this cadence", because
 * an expectation invented from three runs will fire and cost the operator's
 * trust in the whole alert category.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetch = vi.fn();
const mockUpdate = vi.fn();
const mockLearn = vi.fn();
const mockAccept = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemLiveness: mockFetch,
        updateAISystemLiveness: mockUpdate,
        learnAISystemLiveness: mockLearn,
        acceptAISystemLiveness: mockAccept,
    }),
}));

import LivenessCard from '../LivenessCard';
import type { LivenessStatus } from '../../../lib/aiSystemTypes';

function status(overrides: Partial<LivenessStatus> = {}): LivenessStatus {
    return {
        ai_system_id: 'sys-1',
        configured: true,
        expectation: {
            id: 'live-1',
            ai_system_id: 'sys-1',
            mode: 'continuous',
            expected_min_runs: 4,
            window_seconds: 3600,
            grace_seconds: 300,
            enabled: true,
            accepted_from_learned: false,
            alerts_on_silence: true,
            deadline_seconds: 3900,
            has_learned_cadence: false,
            last_checked_at: '2026-07-27T12:00:00Z',
            ...(overrides.expectation ?? {}),
        },
        state: 'alive',
        reason: '4 run(s) in the window.',
        window_hours: 720,
        inbound_calls: 0,
        last_activity_at: '2026-07-27T11:59:00Z',
        last_run_at: '2026-07-27T11:59:00Z',
        quiet_for_seconds: 60,
        last_processed_run_at: '2026-07-27T09:00:00Z',
        processed_quiet_for_seconds: 10800,
        work_quiet: false,
        work_quiet_reason: null,
        has_ever_run: true,
        is_silent: false,
        ...overrides,
    };
}

describe('LivenessCard', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetch.mockResolvedValue(status());
    });

    it('explains what mode means before an operator changes it', async () => {
        // The line under the select describes the mode already chosen; this
        // explains the choice, which is what you need in order to change it.
        render(<LivenessCard aiSystemId="sys-1" />);
        await screen.findByTestId('liveness-card');
        const label = screen.getByText('Mode', { selector: 'label' });
        fireEvent.click(within(label).getByTestId('help-tip'));
        const tip = screen.getByTestId('help-tip-popover');
        expect(tip).toHaveTextContent(/missed schedule/i);
        expect(tip).toHaveTextContent(/quiet is never a finding/i);
    });

    it('loads liveness on mount', async () => {
        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('liveness-card')).toBeInTheDocument());
        expect(mockFetch).toHaveBeenCalledWith('sys-1');
    });

    // The pill is the endpoint's ONE `state`, one test per
    // state, with its reason under it.
    it.each([
        ['alive', 'Alive', '4 run(s) in the window.'],
        ['silent', 'Silent', 'No activity for 25.0 hours after running before.'],
        ['idle', 'Idle', 'No inbound calls in the window — on-demand, so not a fault.'],
        ['unstarted', 'Unstarted', 'Has never run.'],
        ['not_configured', 'Not configured', 'The liveness expectation is disabled.'],
    ] as const)('renders state %s as "%s" with its reason', async (state, label, reason) => {
        mockFetch.mockResolvedValue(status({ state, reason }));
        render(<LivenessCard aiSystemId="sys-1" />);
        const pill = await screen.findByTestId(`pill-${state}`);
        expect(pill).toHaveTextContent(label);
        expect(pill).toHaveAttribute('title', reason);
        expect(screen.getByTestId('liveness-reason')).toHaveTextContent(reason);
    });

    it('renders the server state even when the flags beside it would say otherwise', async () => {
        // Deriving "Alive" from has_ever_run && !is_silent && configured
        // cannot say "idle". An on-demand service nobody
        // called has run before, is not silent and is configured: it is idle.
        mockFetch.mockResolvedValue(
            status({ state: 'idle', reason: 'No inbound calls in the window.', has_ever_run: true, is_silent: false, configured: true })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('pill-idle')).toHaveTextContent('Idle');
        expect(screen.queryByTestId('pill-alive')).not.toBeInTheDocument();
    });

    it('a never-run system is unstarted, never silent or alive', async () => {
        mockFetch.mockResolvedValue(
            status({ state: 'unstarted', reason: 'Has never run.', has_ever_run: false, last_run_at: null })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('pill-unstarted')).toHaveAttribute('data-tone', 'neutral');
        expect(screen.queryByTestId('pill-silent')).not.toBeInTheDocument();
        expect(screen.queryByTestId('pill-alive')).not.toBeInTheDocument();
    });

    it('warns when the detector has never evaluated the system', async () => {
        // "Never checked" is not health information. Rendering it as green
        // would be the exact failure this feature exists to prevent.
        mockFetch.mockResolvedValue(
            status({ expectation: { ...status().expectation, last_checked_at: null } })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        const note = await screen.findByTestId('never-checked');
        expect(note).toHaveTextContent('not a health verdict');
    });

    it('says when it is showing a default rather than a saved expectation', async () => {
        mockFetch.mockResolvedValue(status({ configured: false }));
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('liveness-default-note')).toBeInTheDocument();
    });

    it('hides window settings for modes that never alert on silence', async () => {
        mockFetch.mockResolvedValue(
            status({
                expectation: { ...status().expectation, mode: 'sporadic', alerts_on_silence: false },
            })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('liveness-card')).toBeInTheDocument());
        expect(screen.queryByTestId('liveness-window')).not.toBeInTheDocument();
    });

    it('changes the mode and reloads', async () => {
        mockUpdate.mockResolvedValue(status().expectation);
        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1));

        fireEvent.change(screen.getByLabelText('Liveness mode'), {
            target: { value: 'scheduled' },
        });
        await waitFor(() => {
            expect(mockUpdate).toHaveBeenCalledWith('sys-1', { mode: 'scheduled' });
        });
        await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
    });

    it('offers the learned cadence when there is enough history', async () => {
        mockLearn.mockResolvedValue({
            mode: 'continuous',
            window_seconds: 3600,
            grace_seconds: 360,
            expected_min_runs: 12,
            learned_from_runs: 240,
            median_gap_seconds: 120,
            p95_gap_seconds: 3200,
            confident: true,
            rationale: 'Learned from 240 runs over 30 days.',
        });
        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('learn-cadence')).toBeInTheDocument());

        fireEvent.click(screen.getByTestId('learn-cadence'));
        const proposal = await screen.findByTestId('cadence-proposal');
        expect(proposal).toHaveTextContent('Learned from 240 runs');
        expect(screen.getByText('Use this cadence')).toBeInTheDocument();
    });

    it('refuses to offer a cadence it is not confident in', async () => {
        mockLearn.mockResolvedValue({
            mode: 'continuous',
            window_seconds: 3600,
            grace_seconds: 300,
            expected_min_runs: 1,
            learned_from_runs: 3,
            median_gap_seconds: null,
            p95_gap_seconds: null,
            confident: false,
            rationale: 'Only 3 run(s) in the last 30 days.',
        });
        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('learn-cadence')).toBeInTheDocument());

        fireEvent.click(screen.getByTestId('learn-cadence'));
        expect(await screen.findByTestId('not-confident')).toBeInTheDocument();
        // No one-click accept: a guess from three runs will fire and cost
        // the operator's trust in the whole category.
        expect(screen.queryByText('Use this cadence')).not.toBeInTheDocument();
    });

    it('accepts the cadence and reloads', async () => {
        mockLearn.mockResolvedValue({
            mode: 'continuous',
            window_seconds: 3600,
            grace_seconds: 360,
            expected_min_runs: 12,
            learned_from_runs: 240,
            median_gap_seconds: 120,
            p95_gap_seconds: 3200,
            confident: true,
            rationale: 'Learned from 240 runs.',
        });
        mockAccept.mockResolvedValue(status().expectation);

        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('learn-cadence')).toBeInTheDocument());
        fireEvent.click(screen.getByTestId('learn-cadence'));
        fireEvent.click(await screen.findByText('Use this cadence'));

        await waitFor(() => expect(mockAccept).toHaveBeenCalledWith('sys-1'));
        await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
    });

    it('shows where an accepted cadence came from', async () => {
        mockFetch.mockResolvedValue(
            status({
                expectation: {
                    ...status().expectation,
                    accepted_from_learned: true,
                    learned_from_runs: 240,
                    learned_p95_gap_seconds: 3200,
                },
            })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        const badge = await screen.findByTestId('learned-badge');
        expect(badge).toHaveTextContent('Learned from 240 runs');
    });

    it('surfaces an API failure', async () => {
        mockFetch.mockRejectedValue(new Error('control plane unreachable'));
        render(<LivenessCard aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
    });

    it('does not claim a system is alive when no expectation was ever saved', async () => {
        // The failure this guards: nobody saved an expectation, so nothing is
        // watching it — the control plane says not_configured. Painting that
        // green means a system quiet for ten times its default window shows
        // the same reassuring pill as one that called a second ago.
        mockFetch.mockResolvedValue(
            status({
                state: 'not_configured',
                reason: 'No liveness expectation is stored.',
                configured: false,
                quiet_for_seconds: 35606,
                is_silent: false,
            })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('pill-not_configured')).toHaveTextContent('Not configured');
        expect(screen.queryByTestId('pill-alive')).not.toBeInTheDocument();
    });

    it('still reads alive once an expectation is saved and traffic is flowing', async () => {
        mockFetch.mockResolvedValue(
            status({ configured: true, quiet_for_seconds: 60, is_silent: false })
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('pill-alive')).toBeInTheDocument();
    });
});

/**
 * The last run and the last PROCESSED run are different facts: a
 * poll that keeps finding nothing is alive and doing no work. The optional
 * bound turns "no work for too long" into its own state, never silence.
 */
describe('LivenessCard — work getting done', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockUpdate.mockResolvedValue({});
    });

    it('shows the last run and, separately, the last processed run', async () => {
        mockFetch.mockResolvedValue(status());
        render(<LivenessCard aiSystemId="sys-1" />);
        const observed = await screen.findByTestId('liveness-observed');
        expect(observed).toHaveTextContent('Last run');
        expect(screen.getByTestId('liveness-last-processed')).toHaveTextContent(
            new Date('2026-07-27T09:00:00Z').toLocaleString(),
        );
        expect(screen.getByTestId('liveness-processed-quiet')).toHaveTextContent('3.0 hours');
        expect(screen.queryByTestId('liveness-work-quiet')).toBeNull();
    });

    it('says "never" when nothing was ever processed', async () => {
        mockFetch.mockResolvedValue(status({ last_processed_run_at: null, processed_quiet_for_seconds: null }));
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('liveness-last-processed')).toHaveTextContent('never');
        expect(screen.getByTestId('liveness-processed-quiet')).toHaveTextContent('—');
    });

    it('shows work quiet as its own state, with the server’s reason, and not as silence', async () => {
        mockFetch.mockResolvedValue(
            status({
                work_quiet: true,
                work_quiet_reason: 'No processed run for 3.0 hours (bound 2.0 hours).',
                expectation: { max_quiet_processed_seconds: 7200 } as LivenessStatus['expectation'],
            }),
        );
        render(<LivenessCard aiSystemId="sys-1" />);
        const banner = await screen.findByTestId('liveness-work-quiet');
        expect(banner).toHaveTextContent('Work quiet');
        expect(banner).toHaveTextContent('No processed run for 3.0 hours (bound 2.0 hours).');
        expect(banner).toHaveTextContent('not silence');
        // The liveness verdict itself is untouched.
        expect(screen.getByTestId('pill-alive')).toBeInTheDocument();
        expect(screen.queryByTestId('pill-silent')).toBeNull();
    });

    it('shows a stored bound in its largest whole unit', async () => {
        mockFetch.mockResolvedValue(status({ expectation: { max_quiet_processed_seconds: 172800 } as LivenessStatus['expectation'] }));
        render(<LivenessCard aiSystemId="sys-1" />);
        expect(await screen.findByTestId('liveness-quiet-bound-value')).toHaveValue('2');
        expect(screen.getByTestId('liveness-quiet-bound-unit')).toHaveValue('days');
        expect(screen.getByTestId('liveness-quiet-bound-save')).toBeDisabled();
    });

    it('saves a new bound in seconds and reloads', async () => {
        mockFetch.mockResolvedValue(status());
        render(<LivenessCard aiSystemId="sys-1" />);
        fireEvent.change(await screen.findByTestId('liveness-quiet-bound-value'), { target: { value: '6' } });
        fireEvent.change(screen.getByTestId('liveness-quiet-bound-unit'), { target: { value: 'hours' } });
        fireEvent.click(screen.getByTestId('liveness-quiet-bound-save'));
        await waitFor(() =>
            expect(mockUpdate).toHaveBeenCalledWith('sys-1', { max_quiet_processed_seconds: 21600 }),
        );
        await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
    });

    it('clears a bound with an empty value (null, not 0)', async () => {
        mockFetch.mockResolvedValue(status({ expectation: { max_quiet_processed_seconds: 3600 } as LivenessStatus['expectation'] }));
        render(<LivenessCard aiSystemId="sys-1" />);
        fireEvent.change(await screen.findByTestId('liveness-quiet-bound-value'), { target: { value: '' } });
        const button = screen.getByTestId('liveness-quiet-bound-save');
        expect(button).toHaveTextContent('Clear');
        fireEvent.click(button);
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('sys-1', { max_quiet_processed_seconds: null }));
    });

    it('refuses a bound outside 1 minute – 366 days before saving', async () => {
        mockFetch.mockResolvedValue(status());
        render(<LivenessCard aiSystemId="sys-1" />);
        fireEvent.change(await screen.findByTestId('liveness-quiet-bound-value'), { target: { value: '400' } });
        fireEvent.change(screen.getByTestId('liveness-quiet-bound-unit'), { target: { value: 'days' } });
        expect(screen.getByTestId('liveness-quiet-bound-error')).toHaveTextContent('At most 366 days.');
        expect(screen.getByTestId('liveness-quiet-bound-save')).toBeDisabled();
        fireEvent.change(screen.getByTestId('liveness-quiet-bound-value'), { target: { value: 'soon' } });
        expect(screen.getByTestId('liveness-quiet-bound-error')).toHaveTextContent('Enter a number.');
        expect(mockUpdate).not.toHaveBeenCalled();
    });

    it('surfaces a refused bound', async () => {
        mockFetch.mockResolvedValue(status());
        mockUpdate.mockRejectedValue(new Error('max_quiet_processed_seconds: out of range'));
        render(<LivenessCard aiSystemId="sys-1" />);
        fireEvent.change(await screen.findByTestId('liveness-quiet-bound-value'), { target: { value: '5' } });
        fireEvent.click(screen.getByTestId('liveness-quiet-bound-save'));
        expect(await screen.findByText('max_quiet_processed_seconds: out of range')).toBeInTheDocument();
    });
});
