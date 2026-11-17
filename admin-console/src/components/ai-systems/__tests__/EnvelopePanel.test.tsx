/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for EnvelopePanel.
 *
 * What is worth defending:
 *
 *   - the term vocabulary is server-provided and closed — the panel renders
 *     exactly the known terms, no free-text promises;
 *   - saving reports the "not under signature" consequence when config
 *     drifts, and notifies upward so the contract/gate views refresh;
 *   - clearing every field clears the declaration (null, not {});
 *   - a validation refusal from the server surfaces instead of vanishing.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetch = vi.fn();
const mockPut = vi.fn();
const mockOutcomes = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemEnvelope: mockFetch,
        putAISystemEnvelope: mockPut,
        fetchAISystemOutcomes: mockOutcomes,
    }),
}));

import EnvelopePanel from '../EnvelopePanel';

const KNOWN_TERMS = {
    max_avg_cost_per_completed_task_usd: {
        direction: 'max', unit: 'usd', label: 'Average cost per completed task',
        signal: 'cost', enforced: false, help: 'The mean cost of a finished task.',
    },
    min_completion_rate: {
        direction: 'min', unit: 'rate', label: 'Tasks that complete',
        signal: 'reliability', enforced: false, help: 'A floor, not a ceiling.',
    },
    max_cost_per_run_usd: {
        direction: 'max', unit: 'usd', label: 'Cost ceiling for one run',
        signal: 'cost', enforced: true, help: 'No single run may spend more than this.',
    },
    max_llm_calls_per_run: {
        direction: 'max', unit: 'count', label: 'Model calls per run',
        signal: 'cost', enforced: true, help: 'The classic runaway-loop guard.',
    },
    max_tokens_per_run: {
        direction: 'max', unit: 'count', label: 'Tokens per run',
        signal: 'cost', enforced: true, help: 'The price-independent safety limit.',
    },
    // Terms that name a declared outcome in their key.
    max_outcome_share: {
        direction: 'max', unit: 'rate', label: 'Share of runs ending in an outcome',
        signal: 'reliability', enforced: false, parameterized: true,
        help: 'Declared as max_outcome_share:<outcome id>.',
    },
    avg_cost_per_outcome: {
        direction: 'max', unit: 'usd', label: 'Average cost of an outcome',
        signal: 'cost', enforced: false, parameterized: true,
        help: 'Declared as avg_cost_per_outcome:<outcome id>.',
    },
};

function resp(envelope: Record<string, number> | null, drifted = false) {
    return {
        ai_system_id: 'sys-1',
        operating_envelope: envelope,
        known_terms: KNOWN_TERMS,
        config_drifted: drifted,
    };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue(resp(null));
    mockPut.mockResolvedValue(resp({ max_avg_cost_per_completed_task_usd: 0.15 }, true));
    mockOutcomes.mockResolvedValue({
        ai_system_id: 'sys-1',
        outcomes: [
            {
                id: 'application_processed', label: 'Application processed', counts_toward_baseline: true,
                variants: [{ id: 'loan_approved', label: 'Approve' }],
            },
            { id: 'no_applications_available', label: 'Nothing to screen', counts_toward_baseline: false },
        ],
        active_outcomes: null,
        reportable_ids: [],
        config_drifted: false,
    });
});

describe('EnvelopePanel', () => {
    it('renders exactly the server-provided terms', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('envelope-panel')).toBeInTheDocument()
        );
        expect(
            screen.getByTestId('envelope-max_avg_cost_per_completed_task_usd')
        ).toBeInTheDocument();
        expect(screen.getByTestId('envelope-min_completion_rate')).toBeInTheDocument();
        expect(screen.queryByTestId('envelope-min_correct_decisions')).toBeNull();
        // The cost ceiling is enforced, so it belongs in that group.
        expect(
            screen.getByTestId('envelope-group-enforced')
        ).toHaveTextContent('Cost ceiling for one run');
    });

    it('saves declared terms and reports the signature consequence', async () => {
        const onChanged = vi.fn();
        render(<EnvelopePanel aiSystemId="sys-1" onChanged={onChanged} />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());

        fireEvent.change(
            screen.getByTestId('envelope-max_avg_cost_per_completed_task_usd'),
            { target: { value: '0.15' } }
        );
        fireEvent.click(screen.getByTestId('envelope-save'));

        await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
        expect(mockPut).toHaveBeenCalledWith('sys-1', {
            max_avg_cost_per_completed_task_usd: 0.15,
        });
        await waitFor(() =>
            expect(screen.getByTestId('envelope-notice').textContent).toMatch(
                /mint and approve/i
            )
        );
        expect(onChanged).toHaveBeenCalled();
    });

    // -- Enforced vs graded, and the explanations ------------------

    it('separates ceilings that stop traffic from bounds that only grade it', async () => {
        // The distinction an undifferentiated grid would hide: an operator
        // must never set a number believing it protects them when it only
        // describes them.
        render(<EnvelopePanel aiSystemId="sys-1" />);
        const enforced = await screen.findByTestId('envelope-group-enforced');
        const graded = screen.getByTestId('envelope-group-graded');

        expect(enforced).toHaveTextContent(/enforced/i);
        expect(enforced).toHaveTextContent('Cost ceiling for one run');
        expect(enforced).toHaveTextContent('Model calls per run');
        expect(enforced).toHaveTextContent('Tokens per run');

        expect(graded).toHaveTextContent(/graded/i);
        expect(graded).toHaveTextContent('Average cost per completed task');
        expect(graded).toHaveTextContent('Tasks that complete');
        // An enforced ceiling must never be filed under "graded".
        expect(graded).not.toHaveTextContent('Cost ceiling for one run');
    });

    it('every field carries the explanation the server sent', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-group-enforced');
        // One vocabulary, one explanation: the form must not invent its own
        // wording for a term the report also describes.
        const tips = screen.getAllByTestId('help-tip');
        expect(tips.length).toBeGreaterThanOrEqual(
            Object.keys(KNOWN_TERMS).length - 1
        );
    });

    it('count ceilings step in whole numbers, dollars in fractions', async () => {
        // A fractional call ceiling is either truncated silently or refused
        // by the server; the input should not offer it in the first place.
        render(<EnvelopePanel aiSystemId="sys-1" />);
        const calls = await screen.findByTestId('envelope-max_llm_calls_per_run');
        expect(calls).toHaveAttribute('step', '1');
        expect(
            screen.getByTestId('envelope-max_cost_per_run_usd')
        ).toHaveAttribute('step', '0.0001');
    });

    it('clearing every field clears the declaration', async () => {
        mockFetch.mockResolvedValue(resp({ max_cost_per_run_usd: 0.15 }));
        mockPut.mockResolvedValue(resp(null, true));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(
                (screen.getByTestId<HTMLInputElement>('envelope-max_cost_per_run_usd'))
                    .value
            ).toBe('0.15')
        );
        fireEvent.change(screen.getByTestId('envelope-max_cost_per_run_usd'), {
            target: { value: '' },
        });
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() =>
            expect(mockPut).toHaveBeenCalledWith('sys-1', null)
        );
    });

    it('surfaces a server refusal', async () => {
        mockPut.mockRejectedValue(new Error('unknown envelope term'));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());
        fireEvent.change(screen.getByTestId('envelope-min_completion_rate'), {
            target: { value: '0.9' },
        });
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() =>
            expect(screen.getByText(/unknown envelope term/)).toBeInTheDocument()
        );
    });

    it('save is disabled until something changes', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('envelope-save')).toBeInTheDocument()
        );
        expect(screen.getByTestId('envelope-save')).toBeDisabled();
    });
});

describe('EnvelopePanel — bounds per declared outcome', () => {
    it('lists the per-outcome terms in their own section, never as a bare field', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        const group = await screen.findByTestId('envelope-group-outcomes');
        expect(group).toHaveTextContent('Share of runs ending in an outcome');
        expect(group).toHaveTextContent('Average cost of an outcome');
        // A parameterised term has no field of its own: its key needs an outcome.
        expect(screen.queryByTestId('envelope-max_outcome_share')).toBeNull();
        expect(screen.queryByTestId('envelope-avg_cost_per_outcome')).toBeNull();
    });

    it('shows declared bounds by outcome and saves them under their keys', async () => {
        mockFetch.mockResolvedValue(resp({ 'max_outcome_share:no_applications_available': 0.9, max_tokens_per_run: 5000 }));
        mockPut.mockResolvedValue(resp({ 'max_outcome_share:no_applications_available': 0.8, max_tokens_per_run: 5000 }, true));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        const field = await screen.findByTestId('envelope-max_outcome_share:no_applications_available');
        expect(field).toHaveValue(0.9);
        expect(screen.getByTestId('envelope-outcome-term-max_outcome_share')).toHaveTextContent('no_applications_available');
        fireEvent.change(field, { target: { value: '0.8' } });
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() =>
            expect(mockPut).toHaveBeenCalledWith('sys-1', {
                'max_outcome_share:no_applications_available': 0.8,
                max_tokens_per_run: 5000,
            }),
        );
    });

    it('adds a bound for an outcome id and offers the declared ids', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-group-outcomes');
        await waitFor(() => expect(mockOutcomes).toHaveBeenCalledWith('sys-1'));
        // The outcome alone (covering its variants) and every reportable id.
        await waitFor(() => {
            const ids = Array.from(document.querySelectorAll('#envelope-outcome-ids option')).map((o) => o.getAttribute('value'));
            expect(ids).toEqual(['application_processed', 'application_processed.loan_approved', 'no_applications_available']);
        });
        fireEvent.change(screen.getByTestId('envelope-add-outcome-avg_cost_per_outcome'), {
            target: { value: 'application_processed.loan_approved' },
        });
        fireEvent.click(screen.getByTestId('envelope-add-avg_cost_per_outcome'));
        const field = screen.getByTestId('envelope-avg_cost_per_outcome:application_processed.loan_approved');
        fireEvent.change(field, { target: { value: '0.03' } });
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() =>
            expect(mockPut).toHaveBeenCalledWith('sys-1', { 'avg_cost_per_outcome:application_processed.loan_approved': 0.03 }),
        );
    });

    it('refuses a malformed outcome id instead of adding it', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-group-outcomes');
        fireEvent.change(screen.getByTestId('envelope-add-outcome-max_outcome_share'), { target: { value: 'Not An Id' } });
        fireEvent.click(screen.getByTestId('envelope-add-max_outcome_share'));
        expect(screen.getByTestId('envelope-add-error-max_outcome_share')).toHaveTextContent('Enter an outcome id');
        expect(screen.queryByTestId('envelope-max_outcome_share:Not An Id')).toBeNull();
    });

    it('removes a bound, and an added bound left empty is not declared', async () => {
        mockFetch.mockResolvedValue(resp({ 'avg_cost_per_outcome:application_processed': 0.05 }));
        mockPut.mockResolvedValue(resp(null));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-avg_cost_per_outcome:application_processed');
        fireEvent.click(screen.getByTestId('envelope-remove-avg_cost_per_outcome:application_processed'));
        expect(screen.queryByTestId('envelope-avg_cost_per_outcome:application_processed')).toBeNull();
        fireEvent.change(screen.getByTestId('envelope-add-outcome-max_outcome_share'), { target: { value: 'no_applications_available' } });
        fireEvent.click(screen.getByTestId('envelope-add-max_outcome_share'));
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() => expect(mockPut).toHaveBeenCalledWith('sys-1', null));
    });

    it('still edits per-outcome bounds when the catalogue cannot be read', async () => {
        mockOutcomes.mockRejectedValue(new Error('404'));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-group-outcomes');
        fireEvent.change(screen.getByTestId('envelope-add-outcome-max_outcome_share'), { target: { value: 'task_completed' } });
        fireEvent.click(screen.getByTestId('envelope-add-max_outcome_share'));
        expect(screen.getByTestId('envelope-max_outcome_share:task_completed')).toBeInTheDocument();
    });
});

/** The terms the gateway enforces and the report does not grade, as the Control Plane serves them. */
const ENFORCED_ONLY_TERMS = {
    max_actions_per_run: {
        direction: 'max', unit: 'count', label: 'Actions per run',
        signal: 'cost', enforced: true, graded: false, help: 'The most actions one run may take.',
    },
    max_cost_per_session_usd: {
        direction: 'max', unit: 'usd', label: 'Cost ceiling for one conversation',
        signal: 'cost', enforced: true, graded: false, help: 'No single conversation may spend more than this, across every message in it.',
    },
    max_new_runs_per_window: {
        direction: 'max', unit: 'count', label: 'New runs per credential in a window',
        signal: 'cost', enforced: true, graded: false, help: 'The most runs one credential may start in a window.',
    },
    new_runs_window_seconds: {
        direction: 'max', unit: 'seconds', label: 'Window for new runs',
        signal: 'cost', enforced: true, graded: false,
        help: 'The length, in seconds, of the window max_new_runs_per_window counts in: a whole number from 1 to 2678400 (31 days); an hour (3600) when not declared. Declared only together with max_new_runs_per_window.',
    },
};

function respWithEnforcedOnly(envelope: Record<string, number> | null) {
    return { ...resp(envelope), known_terms: { ...KNOWN_TERMS, ...ENFORCED_ONLY_TERMS } };
}

describe('EnvelopePanel — terms that are enforced and not graded', () => {
    beforeEach(() => {
        mockFetch.mockResolvedValue(respWithEnforcedOnly(null));
        mockPut.mockImplementation((_id: string, envelope: Record<string, number> | null) => Promise.resolve(respWithEnforcedOnly(envelope)));
    });

    it('offers the action ceiling among the ceilings for one run, with its label and help', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        const perRun = screen.getByTestId('envelope-group-enforced');
        expect(perRun).toContainElement(screen.getByTestId('envelope-max_actions_per_run'));
        expect(screen.getByLabelText('Actions per run')).toBe(screen.getByTestId('envelope-max_actions_per_run'));
        expect(screen.getByRole('button', { name: 'Help: Actions per run' })).toBeInTheDocument();
    });

    it('offers the cost ceiling of a conversation in a section of its own, not among the ceilings for one run', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        const conversation = screen.getByTestId('envelope-group-conversation');
        expect(conversation).toHaveTextContent('Ceiling for one conversation');
        expect(conversation).toHaveTextContent('enforced');
        expect(conversation).toContainElement(screen.getByTestId('envelope-max_cost_per_session_usd'));
        expect(screen.getByLabelText('Cost ceiling for one conversation')).toBe(screen.getByTestId('envelope-max_cost_per_session_usd'));
        // A conversation is many runs: its ceiling is not a ceiling of one run.
        expect(screen.getByTestId('envelope-group-enforced')).toHaveTextContent('Ceilings for one run');
        expect(screen.getByTestId('envelope-group-enforced')).not.toContainElement(screen.getByTestId('envelope-max_cost_per_session_usd'));
        // The sections in the order they are read: one run, one conversation, how many runs.
        const order = ['enforced', 'conversation', 'new-runs'].map((key) => screen.getByTestId(`envelope-group-${key}`));
        expect(order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(order[1].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it('offers the limit on new runs and its window in a section of their own', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        const newRuns = screen.getByTestId('envelope-group-new-runs');
        expect(newRuns).toHaveTextContent('Limit on new runs');
        expect(newRuns).toContainElement(screen.getByTestId('envelope-max_new_runs_per_window'));
        expect(newRuns).toContainElement(screen.getByTestId('envelope-new_runs_window_seconds'));
        expect(screen.getByLabelText('New runs per credential in a window')).toBe(screen.getByTestId('envelope-max_new_runs_per_window'));
        expect(screen.getByLabelText('Window for new runs')).toBe(screen.getByTestId('envelope-new_runs_window_seconds'));
        expect(screen.getByRole('button', { name: 'Help: Window for new runs' })).toBeInTheDocument();
        // Neither is a ceiling of one run.
        expect(screen.getByTestId('envelope-group-enforced')).not.toContainElement(screen.getByTestId('envelope-max_new_runs_per_window'));
        expect(screen.getByTestId('envelope-group-enforced')).not.toContainElement(screen.getByTestId('envelope-new_runs_window_seconds'));
    });

    it('says which terms the report states without grading', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        for (const term of Object.keys(ENFORCED_ONLY_TERMS)) {
            expect(screen.getByTestId(`envelope-not-graded-${term}`)).toHaveTextContent(
                'Enforced, not graded: the Assurance Report states this term and shows it as not graded.',
            );
        }
        expect(screen.queryByTestId('envelope-not-graded-max_cost_per_run_usd')).toBeNull();
        expect(screen.queryByTestId('envelope-not-graded-min_completion_rate')).toBeNull();
    });

    it('takes a window only together with the maximum it is the window of', async () => {
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        const window = screen.getByTestId('envelope-new_runs_window_seconds');
        expect(window).toBeDisabled();
        expect(screen.getByTestId('envelope-window-needs-maximum')).toHaveTextContent(
            'Declared only together with New runs per credential in a window.',
        );
        fireEvent.change(screen.getByTestId('envelope-max_new_runs_per_window'), { target: { value: '40' } });
        expect(window).toBeEnabled();
        expect(screen.queryByTestId('envelope-window-needs-maximum')).toBeNull();
        fireEvent.change(window, { target: { value: '600' } });
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() => expect(mockPut).toHaveBeenCalledWith('sys-1', { max_new_runs_per_window: 40, new_runs_window_seconds: 600 }));
    });

    it('drops the window when the maximum is cleared', async () => {
        mockFetch.mockResolvedValue(respWithEnforcedOnly({ max_new_runs_per_window: 40, new_runs_window_seconds: 600, max_actions_per_run: 12 }));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        const window = screen.getByTestId('envelope-new_runs_window_seconds');
        expect(window).toHaveValue(600);
        expect(window).toBeEnabled();
        fireEvent.change(screen.getByTestId('envelope-max_new_runs_per_window'), { target: { value: '' } });
        expect(window).toHaveValue(null);
        expect(window).toBeDisabled();
        fireEvent.click(screen.getByTestId('envelope-save'));
        await waitFor(() => expect(mockPut).toHaveBeenCalledWith('sys-1', { max_actions_per_run: 12 }));
    });

    it('shows the refusal of a window longer than the Control Plane keeps', async () => {
        mockPut.mockRejectedValue(new Error('new_runs_window_seconds must be at most 2678400 (31 days)'));
        mockFetch.mockResolvedValue(respWithEnforcedOnly({ max_new_runs_per_window: 40 }));
        render(<EnvelopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('envelope-panel');
        fireEvent.change(screen.getByTestId('envelope-new_runs_window_seconds'), { target: { value: '9999999' } });
        fireEvent.click(screen.getByTestId('envelope-save'));
        expect(await screen.findByText('new_runs_window_seconds must be at most 2678400 (31 days)')).toBeInTheDocument();
    });
});
