/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for `formatCostByMode` — the cell renderer used in
 * `ProviderModelsView` (Resources → AI Models → Providers →
 * drill-down) and any future view that wants the same per-row
 * cost display.
 *
 * Coverage targets: every mode branch (token, image, TTS, STT,
 * video), the missing-data → null contract, the precision-pick
 * boundary (≥1 cent vs <1 cent), and the per-1M-char scaling for
 * TTS so token-mode and TTS-mode values land in the same visual
 * register.
 */

import { describe, it, expect } from 'vitest';
import { formatCostByMode } from '../modelCostFormatting';

describe('formatCostByMode — token modes', () => {
    it('renders both input and output for chat models', () => {
        // gpt-4o-style pricing: $5/1M in, $15/1M out
        expect(
            formatCostByMode({
                mode: 'chat',
                cost_per_input_token: 5,
                cost_per_output_token: 15,
            }),
        ).toBe('$5.00 / 1M in · $15.00 out');
    });

    it('renders only input when output is missing', () => {
        expect(
            formatCostByMode({
                mode: 'chat',
                cost_per_input_token: 0.5,
            }),
        ).toBe('$0.50 / 1M in');
    });

    it('renders only output when input is missing (rare but valid)', () => {
        expect(
            formatCostByMode({
                mode: 'chat',
                cost_per_output_token: 1.25,
            }),
        ).toBe('$1.25 out');
    });

    it('returns null when neither input nor output is set', () => {
        expect(
            formatCostByMode({ mode: 'chat' }),
        ).toBeNull();
    });

    it('returns null when costs are zero (treats 0 as "unset")', () => {
        // Operators that put `0` in the field haven't really
        // configured pricing — show the em-dash so the column
        // stays scannable for the rows that DO have pricing.
        expect(
            formatCostByMode({
                mode: 'chat',
                cost_per_input_token: 0,
                cost_per_output_token: 0,
            }),
        ).toBeNull();
    });

    it('handles other token-priced modes the same way (embedding, completion, responses, …)', () => {
        for (const mode of ['embedding', 'completion', 'responses', 'rerank', 'moderation']) {
            expect(
                formatCostByMode({
                    mode,
                    cost_per_input_token: 0.13,
                    cost_per_output_token: 0,
                }),
            ).toBe('$0.13 / 1M in');
        }
    });

    it('falls back to token formatting when mode is missing/null/unknown', () => {
        // No `mode` field on the model row → treat as token-priced.
        expect(
            formatCostByMode({
                cost_per_input_token: 1,
                cost_per_output_token: 2,
            }),
        ).toBe('$1.00 / 1M in · $2.00 out');
    });
});

describe('formatCostByMode — image_generation', () => {
    it('renders per-image cost when set (2-decimal at ≥ 1¢)', () => {
        // dall-e-3: $0.04 / image at 1024x1024 — above the 1¢
        // threshold so 2-decimal precision wins.
        expect(
            formatCostByMode({
                mode: 'image_generation',
                cost_per_image: 0.04,
            }),
        ).toBe('$0.04 / image');
    });

    it('renders sub-cent per-image cost with 4-decimal precision', () => {
        // Some hosted-OSS image models bill < 1¢; the higher
        // precision keeps that visible rather than rounding to "$0.00".
        expect(
            formatCostByMode({
                mode: 'image_generation',
                cost_per_image: 0.0035,
            }),
        ).toBe('$0.0035 / image');
    });

    it('uses 2-decimal precision when cost is ≥ 1 cent ON THE DOT', () => {
        // Boundary: exactly $0.01 should use the 2-decimal path.
        expect(
            formatCostByMode({
                mode: 'image_generation',
                cost_per_image: 0.01,
            }),
        ).toBe('$0.01 / image');
    });

    it('uses 2-decimal precision for higher-tier images', () => {
        // dall-e-3 hd / 1792x1024 sits at $0.12.
        expect(
            formatCostByMode({
                mode: 'image_generation',
                cost_per_image: 0.12,
            }),
        ).toBe('$0.12 / image');
    });

    it('returns null when image cost is missing', () => {
        expect(
            formatCostByMode({ mode: 'image_generation' }),
        ).toBeNull();
    });

    it('returns null when image cost is zero', () => {
        expect(
            formatCostByMode({
                mode: 'image_generation',
                cost_per_image: 0,
            }),
        ).toBeNull();
    });

    it('ignores token costs even when present (image mode does not bill by token)', () => {
        // Defensive: a copy/paste of token costs onto an image
        // model shouldn't accidentally surface in the cell.
        expect(
            formatCostByMode({
                mode: 'image_generation',
                cost_per_input_token: 5,
                cost_per_output_token: 10,
            }),
        ).toBeNull();
    });
});

describe('formatCostByMode — audio_speech (TTS)', () => {
    it('scales per-character cost to per-1M chars (matches token-mode visual register)', () => {
        // OpenAI tts-1: $15 / 1M chars → 0.000015 per char.
        expect(
            formatCostByMode({
                mode: 'audio_speech',
                cost_per_audio_char: 0.000015,
            }),
        ).toBe('$15.00 / 1M chars');
    });

    it('returns null when char-pricing is absent (there is no per-second price)', () => {
        // There is no cost_per_audio_second — TTS cost is char-based.
        expect(
            formatCostByMode({
                mode: 'audio_speech',
            }),
        ).toBeNull();
    });

    it('returns null when no audio cost is set', () => {
        expect(
            formatCostByMode({ mode: 'audio_speech' }),
        ).toBeNull();
    });
});

describe('formatCostByMode — audio_transcription (STT)', () => {
    it('renders per-minute cost', () => {
        // OpenAI whisper-1: $0.006 / minute
        expect(
            formatCostByMode({
                mode: 'audio_transcription',
                cost_per_audio_minute: 0.006,
            }),
        ).toBe('$0.0060 / min');
    });

    it('uses 2-decimal precision at higher per-minute prices', () => {
        // E.g. AssemblyAI's premium tier sits north of 1 cent.
        expect(
            formatCostByMode({
                mode: 'audio_transcription',
                cost_per_audio_minute: 0.025,
            }),
        ).toBe('$0.03 / min');
    });

    it('returns null when minute cost is missing', () => {
        expect(
            formatCostByMode({ mode: 'audio_transcription' }),
        ).toBeNull();
    });
});

describe('formatCostByMode — video_generation', () => {
    it('renders per-second cost', () => {
        // luma-dream-machine via Fal: $0.10 / sec.
        expect(
            formatCostByMode({
                mode: 'video_generation',
                cost_per_video_second: 0.1,
            }),
        ).toBe('$0.10 / sec');
    });

    it('uses 4-decimal precision for sub-cent per-second pricing', () => {
        expect(
            formatCostByMode({
                mode: 'video_generation',
                cost_per_video_second: 0.0042,
            }),
        ).toBe('$0.0042 / sec');
    });

    it('returns null when video cost is missing', () => {
        expect(
            formatCostByMode({ mode: 'video_generation' }),
        ).toBeNull();
    });
});
