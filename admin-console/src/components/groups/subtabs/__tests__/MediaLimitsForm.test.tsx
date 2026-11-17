/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The per-modality media limits form.
 *
 * Each modality is billed on a different axis — TTS per character, STT per
 * audio second, image generation per image, embeddings per input token — so
 * the field set is not shared and the wrong axis on screen is a limit the
 * operator cannot actually set. The field NAMES are also load-bearing: they
 * mirror the control plane's Pydantic models exactly so a policy bundle
 * round-trips without a mapping layer.
 *
 * The form edits one modality at a time out of a dict that holds all five,
 * so the merge has to leave the other four untouched — and "cleared" has to
 * reach the caller as `undefined` (no limit), never as 0 (block everything).
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import MediaLimitsForm, { MEDIA_MODES, MEDIA_MODE_META, type MediaMode } from '../MediaLimitsForm';

function renderForm(mode: MediaMode, value: Record<string, unknown> = {}) {
    const onChange = vi.fn();
    const view = render(<MediaLimitsForm mode={mode} value={value} onChange={onChange} />);
    return { onChange, ...view };
}

describe('MediaLimitsForm — the modality catalogue', () => {
    it('exposes all five governed modalities', () => {
        expect(MEDIA_MODES).toEqual([
            'audio_speech',
            'audio_transcription',
            'image_generation',
            'video_generation',
            'embedding',
        ]);
    });

    it('publishes a label and icon for every mode, for the caller’s switcher', () => {
        expect(MEDIA_MODE_META.map((m) => m.id)).toEqual(MEDIA_MODES);
        for (const meta of MEDIA_MODE_META) {
            expect(meta.label).toBeTruthy();
            expect(meta.icon).toBeTruthy();
        }
    });
});

describe('MediaLimitsForm — each modality gets its own billing axis', () => {
    it('TTS is capped on characters, not on audio seconds', () => {
        renderForm('audio_speech');
        expect(screen.getByText('Daily input characters synthesized')).toBeInTheDocument();
        expect(screen.getByText(/bills per character/)).toBeInTheDocument();
        expect(screen.queryByText(/audio seconds/i)).toBeNull();
    });

    it('STT is capped on audio seconds, not on characters', () => {
        renderForm('audio_transcription');
        expect(screen.getByText('Daily audio seconds transcribed')).toBeInTheDocument();
        expect(screen.queryByText(/characters/i)).toBeNull();
    });

    it('image generation counts images', () => {
        renderForm('image_generation');
        expect(screen.getByText('Daily images')).toBeInTheDocument();
        expect(screen.queryByText('Monthly images')).toBeNull();
    });

    it('video generation counts seconds of video', () => {
        renderForm('video_generation');
        expect(screen.getByText('Daily video seconds')).toBeInTheDocument();
    });

    it('embeddings have no limit of their own: the group\'s LLM limits bound them', () => {
        renderForm('embedding');
        expect(screen.queryByText('Daily input tokens embedded')).toBeNull();
        expect(screen.queryAllByPlaceholderText('(unset)')).toHaveLength(0);
    });

    it('every modality but embeddings carries a daily dollar budget, and none a monthly one', () => {
        for (const mode of MEDIA_MODES.filter((m) => m !== 'embedding')) {
            const { unmount } = renderForm(mode);
            expect(screen.getByText('Daily budget (USD)')).toBeInTheDocument();
            expect(screen.queryByText('Monthly budget (USD)')).toBeNull();
            unmount();
        }
    });
});

describe('MediaLimitsForm — editing', () => {
    it('reads back the stored value for the mode on screen', () => {
        renderForm('image_generation', { image_generation: { daily_images: 25 } });
        expect(screen.getByDisplayValue('25')).toBeInTheDocument();
    });

    it('an unset field shows the placeholder, not a zero', () => {
        renderForm('image_generation');
        const inputs = screen.getAllByPlaceholderText('(unset)');
        expect(inputs.length).toBeGreaterThan(0);
        expect(inputs[0]).toHaveValue(null);
    });

    it('an edit merges into the mode without disturbing the others', () => {
        const { onChange } = renderForm('image_generation', {
            audio_speech: { daily_characters: 1000 },
            image_generation: { daily_images: 500 },
        });
        fireEvent.change(screen.getByDisplayValue('500'), { target: { value: '600' } });
        expect(onChange).toHaveBeenCalledWith({
            audio_speech: { daily_characters: 1000 },
            image_generation: { daily_images: 600 },
        });
    });

    it('clearing a field means "no limit" — undefined, never 0', () => {
        const { onChange } = renderForm('image_generation', { image_generation: { daily_images: 25 } });
        fireEvent.change(screen.getByDisplayValue('25'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ image_generation: { daily_images: undefined } });
    });

    it('a mode with nothing stored yet still accepts its first limit', () => {
        const { onChange } = renderForm('image_generation', { audio_speech: { daily_requests: 5 } });
        const inputs = screen.getAllByPlaceholderText('(unset)');
        fireEvent.change(inputs[0], { target: { value: '12' } });
        expect(onChange).toHaveBeenCalledWith({
            audio_speech: { daily_requests: 5 },
            image_generation: { daily_budget_usd: 12 },
        });
    });

    it('a read-only caller gets fields it cannot edit', () => {
        render(
            <MediaLimitsForm mode="image_generation" value={{}} onChange={vi.fn()} disabled />,
        );
        for (const input of screen.getAllByPlaceholderText('(unset)')) {
            expect(input).toBeDisabled();
        }
    });

    it('no limit can be driven negative', () => {
        renderForm('video_generation');
        for (const input of screen.getAllByPlaceholderText('(unset)')) {
            expect(input).toHaveAttribute('min', '0');
        }
    });
});
