/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-modality behavioral governance — the proxy enforces these
 * before dispatching to the upstream provider:
 *   - audio_speech: voice / response_format whitelist, input cap, speed bounds
 *   - audio_transcription: language allow-list, response_format, audio size cap
 *   - image_generation: size / quality / style whitelist, max_n
 *   - video_generation: max duration, resolution whitelist, image-input requirement
 *   - embedding: dim whitelist, max batch, max tokens per item
 *
 * Sibling tab to `GroupMediaLimitsTab`. Both write the same JSONB
 * column shape — keyed by mode — but to different columns
 * (`media_global_governance` vs `media_global_limits`). The per-mode
 * field rendering is delegated to the reusable `MediaGovernanceForm`;
 * this tab owns the mode switcher, fetch/save, and change tracking.
 */

import React, { useState, useEffect, useMemo } from 'react';
import { Save } from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import type { MediaMode } from './MediaLimitsForm';
import MediaGovernanceForm, { MEDIA_GOVERNANCE_MODES } from './MediaGovernanceForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { withoutRefused } from '../../../lib/refusedSettings';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupMediaGovernanceTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchMediaGlobalGovernance, updateMediaGlobalGovernance } = useGateway();
    const [mode, setMode] = useState<MediaMode>('audio_speech');
    const [governance, setGovernance] = useState<Record<string, any>>({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [hasChanges, setHasChanges] = useState(false);

    useEffect(() => {
        runBackground(loadGovernance(), 'governance');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [group.id]);

    const loadGovernance = async () => {
        setLoading(true);
        try {
            const data = await fetchMediaGlobalGovernance(group.id);
            setGovernance(data?.media_global_governance || {});
            setHasChanges(false);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load media governance'));
        } finally {
            setLoading(false);
        }
    };

    const currentMode = useMemo(() => MEDIA_GOVERNANCE_MODES.find(m => m.id === mode)!, [mode]);

    const handleGovernanceChange = (next: Record<string, any>) => {
        setGovernance(next);
        setHasChanges(true);
    };

    const clearMode = () => {
        setGovernance(prev => {
            const next = { ...prev };
            delete next[mode];
            return next;
        });
        setHasChanges(true);
    };

    const handleSave = async () => {
        setSaving(true);
        setError('');
        try {
            await updateMediaGlobalGovernance(group.id, withoutRefused('media_governance', governance));
            setHasChanges(false);
            onUpdate();
        } catch (err) {
            setError(errorMessage(err, 'Failed to update media governance'));
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return <div className="p-6 text-sm text-secondary">Loading…</div>;
    }

    return (
        // Outer padding mirrors GroupLLMGovernanceTab / GroupMCPGovernanceTab
        // (`p-6 space-y-6`) so the Media subtabs sit flush with the
        // sibling tabs in the Usage Policies panel.
        <div className="p-6 space-y-6">
            <div>
                <h3 className="text-base font-semibold text-default mb-1">Media governance</h3>
                <p className="text-xs text-secondary">
                    Behavioral constraints enforced by the proxy before each
                    multimodal request dispatches. Lists accept comma-separated
                    values. Empty / unset = no constraint at this layer.
                </p>
            </div>

            <div className="flex flex-wrap gap-2 border-b border-default pb-3">
                {MEDIA_GOVERNANCE_MODES.map(m => {
                    const Icon = m.icon;
                    const isActive = m.id === mode;
                    const hasValues = Object.keys(governance[m.id] || {}).length > 0;
                    return (
                        <button
                            key={m.id}
                            type="button"
                            onClick={() => setMode(m.id)}
                            className={
                                'flex items-center gap-1.5 px-3 py-1.5 text-xs rounded border transition ' +
                                (isActive
                                    ? 'border-primary text-primary bg-primary/10'
                                    : 'border-default text-secondary hover:bg-surface-alt')
                            }
                        >
                            <Icon className="w-3.5 h-3.5" />
                            <span>{m.label}</span>
                            {hasValues && (
                                <span className="ml-1 w-1.5 h-1.5 rounded-full bg-primary" />
                            )}
                        </button>
                    );
                })}
            </div>

            {error && (
                <div className="text-xs text-red-600 bg-surface-alt dark:bg-surface-raised p-2 rounded">
                    {error}
                </div>
            )}

            <MediaGovernanceForm
                mode={mode}
                value={governance}
                onChange={handleGovernanceChange}
            />

            <div className="flex gap-2 pt-3 border-t border-default">
                <button
                    type="button"
                    onClick={asyncHandler(handleSave, 'click')}
                    disabled={!hasChanges || saving}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs bg-primary text-white rounded hover:bg-primary-700 disabled:opacity-50"
                >
                    <Save className="w-3.5 h-3.5" />
                    {saving ? 'Saving…' : 'Save'}
                </button>
                <button
                    type="button"
                    onClick={asyncHandler(loadGovernance, 'click')}
                    disabled={!hasChanges || saving}
                    className="px-3 py-1.5 text-xs border border-default rounded text-secondary hover:bg-surface-alt disabled:opacity-50"
                >
                    Discard
                </button>
                <button
                    type="button"
                    onClick={clearMode}
                    className="ml-auto px-3 py-1.5 text-xs text-red-600 hover:underline"
                >
                    Clear all {currentMode.label} governance
                </button>
            </div>
        </div>
    );
};

export default GroupMediaGovernanceTab;
