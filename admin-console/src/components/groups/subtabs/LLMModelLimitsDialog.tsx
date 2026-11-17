/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Per-model LLM limits dialog.
 *
 * Thin wrapper around the shared `LLMLimitsForm` component. The form is
 * mounted in `mode="model"` so each input shows `Global: X / Effective:
 * min(global, per-model)` derived from the group-level limits we load
 * alongside the per-model row.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Loader, X } from 'lucide-react';
import { LLMGlobalLimits } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import { useToast } from '../../common/Toast';
import LLMLimitsForm from './LLMLimitsForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

interface LLModelInfo {
    id: string;
    name?: string;
    model_name?: string;
}

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSave?: () => void;
    groupId: string;
    groupName: string;
    model: LLModelInfo;
}

const LLMModelLimitsDialog: React.FC<Props> = ({
    isOpen,
    onClose,
    onSave,
    groupId,
    groupName,
    model,
}) => {
    const { fetchLLMModelLimits, updateLLMModelLimits, clearLLMModelLimits } = useGateway();
    const toast = useToast();

    const [limits, setLimits] = useState<LLMGlobalLimits>({});
    const [globalLimits, setGlobalLimits] = useState<LLMGlobalLimits>({});
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);

    const loadLimits = useCallback(async () => {
        setLoading(true);
        try {
            const response = await fetchLLMModelLimits(groupId, model.id);
            setLimits((response.limits || {}) as LLMGlobalLimits);
            setGlobalLimits((response.effective_global_limits || {}) as LLMGlobalLimits);
        } catch (err) {
            toast.error('Failed to load model limits', errorMessage(err));
        } finally {
            setLoading(false);
        }
    }, [groupId, model.id, fetchLLMModelLimits, toast]);

    useEffect(() => {
        if (isOpen) runBackground(loadLimits(), 'limits');
    }, [isOpen, loadLimits]);

    const handleSave = async () => {
        setSaving(true);
        try {
            await updateLLMModelLimits(groupId, model.id, { ...limits });
            toast.success('Saved', 'Per-model LLM limits saved');
            onSave?.();
            onClose();
        } catch (err) {
            toast.error('Failed to save model limits', errorMessage(err));
        } finally {
            setSaving(false);
        }
    };

    const handleClear = async () => {
        setSaving(true);
        try {
            await clearLLMModelLimits(groupId, model.id);
            toast.success('Cleared', 'Per-model LLM limits removed');
            onSave?.();
            onClose();
        } catch (err) {
            toast.error('Failed to clear model limits', errorMessage(err));
        } finally {
            setSaving(false);
        }
    };

    if (!isOpen) return null;

    const displayName = model.name || model.model_name || model.id;

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-6xl w-full max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <div>
                        <h2 className="text-lg font-semibold text-default">
                            Per-Model Limits — {displayName}
                        </h2>
                        <p className="text-sm text-muted">{groupName}</p>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-6 space-y-4">
                    <InfoBanner className="mb-0">
                        <p>
                            These limits apply specifically to{' '}
                            <strong>{displayName}</strong> in <strong>{groupName}</strong>.
                            The effective limit at request time is{' '}
                            <code>min(group, per-model)</code> at every scope. Throughput,
                            concurrency, budget, tokens, and request sections all enforce
                            concurrently.
                        </p>
                    </InfoBanner>

                    {loading ? (
                        <div className="flex items-center justify-center h-48">
                            <Loader className="w-8 h-8 text-primary-600 animate-spin" />
                        </div>
                    ) : (
                        <LLMLimitsForm
                            limits={limits}
                            globalLimits={globalLimits}
                            onChange={setLimits}
                            mode="model"
                            disabled={saving}
                        />
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between p-6 border-t border-default bg-surface-alt">
                    <button
                        onClick={asyncHandler(handleClear, 'click')}
                        disabled={saving || loading}
                        className="text-sm text-muted hover:text-red-600 transition-colors disabled:opacity-50"
                    >
                        Clear all per-model limits
                    </button>
                    <div className="flex items-center space-x-3">
                        <button
                            onClick={onClose}
                            disabled={saving}
                            className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleSave, 'click')}
                            disabled={saving || loading}
                            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50"
                        >
                            {saving ? (
                                <span className="flex items-center space-x-2">
                                    <Loader className="w-4 h-4 animate-spin" />
                                    <span>Saving...</span>
                                </span>
                            ) : (
                                'Save'
                            )}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default LLMModelLimitsDialog;
