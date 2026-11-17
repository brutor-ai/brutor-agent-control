/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational media-limits form. Renders the field set for a
 * single media modality (audio_speech, audio_transcription,
 * image_generation, video_generation, embedding) over a plain limit
 * dict and reports edits back via `onChange`. It owns no state and does
 * no fetching/saving — the parent (e.g. `GroupMediaLimitsTab`) holds the
 * limits dict and the mode switcher, so the same form can be reused by
 * any caller that needs to edit one modality's limits.
 */

/* eslint-disable react-refresh/only-export-components --
 * This module is the single source of truth for the media modes: it
 * intentionally co-locates the mode union/list/metadata with the form
 * component so callers (e.g. GroupMediaLimitsTab) reuse them instead of
 * duplicating the field defs (DRY). react-refresh is a dev-only HMR
 * nicety and does not affect production output.
 */
import React from 'react';
import { Mic, Speaker, Image as ImageIcon, Film, Hash } from 'lucide-react';
import UnavailableSettings from '../../common/UnavailableSettings';
import { withoutUnavailable, type UnavailableSetting } from '../../../lib/unavailableSettings';

export type MediaMode =
    | 'audio_speech'
    | 'audio_transcription'
    | 'image_generation'
    | 'video_generation'
    | 'embedding';

export const MEDIA_MODES: MediaMode[] = [
    'audio_speech',
    'audio_transcription',
    'image_generation',
    'video_generation',
    'embedding',
];

interface FieldDef {
    name: string;
    label: string;
    type: 'number' | 'checkbox';
    help?: string;
    min?: number;
    max?: number;
    /** False for a limit the gateway does not act on in this release: not offered, a stored one shown read-only. */
    available?: boolean;
}

interface ModeDef {
    id: MediaMode;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    fields: FieldDef[];
}

// Field sets per mode mirror the Pydantic models in
// `brutor-gateway-control/app/api/v1/models/admin/policy.py`
// (PolicyAudioSpeechUsageLimits etc). Keep the field names
// identical to the Python `model_dump` output so a YAML bundle
// produced by the policy-engine round-trips through this UI
// without a name-mapping layer.
const MODES: ModeDef[] = [
    {
        id: 'audio_speech',
        label: 'TTS (audio_speech)',
        icon: Speaker,
        fields: [
            { name: 'daily_budget_usd', label: 'Daily budget (USD)', type: 'number', min: 0 },
            { name: 'monthly_budget_usd', label: 'Monthly budget (USD)', type: 'number', min: 0, available: false },
            { name: 'daily_requests', label: 'Daily requests', type: 'number', min: 0 },
            { name: 'monthly_requests', label: 'Monthly requests', type: 'number', min: 0, available: false },
            { name: 'daily_characters', label: 'Daily input characters synthesized', type: 'number', min: 0,
              help: 'OpenAI TTS bills per character — this is the cost-shaping axis.' },
            { name: 'monthly_characters', label: 'Monthly input characters', type: 'number', min: 0, available: false },
        ],
    },
    {
        id: 'audio_transcription',
        label: 'STT (audio_transcription)',
        icon: Mic,
        fields: [
            { name: 'daily_budget_usd', label: 'Daily budget (USD)', type: 'number', min: 0 },
            { name: 'monthly_budget_usd', label: 'Monthly budget (USD)', type: 'number', min: 0, available: false },
            { name: 'daily_requests', label: 'Daily requests', type: 'number', min: 0 },
            { name: 'monthly_requests', label: 'Monthly requests', type: 'number', min: 0, available: false },
            { name: 'daily_audio_seconds', label: 'Daily audio seconds transcribed', type: 'number', min: 0 },
            { name: 'monthly_audio_seconds', label: 'Monthly audio seconds', type: 'number', min: 0, available: false },
        ],
    },
    {
        id: 'image_generation',
        label: 'Image generation',
        icon: ImageIcon,
        fields: [
            { name: 'daily_budget_usd', label: 'Daily budget (USD)', type: 'number', min: 0 },
            { name: 'monthly_budget_usd', label: 'Monthly budget (USD)', type: 'number', min: 0, available: false },
            { name: 'daily_images', label: 'Daily images', type: 'number', min: 0 },
            { name: 'monthly_images', label: 'Monthly images', type: 'number', min: 0, available: false },
        ],
    },
    {
        id: 'video_generation',
        label: 'Video generation',
        icon: Film,
        fields: [
            { name: 'daily_budget_usd', label: 'Daily budget (USD)', type: 'number', min: 0 },
            { name: 'monthly_budget_usd', label: 'Monthly budget (USD)', type: 'number', min: 0, available: false },
            { name: 'daily_video_seconds', label: 'Daily video seconds', type: 'number', min: 0 },
            { name: 'monthly_video_seconds', label: 'Monthly video seconds', type: 'number', min: 0, available: false },
            { name: 'daily_requests', label: 'Daily requests', type: 'number', min: 0 },
        ],
    },
    {
        id: 'embedding',
        label: 'Embeddings',
        icon: Hash,
        fields: [
            { name: 'daily_budget_usd', label: 'Daily budget (USD)', type: 'number', min: 0, available: false },
            { name: 'monthly_budget_usd', label: 'Monthly budget (USD)', type: 'number', min: 0, available: false },
            { name: 'daily_input_tokens', label: 'Daily input tokens embedded', type: 'number', min: 0, available: false },
            { name: 'monthly_input_tokens', label: 'Monthly input tokens', type: 'number', min: 0, available: false },
            { name: 'daily_requests', label: 'Daily requests', type: 'number', min: 0, available: false },
        ],
    },
];

// Mode metadata (label + icon) exposed for callers rendering a mode
// switcher — keeps the single source of truth for mode presentation
// in this file alongside the field defs.
export const MEDIA_MODE_META: { id: MediaMode; label: string; icon: React.ComponentType<{ className?: string }> }[] =
    MODES.map(({ id, label, icon }) => ({ id, label, icon }));

interface MediaLimitsFormProps {
    mode: MediaMode;                 // which mode's fields to render
    value: Record<string, any>;
    onChange: (next: Record<string, any>) => void;
    disabled?: boolean;
}

/**
 * The media limits the gateway does not act on in this release, and the
 * Control Plane refuses: every monthly limit, and every limit of embeddings
 * (the group's LLM limits bound those).
 */
export const MEDIA_LIMITS_NOT_AVAILABLE: readonly UnavailableSetting[] = MODES.flatMap((m) =>
    m.fields.filter((f) => f.available === false).map((f) => ({ path: [m.id, f.name], label: `${m.label}: ${f.label}` })),
);

const MediaLimitsForm: React.FC<MediaLimitsFormProps> = ({ mode, value, onChange, disabled }) => {
    const currentMode = MODES.find(m => m.id === mode)!;
    const modeLimits = value[mode] || {};
    const offered = currentMode.fields.filter((f) => f.available !== false);

    const updateField = (field: string, fieldValue: any) => {
        const next = {
            ...value,
            [mode]: {
                ...(value[mode] || {}),
                [field]: fieldValue,
            },
        };
        onChange(withoutUnavailable(next, MEDIA_LIMITS_NOT_AVAILABLE));
    };

    return (
        <div className="space-y-3 max-w-3xl">
        {offered.length === 0 && (
            <p className="text-xs text-muted" data-testid={`media-limits-${mode}-unavailable`}>
                This release has no media limit of its own for {currentMode.label}: the group&apos;s LLM limits bound it.
            </p>
        )}
        <UnavailableSettings
            doc={value}
            settings={MEDIA_LIMITS_NOT_AVAILABLE.filter((setting) => setting.path[0] === mode)}
        />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {offered.map(f => (
                <div key={f.name} className="space-y-1">
                    <label className="text-xs font-medium text-secondary">
                        {f.label}
                    </label>
                    {f.type === 'number' ? (
                        <input
                            type="number"
                            min={f.min}
                            max={f.max}
                            disabled={disabled}
                            value={modeLimits[f.name] ?? ''}
                            onChange={(e) => {
                                const v = e.target.value;
                                updateField(
                                    f.name,
                                    v === '' ? undefined : Number(v)
                                );
                            }}
                            className="w-full px-2 py-1.5 text-sm bg-surface border border-strong rounded focus:ring-2 focus:ring-primary"
                            placeholder="(unset)"
                        />
                    ) : (
                        <input
                            type="checkbox"
                            disabled={disabled}
                            checked={!!modeLimits[f.name]}
                            onChange={(e) => updateField(f.name, e.target.checked)}
                            className="w-4 h-4 rounded"
                        />
                    )}
                    {f.help && (
                        <p className="text-[11px] text-muted">{f.help}</p>
                    )}
                </div>
            ))}
        </div>
        </div>
    );
};

export default MediaLimitsForm;
