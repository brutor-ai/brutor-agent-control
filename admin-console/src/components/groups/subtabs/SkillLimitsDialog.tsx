/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState, useEffect, useCallback } from 'react';
import { ArrowDown, Info } from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import type { AgentSkillInfo, PerSkillCapacityInfo } from '../../../lib/groupTypes';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { errorMessage } from '../../../lib/narrow';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSave?: () => void;
    groupId: string;
    groupName: string;
    skill: AgentSkillInfo;
}

interface ExecutionLimits {
    daily_limit?: number;
    monthly_limit?: number;
    daily_warning_percent?: number;
    monthly_warning_percent?: number;
}

interface SkillLimitsData {
    execution?: ExecutionLimits;
}

/** Get a nested value from a limits object by section.field path */
const getNestedValue = (limits: Record<string, any> | null | undefined, section: string, field: string): number | undefined => {
    if (!limits) return undefined;
    const sec = limits[section] as Record<string, any> | undefined;
    return sec?.[field] ?? undefined;
};

function SkillLimitsDialog({ isOpen, onClose, onSave, groupId, groupName, skill }: Props) {
    const { fetchSkillItemLimits, updateSkillItemLimits, clearSkillItemLimits } = useGateway();
    const toast = useToast();

    const [limits, setLimits] = useState<SkillLimitsData>({});
    const [globalLimits, setGlobalLimits] = useState<SkillLimitsData>({});
    const [capacity, setCapacity] = useState<PerSkillCapacityInfo | null>(null);
    const [capacityPopoverField, setCapacityPopoverField] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);

    const loadLimits = useCallback(async () => {
        setLoading(true);
        try {
            const response = await fetchSkillItemLimits(groupId, skill.id);
            setLimits(response.limits || {});
            setGlobalLimits(response.effective_global_limits || {});
            setCapacity(response.capacity || null);
        } catch (err) {
            toast.error('Failed to load skill limits', errorMessage(err, '') || undefined);
        } finally {
            setLoading(false);
        }
    }, [groupId, skill.id, fetchSkillItemLimits]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadLimits(), 'limits');
            setCapacityPopoverField(null);
        }
    }, [isOpen, loadLimits]);

    const handleSave = async () => {
        setSaving(true);
        try {
            await updateSkillItemLimits(groupId, skill.id, { ...limits });
            toast.success('Saved', 'Skill limits saved successfully');
            onSave?.();
            onClose();
        } catch (err) {
            toast.error('Failed to save skill limits', errorMessage(err, '') || undefined);
        } finally {
            setSaving(false);
        }
    };

    const handleClear = async () => {
        setSaving(true);
        try {
            await clearSkillItemLimits(groupId, skill.id);
            toast.success('Cleared', 'Skill limits cleared');
            setLimits({});
            onSave?.();
            onClose();
        } catch (err) {
            toast.error('Failed to clear skill limits', errorMessage(err, '') || undefined);
        } finally {
            setSaving(false);
        }
    };

    const updateExecution = (field: keyof ExecutionLimits, value: string) => {
        setLimits(prev => ({
            ...prev,
            execution: {
                ...prev.execution,
                [field]: value === '' ? undefined : Number(value),
            },
        }));
    };

    const effectiveValue = (global: number | undefined, perItem: number | undefined): string => {
        if (global !== undefined && perItem !== undefined) return String(Math.min(global, perItem));
        if (perItem !== undefined) return String(perItem);
        if (global !== undefined) return String(global);
        return 'No limit';
    };

    /** Render a limit input with capacity indicators and popover */
    const renderLimitInput = (
        label: string,
        section: string,
        field: string,
        currentValue: number | undefined,
        globalValue: number | undefined,
        onChange: (value: string) => void,
        opts: { isPercent?: boolean } = {}
    ) => {
        const available = capacity ? getNestedValue(capacity.available, section, field) : undefined;
        const otherSkillsTotal = capacity ? getNestedValue(capacity.other_skills_total, section, field) : undefined;
        const groupGlobalLimit = capacity ? getNestedValue(capacity.group_global_limit, section, field) : undefined;
        const hasCapacity = available !== undefined;

        const exceedsAvailable = hasCapacity && currentValue !== undefined && currentValue > available;
        const isOverLimit = exceedsAvailable;

        const suffix = opts.isPercent ? '%' : '';
        const fieldKey = `${section}.${field}`;
        const showPopover = capacityPopoverField === fieldKey;

        return (
            <div>
                <label className="block text-xs text-muted mb-1">
                    {label}
                    {hasCapacity && (
                        <span
                            className={`ml-1.5 inline-flex items-center text-xs ${
                                isOverLimit ? 'text-red-600 font-semibold' : 'text-amber-600'
                            }`}
                        >
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            available {available}{suffix} ({groupGlobalLimit}{suffix})
                        </span>
                    )}
                    {hasCapacity && (
                        <button
                            onClick={() => setCapacityPopoverField(showPopover ? null : fieldKey)}
                            className="ml-1 text-muted hover:text-primary align-middle"
                            title="Show capacity breakdown"
                            type="button"
                        >
                            <Info className="w-3 h-3" />
                        </button>
                    )}
                </label>
                <input
                    type="number"
                    className={`w-full px-2 py-1.5 text-sm border rounded bg-surface text-default ${
                        isOverLimit
                            ? 'border-default bg-surface-alt/40/20'
                            : hasCapacity
                                ? 'border-default bg-surface-alt/30/10'
                                : 'border-strong'
                    }`}
                    value={currentValue ?? ''}
                    onChange={e => onChange(e.target.value)}
                    placeholder={hasCapacity ? `Available: ${available}` : `Global: ${globalValue ?? 'No limit'}`}
                />
                {isOverLimit && (
                    <p className="text-xs text-red-600 mt-0.5 font-medium">
                        Exceeds available capacity ({available}{suffix}) — other skills already allocated {otherSkillsTotal ?? 0}{suffix} of group's {groupGlobalLimit}{suffix}
                    </p>
                )}
                <span className="text-xs text-muted mt-0.5">Effective: {effectiveValue(globalValue, currentValue)}{suffix}</span>
                {showPopover && hasCapacity && capacity && (
                    <div className="mt-1.5 p-3 bg-surface-alt border border-default rounded-lg text-xs space-y-1.5 shadow-sm">
                        <div className="flex justify-between">
                            <span className="text-secondary">Group Global Limit ({capacity.group_name ?? groupName}):</span>
                            <span className="font-medium text-default">{groupGlobalLimit}{suffix}</span>
                        </div>
                        {capacity.other_skills.length > 0 && (
                            <>
                                <div className="text-muted font-medium pt-1">Other Skills:</div>
                                {capacity.other_skills.map(s => {
                                    const sVal = getNestedValue(s.limits, section, field);
                                    return (
                                        <div key={s.agent_skill_id} className="flex justify-between pl-3">
                                            <span className="text-secondary">{s.skill_name}:</span>
                                            <span className="text-default">{sVal !== undefined ? `${sVal}${suffix}` : '\u2014'}</span>
                                        </div>
                                    );
                                })}
                                <div className="flex justify-between border-t border-default pt-1.5">
                                    <span className="text-secondary">Other skills total:</span>
                                    <span className="font-medium text-default">{otherSkillsTotal ?? 0}{suffix}</span>
                                </div>
                            </>
                        )}
                        <div className="flex justify-between border-t border-default pt-1.5">
                            <span className="font-semibold text-default">Available for this skill:</span>
                            <span className="font-semibold text-green-700">{available}{suffix}</span>
                        </div>
                    </div>
                )}
            </div>
        );
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <h2 className="text-lg font-semibold text-default">
                        Per-Skill Limits &mdash; {skill.name}
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
                    These limits apply specifically to <strong>{skill.name}</strong> in <strong>{groupName}</strong>.
                    The effective limit is min(global, per-skill).
                    {capacity && (
                        <span className="ml-1">
                            Fields marked with <ArrowDown className="w-3 h-3 inline text-amber-600" /> show remaining capacity from the group's global limit after other skills' allocations.
                        </span>
                    )}
                </div>

                {loading ? (
                    <div className="p-6 text-center text-muted">Loading limits...</div>
                ) : (
                    <div className="p-6">
                        {/* Execution Limits Card */}
                        <div className="border border-default rounded-lg p-4">
                            <h3 className="text-sm font-semibold text-default mb-3">Execution Limits</h3>
                            <div className="space-y-3">
                                {renderLimitInput(
                                    'Daily Limit', 'execution', 'daily_limit',
                                    limits.execution?.daily_limit, globalLimits.execution?.daily_limit,
                                    v => updateExecution('daily_limit', v)
                                )}
                                {renderLimitInput(
                                    'Monthly Limit', 'execution', 'monthly_limit',
                                    limits.execution?.monthly_limit, globalLimits.execution?.monthly_limit,
                                    v => updateExecution('monthly_limit', v)
                                )}
                                {renderLimitInput(
                                    'Daily Warning %', 'execution', 'daily_warning_percent',
                                    limits.execution?.daily_warning_percent, globalLimits.execution?.daily_warning_percent,
                                    v => updateExecution('daily_warning_percent', v),
                                    { isPercent: true }
                                )}
                                {renderLimitInput(
                                    'Monthly Warning %', 'execution', 'monthly_warning_percent',
                                    limits.execution?.monthly_warning_percent, globalLimits.execution?.monthly_warning_percent,
                                    v => updateExecution('monthly_warning_percent', v),
                                    { isPercent: true }
                                )}
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
                        Clear Limits
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
                            disabled={saving}
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

export default SkillLimitsDialog;
