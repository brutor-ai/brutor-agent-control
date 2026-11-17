/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState, useEffect, useCallback } from 'react';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import type { AgentSkillInfo, SkillGovernance, SkillGlobalGovernance } from '../../../lib/groupTypes';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { errorMessage } from '../../../lib/narrow';
import { APPROVAL_TIMEOUT_MAX_SECONDS, APPROVAL_TIMEOUT_MIN_SECONDS, approvalTimeoutProblem } from '../../../lib/approvalTimeout';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSave?: () => void;
    groupId: string;
    groupName: string;
    skill: AgentSkillInfo;
}

function SkillGovernanceDialog({ isOpen, onClose, onSave, groupId, groupName, skill }: Props) {
    const { getSkillGovernance, updateSkillGovernance, clearSkillGovernance } = useGateway();
    const toast = useToast();

    const [governance, setGovernance] = useState<SkillGovernance>({});
    const [globalGovernance, setGlobalGovernance] = useState<SkillGlobalGovernance | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);

    const loadGovernance = useCallback(async () => {
        setLoading(true);
        try {
            const response = await getSkillGovernance(groupId, skill.id);
            setGovernance(response.governance || {});
            setGlobalGovernance(response.effective_global_governance || null);
        } catch (err) {
            // The Control Plane's reason, as it words it.
            toast.error('Failed to load skill governance', errorMessage(err, '') || undefined);
        } finally {
            setLoading(false);
        }
    }, [groupId, skill.id, getSkillGovernance]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadGovernance(), 'governance');
        }
    }, [isOpen, loadGovernance]);

    const handleSave = async () => {
        setSaving(true);
        try {
            await updateSkillGovernance(groupId, skill.id, governance);
            toast.success('Saved', 'Skill governance saved successfully');
            onSave?.();
            onClose();
        } catch (err) {
            // The Control Plane's reason, as it words it.
            toast.error('Failed to save skill governance', errorMessage(err, '') || undefined);
        } finally {
            setSaving(false);
        }
    };

    const handleClear = async () => {
        setSaving(true);
        try {
            await clearSkillGovernance(groupId, skill.id);
            toast.success('Cleared', 'Skill governance cleared');
            setGovernance({});
            onSave?.();
            onClose();
        } catch (err) {
            // The Control Plane's reason, as it words it.
            toast.error('Failed to clear skill governance', errorMessage(err, '') || undefined);
        } finally {
            setSaving(false);
        }
    };

    if (!isOpen) return null;

    const timeoutProblem = approvalTimeoutProblem(governance.approval_timeout_seconds);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <h2 className="text-lg font-semibold text-default">
                        Per-Skill Governance &mdash; {skill.name}
                    </h2>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary"
                    >
                        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                    </button>
                </div>

                {/* Info Banner */}
                <div className="mx-6 mt-4 p-3 bg-primary-pale/20 border border-primary rounded-lg text-sm text-primary">
                    These governance settings apply specifically to <strong>{skill.name}</strong> in <strong>{groupName}</strong>.
                    The effective governance is the most restrictive of global and per-skill.
                </div>

                {loading ? (
                    <div className="p-6 text-center text-muted">Loading governance...</div>
                ) : (
                    <div className="p-6">
                        {/* Governance Settings Card */}
                        <div className="border border-default rounded-lg p-4">
                            <h3 className="text-sm font-semibold text-default mb-3">Governance Settings</h3>
                            <div className="space-y-3">
                                <div>
                                    <label className="block text-xs text-muted mb-1">Execution Mode</label>
                                    <select
                                        className="w-full px-2 py-1.5 text-sm border border-strong rounded bg-surface text-default"
                                        value={governance.execution_mode ?? ''}
                                        onChange={e => setGovernance(prev => ({
                                            ...prev,
                                            execution_mode: e.target.value === '' ? undefined : e.target.value as SkillGovernance['execution_mode'],
                                        }))}
                                    >
                                        <option value="">
                                            {globalGovernance?.execution_mode
                                                ? `Global: ${globalGovernance.execution_mode}`
                                                : 'Not set'}
                                        </option>
                                        <option value="unrestricted">Unrestricted</option>
                                        <option value="approval_required">Approval Required</option>
                                        <option value="disabled">Disabled</option>
                                    </select>
                                </div>
                                <div>
                                    <label className="block text-xs text-muted mb-1">Approval Timeout (seconds)</label>
                                    <input
                                        type="number"
                                        min={APPROVAL_TIMEOUT_MIN_SECONDS}
                                        max={APPROVAL_TIMEOUT_MAX_SECONDS}
                                        className="w-full px-2 py-1.5 text-sm border border-strong rounded bg-surface text-default"
                                        value={governance.approval_timeout_seconds ?? ''}
                                        onChange={e => setGovernance(prev => ({
                                            ...prev,
                                            approval_timeout_seconds: e.target.value === '' ? undefined : Number(e.target.value),
                                        }))}
                                        placeholder={`Global: ${globalGovernance?.approval_timeout_seconds ?? 'Not set'}`}
                                    />
                                    {timeoutProblem && (
                                        <p role="alert" className="text-xs text-red-600 mt-1">{timeoutProblem}</p>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                )}

                {/* Footer */}
                <div className="flex items-center justify-between p-6 border-t border-default">
                    <button
                        onClick={asyncHandler(handleClear, 'click')}
                        disabled={saving}
                        className="px-4 py-2 text-sm font-medium text-red-600 hover:text-red-700 border border-default rounded-lg hover:bg-surface-alt disabled:opacity-50"
                    >
                        Clear Governance
                    </button>
                    <div className="flex gap-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleSave, 'click')}
                            disabled={saving || timeoutProblem !== null}
                            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 rounded-lg disabled:opacity-50"
                        >
                            {saving ? 'Saving...' : 'Save'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default SkillGovernanceDialog;
