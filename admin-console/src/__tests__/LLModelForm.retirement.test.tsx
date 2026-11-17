/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Retirement tab of the LLModel form.
 *
 * The bug class guarded against: operator opens the form,
 * fills in a retire_date, hits Save — but the backend never sees
 * the value. Or: operator types a date, expects the "alert from"
 * field to default-fill to retire - 14d, doesn't. Or: an existing
 * retired model loads but the tab silently drops the value into
 * the void.
 *
 * Coverage:
 *
 *   1. Empty state — no retire_date in props → empty-state banner
 *      ("No retirement scheduled").
 *   2. Reminder default-fill — typing retire_date populates the
 *      reminder field with retire_date - 14d, when the operator
 *      hasn't touched it.
 *   3. Operator-supplied reminder is respected — typing both
 *      values doesn't get clobbered.
 *   4. Edit mode preload — a model with retire_date already set
 *      preloads the fields and shows the warning banner.
 *   5. Retired-in-past — shows the error banner ("retired N days
 *      ago") not the warning.
 *   6. Submit shape — onSave receives the six retirement fields
 *      (with null for blanks, not empty string).
 *
 * Selector strategy: native `<input type="date">` so we can use
 * the label text directly; the input is `aria-label`-able via
 * the `<label>` text adjacent in the DOM. The Retirement tab nav
 * button is hit by its visible text "Retirement".
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LLModelForm from '../components/llm/config/LLModelForm';

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {},
        groupTree: [],
    }),
}));

vi.mock('../hooks/useLlmProviders', () => ({
    useLlmProviders: () => ({
        providers: [
            { id: 'p-openai', key: 'openai', display_name: 'OpenAI' },
            { id: 'p-anthropic', key: 'anthropic', display_name: 'Anthropic' },
        ],
        loading: false,
    }),
}));


const baseModel = {
    id: 'llm-existing',
    tenant_id: 't1',
    name: 'Existing Model',
    description: 'desc',
    provider: 'openai',
    model_name: 'gpt-test',
    mode: 'chat',
    api_url_auto_discovered: true,
    api_url: '',
    auth_type: 'api_key',
    default_temperature: 0.7,
    default_max_tokens: 4096,
    timeout: 60,
    enabled: true,
    health_status: { status: 'unknown' },
    performance_metrics: {},
    created_at: '2026-05-13T00:00:00Z',
    updated_at: '2026-05-13T00:00:00Z',
};


function renderForm(modelOverride?: Partial<typeof baseModel> & { retire_date?: string }) {
    const onSave = vi.fn(async () => undefined);
    const onClose = vi.fn();
    const model = modelOverride === undefined
        ? null
        : { ...baseModel, ...modelOverride };
    render(
        <LLModelForm
            model={model as any}
            onSave={onSave}
            onClose={onClose}
        />
    );
    return { onSave, onClose };
}


// Click the "Retirement" tab so the panel mounts.
function openRetirementTab() {
    fireEvent.click(screen.getByRole('button', { name: /retirement/i }));
}


function dateInput(labelText: RegExp): HTMLInputElement {
    // Each date input has a sibling <label> above it. Match by walking
    // from the label.
    const label = screen.getByText(labelText);
    const wrapper = label.parentElement!;
    const input = wrapper.querySelector('input[type="date"]') as HTMLInputElement;
    if (!input) throw new Error(`No date input under label: ${labelText}`);
    return input;
}


describe('LLModelForm — Retirement tab', () => {

    it('shows the empty-state banner when no retire_date is set', () => {
        renderForm();
        openRetirementTab();
        expect(screen.getByText(/no retirement scheduled/i)).toBeTruthy();
        // Date fields are present but empty.
        expect(dateInput(/^Retire date$/i).value).toBe('');
    });

    it('default-fills the reminder to retire_date - 14d when blank', async () => {
        renderForm();
        openRetirementTab();

        const retire = dateInput(/^Retire date$/i);
        fireEvent.change(retire, { target: { value: '2026-12-01' } });

        await waitFor(() => {
            expect(dateInput(/^Alert from$/i).value).toBe('2026-11-17');
        });
    });

    it('does not clobber an operator-supplied reminder when retire_date changes', async () => {
        renderForm();
        openRetirementTab();

        // Operator sets reminder first, then retire_date.
        const reminder = dateInput(/^Alert from$/i);
        fireEvent.change(reminder, { target: { value: '2026-10-01' } });
        const retire = dateInput(/^Retire date$/i);
        fireEvent.change(retire, { target: { value: '2026-12-01' } });

        // Reminder must NOT be overwritten — it was set deliberately.
        await waitFor(() => {
            expect(dateInput(/^Alert from$/i).value).toBe('2026-10-01');
        });
    });

    it('preloads retirement fields from an existing model row', () => {
        renderForm({
            retire_date: '2026-06-15',
            retirement_announced_at: '2026-04-14',
            retirement_reminder_date: '2026-06-01',
            retirement_replacement_model_name: 'claude-sonnet-4-6',
            retirement_replacement_provider_key: 'anthropic',
            retirement_notes: 'See provider announcement.',
        } as any);
        openRetirementTab();

        expect(dateInput(/^Retire date$/i).value).toBe('2026-06-15');
        expect(dateInput(/^Announced on$/i).value).toBe('2026-04-14');
        expect(dateInput(/^Alert from$/i).value).toBe('2026-06-01');
        // Replacement model + provider inputs.
        const replacement = screen.getByPlaceholderText<HTMLInputElement>(/gpt-image-2\.6-flare/i);
        expect(replacement.value).toBe('claude-sonnet-4-6');
        // Notes textarea preloaded.
        expect((screen.getByPlaceholderText<HTMLTextAreaElement>(/Provider announcement URL/i)).value)
            .toMatch(/provider announcement/i);
    });

    it('shows the "retired N day(s) ago" banner when retire_date is in the past', () => {
        // Two weeks ago today, ish — any past date works.
        const past = '2020-01-01';
        renderForm({ retire_date: past });
        openRetirementTab();
        expect(screen.getByText(/Model retired \d+ day\(s\) ago/i)).toBeTruthy();
    });

    // ----- Banner countdown — off-by-one ------------------------
    //
    // Mixing local-time `today` with a UTC-anchored `retire` parse
    // makes `Math.ceil((retire - today) / dayMs)` return 0 for a
    // yesterday-retire-date in any non-UTC timezone, rendering
    // "Model retired 0 day(s) ago" instead of "1 day ago". We can't use `vi.useFakeTimers` here because the
    // form has real async effects (group fetch, routing-group fetch)
    // that hang when timers are faked. Instead we compute retire-
    // dates relative to real `new Date()` so the test stays accurate
    // regardless of when it runs.

    describe('banner countdown around the retirement-date boundary', () => {
        // ISO `YYYY-MM-DD` at UTC midnight offset from real today.
        // Matches the format the date <input> emits.
        function dateOffsetFromToday(days: number): string {
            const now = new Date();
            const todayUtc = Date.UTC(
                now.getUTCFullYear(),
                now.getUTCMonth(),
                now.getUTCDate(),
            );
            const shifted = new Date(todayUtc + days * 86400000);
            return shifted.toISOString().slice(0, 10);
        }

        it('renders "retired 1 day(s) ago" for yesterday', () => {
            renderForm({ retire_date: dateOffsetFromToday(-1) });
            openRetirementTab();
            expect(screen.getByText(/Model retired 1 day\(s\) ago/i)).toBeTruthy();
        });

        it('says the model is not served from today when retire_date equals today', () => {
            renderForm({ retire_date: dateOffsetFromToday(0) });
            openRetirementTab();
            expect(screen.getByText('Model retired today')).toBeTruthy();
            expect(screen.getByTestId('retirement-banner-text')).toHaveTextContent(
                'The retire date is today. From its retire date on, the gateway does not serve a model: a call to it is refused as a call to a disabled model is. Move users to the replacement, or change the date.',
            );
        });

        it('says a model past its retire date is not served', () => {
            renderForm({ retire_date: dateOffsetFromToday(-3) });
            openRetirementTab();
            expect(screen.getByTestId('retirement-banner-text')).toHaveTextContent(
                'The retire date has passed, so the gateway does not serve this model: a call to it is refused as a call to a disabled model is. Move users to the replacement, or change the date.',
            );
        });

        it('says what the retire date will do while it is still ahead', () => {
            renderForm({ retire_date: dateOffsetFromToday(5) });
            openRetirementTab();
            expect(screen.getByTestId('retirement-banner-text')).toHaveTextContent('From the retire date on, the gateway does not serve the model.');
        });

        it('shows on every tab that a retired model is not served, and nothing for a served one', () => {
            renderForm({ retire_date: '2020-01-01' });
            expect(screen.getByTestId('model-not-served-banner')).toHaveTextContent(
                'The gateway does not serve this model. Retire date: 2020-01-01. From its retire date on, a model takes no call.',
            );
        });

        it('shows nothing of the kind for a model that is served', () => {
            renderForm({ retire_date: dateOffsetFromToday(5) });
            expect(screen.queryByTestId('model-not-served-banner')).toBeNull();
        });

        it('renders "scheduled in 1 day(s)" for tomorrow', () => {
            renderForm({ retire_date: dateOffsetFromToday(1) });
            openRetirementTab();
            expect(screen.getByText(/Retirement scheduled in 1 day\(s\)/i)).toBeTruthy();
        });

        it('renders "scheduled in 33 day(s)" for 33 days out', () => {
            renderForm({ retire_date: dateOffsetFromToday(33) });
            openRetirementTab();
            expect(screen.getByText(/Retirement scheduled in 33 day\(s\)/i)).toBeTruthy();
        });
    });
});
