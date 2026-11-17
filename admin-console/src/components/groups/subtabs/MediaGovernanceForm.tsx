/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational media-governance form. Renders the field set for a
 * single media modality (audio_speech, audio_transcription,
 * image_generation, video_generation, embedding) over a plain governance
 * dict and reports edits back via `onChange`. It owns no state and does
 * no fetching/saving — the parent (e.g. `GroupMediaGovernanceTab`) holds
 * the governance dict and the mode switcher, so the same form can be
 * reused by any caller that needs to edit one modality's governance.
 *
 * Sibling to `MediaLimitsForm` (behavioral constraints vs cost/usage
 * limits). The `MediaMode` union is shared from `MediaLimitsForm`.
 */

/* eslint-disable react-refresh/only-export-components --
 * This module co-locates the per-mode governance metadata/field defs with
 * the form component so callers (e.g. GroupMediaGovernanceTab) reuse them
 * instead of duplicating the field defs (DRY). react-refresh is a dev-only
 * HMR nicety and does not affect production output.
 */
import React from 'react';
import { Mic, Speaker, Image as ImageIcon, Film, Hash } from 'lucide-react';
import type { MediaMode } from './MediaLimitsForm';
import UnavailableSettings from '../../common/UnavailableSettings';
import { withoutUnavailable, type UnavailableSetting } from '../../../lib/unavailableSettings';

interface FieldDef {
    name: string;
    label: string;
    type: 'number' | 'checkbox' | 'string_list' | 'int_list';
    help?: string;
    placeholder?: string;
}

interface ModeDef {
    id: MediaMode;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    fields: FieldDef[];
}

export const MEDIA_GOVERNANCE_MODES: ModeDef[] = [
    {
        id: 'audio_speech',
        label: 'TTS',
        icon: Speaker,
        fields: [
            { name: 'voice_whitelist', label: 'Allowed voices', type: 'string_list',
              placeholder: 'alloy, echo, fable',
              help: 'Empty = inherit from model whitelist.' },
            { name: 'response_format_whitelist', label: 'Allowed response formats', type: 'string_list',
              placeholder: 'mp3, wav' },
            { name: 'max_input_characters', label: 'Max input characters per request', type: 'number',
              help: 'OpenAI accepts up to 4096; chunked synthesis is server-side. Cap further if you want.' },
            { name: 'speed_min', label: 'Min speed multiplier', type: 'number',
              help: '0.25 – 4.0 (OpenAI bounds).' },
            { name: 'speed_max', label: 'Max speed multiplier', type: 'number' },
        ],
    },
    {
        id: 'audio_transcription',
        label: 'STT',
        icon: Mic,
        fields: [
            { name: 'language_allowlist', label: 'Allowed languages (BCP-47)', type: 'string_list',
              placeholder: 'en, sv, es' },
            { name: 'response_format_whitelist', label: 'Allowed response formats', type: 'string_list',
              placeholder: 'json, text, srt, verbose_json, vtt' },
            { name: 'max_audio_size_mb', label: 'Max audio size (MB)', type: 'number' },
        ],
    },
    {
        id: 'image_generation',
        label: 'Image generation',
        icon: ImageIcon,
        fields: [
            { name: 'size_whitelist', label: 'Allowed sizes', type: 'string_list',
              placeholder: '1024x1024, 1792x1024' },
            { name: 'quality_whitelist', label: 'Allowed qualities', type: 'string_list',
              placeholder: 'standard, hd' },
            { name: 'style_whitelist', label: 'Allowed styles', type: 'string_list',
              placeholder: 'natural, vivid' },
            { name: 'max_n', label: 'Max images per request', type: 'number' },
            { name: 'max_prompt_characters', label: 'Max prompt characters', type: 'number' },
        ],
    },
    {
        id: 'video_generation',
        label: 'Video generation',
        icon: Film,
        fields: [
            { name: 'max_duration_seconds', label: 'Max duration (seconds)', type: 'number' },
            { name: 'resolution_whitelist', label: 'Allowed resolutions', type: 'string_list',
              placeholder: '540p, 720p, 1080p' },
            { name: 'require_image_input', label: 'Require image-to-video (block text-to-video)', type: 'checkbox' },
            { name: 'max_prompt_characters', label: 'Max prompt characters', type: 'number' },
        ],
    },
    {
        id: 'embedding',
        label: 'Embeddings',
        icon: Hash,
        fields: [
            { name: 'dim_whitelist', label: 'Allowed dimensions', type: 'int_list',
              placeholder: '512, 1024, 1536' },
            { name: 'max_batch_size', label: 'Max batch size', type: 'number' },
        ],
    },
];

const parseStringList = (raw: string): string[] | undefined => {
    const trimmed = raw.trim();
    if (!trimmed) return undefined;
    return trimmed
        .split(',')
        .map(s => s.trim())
        .filter(s => s.length > 0);
};

const parseIntList = (raw: string): number[] | undefined => {
    const arr = parseStringList(raw);
    if (!arr) return undefined;
    const ints = arr.map(s => Number(s)).filter(n => Number.isFinite(n));
    return ints.length ? ints : undefined;
};

const formatList = (v: unknown): string => {
    if (!Array.isArray(v)) return '';
    return v.join(', ');
};

/**
 * Media governance the gateway does not act on in this release: a cap on
 * the tokens of one embedding input, and the switch for guardrailing a
 * transcription's prompt (the gateway always guardrails it).
 */
const MEDIA_GOVERNANCE_NOT_AVAILABLE: readonly UnavailableSetting[] = [
    { path: ['embedding', 'max_input_tokens_per_item'], label: 'Max input tokens per item' },
    { path: ['audio_transcription', 'require_prompt_guardrail'], label: 'Run prompt parameter through guardrails' },
];

interface MediaGovernanceFormProps {
    mode: MediaMode;
    value: Record<string, any>;
    onChange: (next: Record<string, any>) => void;
    disabled?: boolean;
}

const MediaGovernanceForm: React.FC<MediaGovernanceFormProps> = ({ mode, value, onChange, disabled }) => {
    const currentMode = MEDIA_GOVERNANCE_MODES.find(m => m.id === mode)!;
    const modeGov = value[mode] || {};

    const updateField = (field: string, fieldValue: any) => {
        const next = { ...value };
        const modeData = { ...(next[mode] || {}) };
        if (fieldValue === undefined || fieldValue === '' || (Array.isArray(fieldValue) && fieldValue.length === 0)) {
            delete modeData[field];
        } else {
            modeData[field] = fieldValue;
        }
        if (Object.keys(modeData).length === 0) {
            delete next[mode];
        } else {
            next[mode] = modeData;
        }
        onChange(withoutUnavailable(next, MEDIA_GOVERNANCE_NOT_AVAILABLE));
    };

    return (
        <div className="space-y-3 max-w-3xl">
        <UnavailableSettings
            doc={value}
            settings={MEDIA_GOVERNANCE_NOT_AVAILABLE.filter((setting) => setting.path[0] === mode)}
        />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {currentMode.fields.map(f => (
                <div key={f.name} className="space-y-1">
                    <label className="text-xs font-medium text-secondary">
                        {f.label}
                    </label>
                    {f.type === 'number' ? (
                        <input
                            type="number"
                            value={modeGov[f.name] ?? ''}
                            onChange={(e) => {
                                const v = e.target.value;
                                updateField(
                                    f.name,
                                    v === '' ? undefined : Number(v)
                                );
                            }}
                            disabled={disabled}
                            className="w-full px-2 py-1.5 text-sm bg-surface border border-strong rounded focus:ring-2 focus:ring-primary"
                            placeholder="(unset)"
                        />
                    ) : f.type === 'checkbox' ? (
                        <input
                            type="checkbox"
                            checked={!!modeGov[f.name]}
                            onChange={(e) => updateField(f.name, e.target.checked)}
                            disabled={disabled}
                            className="w-4 h-4 rounded"
                        />
                    ) : f.type === 'string_list' ? (
                        <input
                            type="text"
                            value={formatList(modeGov[f.name])}
                            onChange={(e) => updateField(f.name, parseStringList(e.target.value))}
                            disabled={disabled}
                            placeholder={f.placeholder ?? 'comma, separated, values'}
                            className="w-full px-2 py-1.5 text-sm bg-surface border border-strong rounded focus:ring-2 focus:ring-primary"
                        />
                    ) : f.type === 'int_list' ? (
                        <input
                            type="text"
                            value={formatList(modeGov[f.name])}
                            onChange={(e) => updateField(f.name, parseIntList(e.target.value))}
                            disabled={disabled}
                            placeholder={f.placeholder ?? 'comma-separated numbers'}
                            className="w-full px-2 py-1.5 text-sm bg-surface border border-strong rounded focus:ring-2 focus:ring-primary"
                        />
                    ) : null}
                    {f.help && (
                        <p className="text-[11px] text-muted">{f.help}</p>
                    )}
                </div>
            ))}
        </div>
        </div>
    );
};

export default MediaGovernanceForm;
