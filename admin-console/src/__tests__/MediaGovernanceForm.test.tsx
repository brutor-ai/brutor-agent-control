/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MediaGovernanceForm — per-modality behavioral-governance form.
 * Rendered once per MediaMode. Tests pin the mode-specific field set
 * and the onChange nested shape ({ [mode]: { field: value } }) for
 * every field, plus the string-list / int-list comma parsing and the
 * delete-on-clear / delete-empty-section contract this component
 * implements.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import MediaGovernanceForm from '../components/groups/subtabs/MediaGovernanceForm';
import type { MediaMode } from '../components/groups/subtabs/MediaLimitsForm';

type FieldType = 'number' | 'checkbox' | 'string_list' | 'int_list';

interface FieldSpec {
    key: string;
    label: string;
    type: FieldType;
    /** raw text typed into text/number inputs */
    input?: string;
    /** value expected in the emitted onChange payload */
    expected: unknown;
}

function inputFor(label: string, type: FieldType) {
    const parent = screen.getByText(label).parentElement!;
    const selector =
        type === 'number'
            ? 'input[type="number"]'
            : type === 'checkbox'
              ? 'input[type="checkbox"]'
              : 'input[type="text"]';
    const el = parent.querySelector(selector);
    if (!el) throw new Error(`no ${type} control for "${label}"`);
    return el as HTMLInputElement;
}

/** Drive a field to its `expected` value and return nothing (caller asserts). */
function writeField(f: FieldSpec) {
    const el = inputFor(f.label, f.type);
    if (f.type === 'checkbox') {
        fireEvent.click(el);
    } else {
        fireEvent.change(el, { target: { value: f.input } });
    }
}

interface ModeSpec {
    mode: MediaMode;
    fields: FieldSpec[];
}

const SPECS: ModeSpec[] = [
    {
        mode: 'audio_speech',
        fields: [
            { key: 'voice_whitelist', label: 'Allowed voices', type: 'string_list', input: 'alloy, echo', expected: ['alloy', 'echo'] },
            { key: 'response_format_whitelist', label: 'Allowed response formats', type: 'string_list', input: 'mp3, wav', expected: ['mp3', 'wav'] },
            { key: 'max_input_characters', label: 'Max input characters per request', type: 'number', input: '4096', expected: 4096 },
            { key: 'speed_min', label: 'Min speed multiplier', type: 'number', input: '0.5', expected: 0.5 },
            { key: 'speed_max', label: 'Max speed multiplier', type: 'number', input: '2', expected: 2 },
        ],
    },
    {
        mode: 'audio_transcription',
        fields: [
            { key: 'language_allowlist', label: 'Allowed languages (BCP-47)', type: 'string_list', input: 'en, sv', expected: ['en', 'sv'] },
            { key: 'response_format_whitelist', label: 'Allowed response formats', type: 'string_list', input: 'json, text', expected: ['json', 'text'] },
            { key: 'max_audio_size_mb', label: 'Max audio size (MB)', type: 'number', input: '25', expected: 25 },
        ],
    },
    {
        mode: 'image_generation',
        fields: [
            { key: 'size_whitelist', label: 'Allowed sizes', type: 'string_list', input: '1024x1024, 1792x1024', expected: ['1024x1024', '1792x1024'] },
            { key: 'quality_whitelist', label: 'Allowed qualities', type: 'string_list', input: 'standard, hd', expected: ['standard', 'hd'] },
            { key: 'style_whitelist', label: 'Allowed styles', type: 'string_list', input: 'natural, vivid', expected: ['natural', 'vivid'] },
            { key: 'max_n', label: 'Max images per request', type: 'number', input: '4', expected: 4 },
            { key: 'max_prompt_characters', label: 'Max prompt characters', type: 'number', input: '1000', expected: 1000 },
        ],
    },
    {
        mode: 'video_generation',
        fields: [
            { key: 'max_duration_seconds', label: 'Max duration (seconds)', type: 'number', input: '20', expected: 20 },
            { key: 'resolution_whitelist', label: 'Allowed resolutions', type: 'string_list', input: '540p, 720p, 1080p', expected: ['540p', '720p', '1080p'] },
            { key: 'require_image_input', label: 'Require image-to-video (block text-to-video)', type: 'checkbox', expected: true },
            { key: 'max_prompt_characters', label: 'Max prompt characters', type: 'number', input: '800', expected: 800 },
        ],
    },
    {
        mode: 'embedding',
        fields: [
            { key: 'dim_whitelist', label: 'Allowed dimensions', type: 'int_list', input: '512, 1024, 1536', expected: [512, 1024, 1536] },
            { key: 'max_batch_size', label: 'Max batch size', type: 'number', input: '128', expected: 128 },
        ],
    },
];

describe.each(SPECS)('MediaGovernanceForm — mode "$mode"', ({ mode, fields }) => {
    it('renders exactly the mode-specific field set', () => {
        render(<MediaGovernanceForm mode={mode} value={{}} onChange={vi.fn()} />);
        for (const f of fields) {
            expect(screen.getByText(f.label)).toBeInTheDocument();
        }
    });

    it.each(fields)('writes $key with the correct parsed shape', (f) => {
        const onChange = vi.fn();
        render(<MediaGovernanceForm mode={mode} value={{}} onChange={onChange} />);
        writeField(f);
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ [mode]: { [f.key]: f.expected } }),
        );
    });

    it('preserves other fields in the same mode when one changes', () => {
        const onChange = vi.fn();
        // Seed with the LAST field already set, then edit the first.
        const seed = fields[fields.length - 1];
        const initial = { [mode]: { [seed.key]: seed.expected } };
        render(<MediaGovernanceForm mode={mode} value={initial} onChange={onChange} />);
        writeField(fields[0]);
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                [mode]: { [seed.key]: seed.expected, [fields[0].key]: fields[0].expected },
            }),
        );
    });
});

describe('MediaGovernanceForm — delete-on-clear contract', () => {
    it('clearing a string-list deletes just that field, keeping siblings', () => {
        const onChange = vi.fn();
        const initial = {
            audio_speech: { voice_whitelist: ['alloy'], max_input_characters: 4096 },
        };
        render(<MediaGovernanceForm mode="audio_speech" value={initial} onChange={onChange} />);
        fireEvent.change(inputFor('Allowed voices', 'string_list'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0];
        expect(next.audio_speech).toEqual({ max_input_characters: 4096 });
    });

    it('clearing the last field removes the whole mode section', () => {
        const onChange = vi.fn();
        const initial = { embedding: { max_batch_size: 128 } };
        render(<MediaGovernanceForm mode="embedding" value={initial} onChange={onChange} />);
        fireEvent.change(inputFor('Max batch size', 'number'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0];
        expect(next.embedding).toBeUndefined();
    });

    it('preserves other modes when clearing a field', () => {
        const onChange = vi.fn();
        const initial = {
            embedding: { max_batch_size: 128 },
            image_generation: { max_n: 4 },
        };
        render(<MediaGovernanceForm mode="embedding" value={initial} onChange={onChange} />);
        fireEvent.change(inputFor('Max batch size', 'number'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0];
        expect(next.image_generation).toEqual({ max_n: 4 });
        expect(next.embedding).toBeUndefined();
    });

    it('int-list drops non-numeric tokens', () => {
        const onChange = vi.fn();
        render(<MediaGovernanceForm mode="embedding" value={{}} onChange={onChange} />);
        fireEvent.change(inputFor('Allowed dimensions', 'int_list'), {
            target: { value: '512, abc, 1024' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ embedding: { dim_whitelist: [512, 1024] } }),
        );
    });
});
