/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Group-wide LLM usage limits.
 *
 * Thin wrapper around the shared `LLMLimitsForm` component — handles
 * load / save / dirty-state. Page layout mirrors the MCP Limits tab
 * (title + save buttons in the header, InfoBanner, section cards) so
 * the two tabs feel like siblings.
 */

import React, { useEffect, useState } from 'react';
import { AlertCircle, ArrowDown, Info, Loader, Save, Shield, X } from 'lucide-react';
import { LimitCapacityInfo, LLMGlobalLimits, ResourceGroup } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import LLMLimitsForm from './LLMLimitsForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { withoutRefused } from '../../../lib/refusedSettings';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupLLMLimitsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchLLMGlobalLimits, updateLLMGlobalLimits } = useGateway();

    const [limits, setLimits] = useState<LLMGlobalLimits>({});
    const [inheritedLimits, setInheritedLimits] = useState<LLMGlobalLimits | undefined>(
        undefined,
    );
    const [effectiveLimits, setEffectiveLimits] = useState<LLMGlobalLimits | undefined>(
        undefined,
    );
    const [capacity, setCapacity] = useState<LimitCapacityInfo | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);
    const [hasChanges, setHasChanges] = useState(false);

    const hasParent = !!group.parent_group_id;

    const loadLimits = async () => {
        try {
            setLoading(true);
            setError('');
            const data = await fetchLLMGlobalLimits(group.id);
            setLimits((data.llm_global_limits || {}));
            setInheritedLimits(
                (data.inherited_llm_limits || undefined),
            );
            setEffectiveLimits(
                (data.effective_llm_limits || undefined),
            );
            setCapacity(data.capacity || null);
            setHasChanges(false);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load LLM limits'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        runBackground(loadLimits(), 'limits');
        // Reload when switching groups.
    }, [group.id]); // eslint-disable-line react-hooks/exhaustive-deps

    const handleChange = (next: LLMGlobalLimits) => {
        setLimits(next);
        setHasChanges(true);
    };

    const handleSave = async () => {
        try {
            setSaving(true);
            setError('');
            const saved = await updateLLMGlobalLimits(group.id, withoutRefused('llm_limits', limits));
            // The save response carries the capacity left AFTER this write,
            // so the remaining-budget figures on screen stay true without a
            // reload. A stale figure here would invite the very over-commit
            // the API refuses.
            if (saved) {
                setInheritedLimits(
                    (saved.inherited_llm_limits || undefined),
                );
                setEffectiveLimits(
                    (saved.effective_llm_limits || undefined),
                );
                setCapacity(saved.capacity || null);
            }
            setHasChanges(false);
            onUpdate();
        } catch (err) {
            setError(errorMessage(err, 'Failed to update LLM limits'));
        } finally {
            setSaving(false);
        }
    };

    const handleCancel = () => {
        runBackground(loadLimits(), 'limits');
    };

    if (loading) {
        return (
            <div className="p-6 flex items-center justify-center py-12">
                <Loader className="w-6 h-6 text-primary-600 animate-spin" />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">LLM Usage Limits</h2>
                    <p className="text-sm text-secondary mt-1">
                        Set limits for LLM usage. These limits apply only to this group.
                    </p>
                </div>
                {hasChanges && (
                    <div className="flex gap-2">
                        <button
                            onClick={handleCancel}
                            disabled={saving}
                            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt"
                        >
                            <X className="w-4 h-4" />
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleSave, 'click')}
                            disabled={saving}
                            className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-2"
                        >
                            {saving ? (
                                <>
                                    <Loader className="w-4 h-4 animate-spin" />
                                    Saving...
                                </>
                            ) : (
                                <>
                                    <Save className="w-4 h-4" />
                                    Save Changes
                                </>
                            )}
                        </button>
                    </div>
                )}
            </div>

            {/* Error */}
            {error && (
                <div className="p-4 bg-surface-alt border border-default rounded-lg flex items-start gap-3">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-red-800">{error}</p>
                </div>
            )}

            {/* Info */}
            <InfoBanner
                icon={<Shield className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />}
            >
                <p>
                    Limits use <strong>governance inheritance</strong> — the effective limit
                    is always the most restrictive value across this group and all its
                    ancestors. The gateway enforces every section below at the request
                    path; throughput buckets are charged post-response from actual billed
                    tokens and cost.
                </p>
                {hasParent && inheritedLimits && (
                    <p className="mt-1" data-testid="llm-limits-inherited-note">
                        Fields marked <ArrowDown className="w-3 h-3 inline" /> carry a limit
                        inherited from a parent group. You can set an equal or more
                        restrictive value; a looser one is refused on save.
                    </p>
                )}
                {hasParent && capacity?.parent_name && (
                    <p className="mt-1" data-testid="llm-limits-capacity-note">
                        The budget comes from <strong>{capacity.parent_name}</strong>
                        {capacity.parent_is_immediate === false ? ' further up the hierarchy' : ''}
                        . Open the <Info className="w-3 h-3 inline" /> on a field to see what
                        the other groups drawing on it have already claimed.
                    </p>
                )}
            </InfoBanner>

            {/* Form */}
            <LLMLimitsForm
                limits={limits}
                inheritedLimits={inheritedLimits}
                effectiveLimits={effectiveLimits}
                capacity={capacity}
                hasParent={hasParent}
                onChange={handleChange}
                mode="group"
                disabled={saving}
            />
        </div>
    );
};

export default GroupLLMLimitsTab;
