/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-modality usage limits for audio_speech, audio_transcription,
 * image_generation, video_generation, and embedding. Sibling tab to
 * `GroupLLMLimitsTab` / `GroupSkillLimitsTab` — picks one mode at a
 * time and edits the limit dict for that mode. The whole thing is
 * stored as a single JSONB column on `resource_groups.media_global_limits`,
 * so partial saves (e.g. setting only audio_speech) don't wipe the
 * other modes — the proxy/control-plane do a deep merge on PATCH.
 *
 * Field rendering is delegated to the reusable presentational
 * `MediaLimitsForm`; this tab owns the mode switcher, fetch/save, and
 * dirty-tracking.
 */

import React, { useState, useEffect, useMemo } from 'react';
import { Save } from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import MediaLimitsForm, { MediaMode, MEDIA_MODE_META } from './MediaLimitsForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { withoutRefused } from '../../../lib/refusedSettings';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupMediaLimitsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchMediaGlobalLimits, updateMediaGlobalLimits } = useGateway();
    const [mode, setMode] = useState<MediaMode>('audio_speech');
    const [limits, setLimits] = useState<Record<string, any>>({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [hasChanges, setHasChanges] = useState(false);

    useEffect(() => {
        runBackground(loadLimits(), 'limits');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [group.id]);

    const loadLimits = async () => {
        setLoading(true);
        try {
            const data = await fetchMediaGlobalLimits(group.id);
            setLimits(data?.media_global_limits || {});
            setHasChanges(false);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load media limits'));
        } finally {
            setLoading(false);
        }
    };

    const currentMode = useMemo(() => MEDIA_MODE_META.find(m => m.id === mode)!, [mode]);

    const handleFormChange = (next: Record<string, any>) => {
        setLimits(next);
        setHasChanges(true);
    };

    const clearMode = () => {
        setLimits(prev => {
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
            await updateMediaGlobalLimits(group.id, withoutRefused('media_limits', limits));
            setHasChanges(false);
            onUpdate();
        } catch (err) {
            setError(errorMessage(err, 'Failed to update media limits'));
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return <div className="p-6 text-sm text-secondary">Loading…</div>;
    }

    return (
        // Outer padding mirrors GroupLLMLimitsTab / GroupSkillLimitsTab
        // (`p-6 space-y-6`) so the Media subtabs sit flush with the
        // sibling tabs in the Usage Limits panel — content has the
        // same gutters, no awkward left-edge collision.
        <div className="p-6 space-y-6">
            <div>
                <h3 className="text-base font-semibold text-default mb-1">Media usage limits</h3>
                <p className="text-xs text-secondary">
                    Per-modality budgets, request counts, and throughput caps. Pick a
                    modality to edit. Sibling modes you don't touch are preserved.
                </p>
            </div>

            {/* Mode picker */}
            <div className="flex flex-wrap gap-2 border-b border-default pb-3">
                {MEDIA_MODE_META.map(m => {
                    const Icon = m.icon;
                    const isActive = m.id === mode;
                    const hasValues = Object.keys(limits[m.id] || {}).length > 0;
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

            {/* Field list for selected mode */}
            <MediaLimitsForm mode={mode} value={limits} onChange={handleFormChange} />

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
                    onClick={asyncHandler(loadLimits, 'click')}
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
                    Clear all {currentMode.label} limits
                </button>
            </div>
        </div>
    );
};

export default GroupMediaLimitsTab;
