/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MediaLimitsForm — per-modality usage-limits form. Rendered once per
 * MediaMode; the parent holds the limits dict keyed by mode. These
 * tests pin the mode-specific field set and the onChange nested shape
 * ({ [mode]: { field: value } }) for every field, per mode.
 *
 * NOTE: unlike LLMLimitsForm, this component's `updateField` does not
 * delete keys/sections on clear — it spreads and sets. Tests therefore
 * assert write + preserve rather than delete-on-clear.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import MediaLimitsForm, { MediaMode } from '../components/groups/subtabs/MediaLimitsForm';

function numberInput(label: string) {
    const el = screen.getByText(label).parentElement!.querySelector('input[type="number"]');
    if (!el) throw new Error(`no number input for "${label}"`);
    return el as HTMLInputElement;
}

function checkbox(label: string) {
    const el = screen.getByText(label).parentElement!.querySelector('input[type="checkbox"]');
    if (!el) throw new Error(`no checkbox for "${label}"`);
    return el as HTMLInputElement;
}

const HARD_STOP = 'Hard stop when budget exceeded';

interface ModeSpec {
    mode: MediaMode;
    numberFields: { key: string; label: string; value: number }[];
    hasHardStop: boolean;
}

const SPECS: ModeSpec[] = [
    {
        mode: 'image_generation',
        hasHardStop: false,
        numberFields: [
            { key: 'daily_budget_usd', label: 'Daily budget (USD)', value: 10 },
            { key: 'daily_images', label: 'Daily images', value: 50 },
        ],
    },
    {
        mode: 'audio_speech',
        hasHardStop: false,
        numberFields: [
            { key: 'daily_budget_usd', label: 'Daily budget (USD)', value: 5 },
            { key: 'daily_requests', label: 'Daily requests', value: 20 },
            { key: 'daily_characters', label: 'Daily input characters synthesized', value: 10000 },
        ],
    },
    {
        mode: 'audio_transcription',
        hasHardStop: false,
        numberFields: [
            { key: 'daily_budget_usd', label: 'Daily budget (USD)', value: 5 },
            { key: 'daily_requests', label: 'Daily requests', value: 20 },
            { key: 'daily_audio_seconds', label: 'Daily audio seconds transcribed', value: 3600 },
        ],
    },
    {
        mode: 'video_generation',
        hasHardStop: false,
        numberFields: [
            { key: 'daily_budget_usd', label: 'Daily budget (USD)', value: 25 },
            { key: 'daily_video_seconds', label: 'Daily video seconds', value: 120 },
            { key: 'daily_requests', label: 'Daily requests', value: 10 },
        ],
    },
];

describe.each(SPECS)('MediaLimitsForm — mode "$mode"', ({ mode, numberFields, hasHardStop }) => {
    it('renders exactly the mode-specific field set', () => {
        render(<MediaLimitsForm mode={mode} value={{}} onChange={vi.fn()} />);
        for (const f of numberFields) {
            expect(screen.getByText(f.label)).toBeInTheDocument();
        }
        if (hasHardStop) {
            expect(screen.getByText(HARD_STOP)).toBeInTheDocument();
        } else {
            expect(screen.queryByText(HARD_STOP)).not.toBeInTheDocument();
        }
    });

    it.each(numberFields)('writes $key under the mode key', ({ key, label, value }) => {
        const onChange = vi.fn();
        render(<MediaLimitsForm mode={mode} value={{}} onChange={onChange} />);
        fireEvent.change(numberInput(label), { target: { value: String(value) } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ [mode]: { [key]: value } }),
        );
    });

    if (hasHardStop) {
        it('toggles hard_stop as a boolean', () => {
            const onChange = vi.fn();
            render(<MediaLimitsForm mode={mode} value={{}} onChange={onChange} />);
            fireEvent.click(checkbox(HARD_STOP));
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ [mode]: { hard_stop: true } }),
            );
        });
    }

    it('preserves existing values in the same mode when one field changes', () => {
        const onChange = vi.fn();
        const first = numberFields[0];
        const second = numberFields[1];
        const initial = { [mode]: { [first.key]: first.value } };
        render(<MediaLimitsForm mode={mode} value={initial} onChange={onChange} />);
        fireEvent.change(numberInput(second.label), { target: { value: String(second.value) } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                [mode]: { [first.key]: first.value, [second.key]: second.value },
            }),
        );
    });

    it('preserves other modes untouched when editing this mode', () => {
        const onChange = vi.fn();
        const other = mode === 'audio_speech' ? 'image_generation' : 'audio_speech';
        const initial = { [other]: { daily_budget_usd: 999 } };
        render(<MediaLimitsForm mode={mode} value={initial} onChange={onChange} />);
        fireEvent.change(numberInput(numberFields[0].label), {
            target: { value: String(numberFields[0].value) },
        });
        const next = onChange.mock.calls[0][0];
        expect(next[other]).toEqual({ daily_budget_usd: 999 });
        expect(next[mode]).toEqual({ [numberFields[0].key]: numberFields[0].value });
    });
});
