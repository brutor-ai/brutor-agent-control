/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Single-cell cost rendering for the admin model list views.
 *
 * Each LLM mode has its own pricing dimension:
 *   - Token-based modes (chat, completion, responses, embedding,
 *     rerank, moderation, routing) — bill per token. Provider
 *     pricing pages quote in $/1M tokens; we display the same
 *     literally because operators tune cost-per-million when
 *     comparing models across vendors. The raw
 *     `cost_per_input_token` / `cost_per_output_token` fields
 *     are already in $/1M units (see seeds: gpt-4 reads as 5.0).
 *   - `image_generation` — bill per output image. Most providers
 *     charge a flat per-image rate that's quality-tier-dependent
 *     but mode-agnostic; we surface the headline number.
 *   - `audio_speech` — bill per output character (OpenAI tts-1)
 *     or per generated second (ElevenLabs). The raw
 *     `cost_per_audio_char` field is per single character; we
 *     scale it to $/1M chars so the displayed number is human
 *     readable.
 *   - `audio_transcription` — bill per minute of input audio
 *     (Whisper, Deepgram, AssemblyAI all use this unit).
 *   - `video_generation` — bill per second of output clip (Luma,
 *     Kling, Runway).
 *
 * `null` means "no pricing on file"; the table renders an em-dash
 * so the column stays scannable when an operator hasn't filled in
 * pricing yet.
 *
 * Pulled out of `ProviderModelsView.tsx` so it can be unit-tested
 * without the React component tree, and reused if a future view
 * (e.g. Resources → AI Models top-level table) wants the same
 * cell.
 */

interface CostFields {
    mode?: string | null;
    cost_per_input_token?: number;
    cost_per_output_token?: number;
    cost_per_image?: number;
    cost_per_audio_char?: number;
    cost_per_audio_minute?: number;
    cost_per_video_second?: number;
}

/** $X.XX — used for token costs already in /1M units. */
function usdToken(n: number): string {
    return `$${n.toFixed(2)}`;
}

/**
 * $X.XX for media costs ≥ 1 cent; $X.XXXX otherwise. Per-second
 * video (luma at $0.10/sec) and per-image dall-e-3 ($0.040/image)
 * land on opposite sides of 1 cent — picking precision dynamically
 * keeps them both readable in the same column.
 */
function usdMedia(n: number): string {
    return n >= 0.01 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

export function formatCostByMode(model: CostFields): string | null {
    switch (model.mode) {
        case 'image_generation': {
            if (typeof model.cost_per_image === 'number' && model.cost_per_image > 0) {
                return `${usdMedia(model.cost_per_image)} / image`;
            }
            return null;
        }
        case 'audio_speech': {
            // OpenAI tts-1 uses chars; per-1M scaling keeps the
            // displayed number close to its $/1M-tokens cousin so
            // operators can compare across modes at a glance.
            if (
                typeof model.cost_per_audio_char === 'number'
                && model.cost_per_audio_char > 0
            ) {
                const per1M = model.cost_per_audio_char * 1_000_000;
                return `${usdToken(per1M)} / 1M chars`;
            }
            return null;
        }
        case 'audio_transcription': {
            if (
                typeof model.cost_per_audio_minute === 'number'
                && model.cost_per_audio_minute > 0
            ) {
                return `${usdMedia(model.cost_per_audio_minute)} / min`;
            }
            return null;
        }
        case 'video_generation': {
            if (
                typeof model.cost_per_video_second === 'number'
                && model.cost_per_video_second > 0
            ) {
                return `${usdMedia(model.cost_per_video_second)} / sec`;
            }
            return null;
        }
        // Fall-through: every other mode (chat, completion,
        // responses, embedding, rerank, moderation, routing,
        // missing/null) is token-priced. Input first because
        // that's what operators tune most when picking models.
        default: {
            const hasIn =
                typeof model.cost_per_input_token === 'number'
                && model.cost_per_input_token > 0;
            const hasOut =
                typeof model.cost_per_output_token === 'number'
                && model.cost_per_output_token > 0;
            if (!hasIn && !hasOut) return null;
            const parts: string[] = [];
            if (hasIn) {
                parts.push(`${usdToken(model.cost_per_input_token!)} / 1M in`);
            }
            if (hasOut) {
                parts.push(`${usdToken(model.cost_per_output_token!)} out`);
            }
            return parts.join(' · ');
        }
    }
}
