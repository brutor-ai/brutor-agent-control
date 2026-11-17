/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Save,
    X,
    Loader,
    AlertCircle,
    ArrowDown,
    Shield,
} from 'lucide-react';
import { ResourceGroup, MCPGlobalLimits, LimitCapacityInfo } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import MCPLimitsForm from './MCPLimitsForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { withoutRefused } from '../../../lib/refusedSettings';
import { mcpExecutionLimitsProblem } from '../../../lib/mcpExecutionLimits';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupMCPGlobalLimitsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchMCPGlobalLimits, updateMCPGlobalLimits } = useGateway();

    const [limits, setLimits] = useState<MCPGlobalLimits>({});
    const [inheritedLimits, setInheritedLimits] = useState<MCPGlobalLimits | null>(null);
    const [effectiveLimits, setEffectiveLimits] = useState<MCPGlobalLimits | null>(null);
    const [capacity, setCapacity] = useState<LimitCapacityInfo | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);
    const [hasChanges, setHasChanges] = useState(false);

    const hasParent = !!group.parent_group_id;
    // A limit the gateway cannot read would refuse every call: never sent.
    const problem = mcpExecutionLimitsProblem(limits);

    // Load limits
    useEffect(() => {
        runBackground(loadLimits(), 'limits');
    }, [group.id]);

    const loadLimits = async () => {
        try {
            setLoading(true);
            setError('');
            const data = await fetchMCPGlobalLimits(group.id);
            setLimits(data.mcp_global_limits || {});
            setInheritedLimits(data.inherited_mcp_limits || null);
            setEffectiveLimits(data.effective_mcp_limits || null);
            setCapacity(data.capacity || null);
            setHasChanges(false);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load MCP limits'));
        } finally {
            setLoading(false);
        }
    };

    // Apply an edit from the presentational form
    const handleChange = (next: MCPGlobalLimits) => {
        setLimits(next);
        setHasChanges(true);
    };

    // Handle save
    const handleSave = async () => {
        if (problem) return;
        try {
            setSaving(true);
            setError('');
            const saved = await updateMCPGlobalLimits(group.id, withoutRefused('mcp_limits', limits));
            // The save response carries the capacity left AFTER this write, as
            // on the LLM tab: a stale figure invites the over-commit the API
            // refuses.
            if (saved) {
                setInheritedLimits(saved.inherited_mcp_limits || null);
                setEffectiveLimits(saved.effective_mcp_limits || null);
                setCapacity(saved.capacity || null);
            }
            setHasChanges(false);
            onUpdate();
        } catch (err) {
            setError(errorMessage(err, 'Failed to update MCP limits'));
        } finally {
            setSaving(false);
        }
    };

    // Handle cancel
    const handleCancel = () => {
        runBackground(loadLimits(), 'limits');
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center p-12">
                <Loader className="w-8 h-8 animate-spin text-primary" />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">MCP Server Limits</h2>
                    <p className="text-sm text-secondary mt-1">
                        Set limits for MCP server usage. These limits apply only to this group.
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
                            disabled={saving || problem !== null}
                            title={problem ?? undefined}
                            className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-2 disabled:opacity-50"
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

            {problem && (
                <div
                    className="p-4 bg-surface-alt border border-default rounded-lg flex items-start gap-3"
                    data-testid="mcp-limits-problem"
                >
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-red-800">{problem}</p>
                </div>
            )}

            {/* Info */}
            <InfoBanner
                icon={<Shield className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />}
            >
                <p>
                    Limits use <strong>governance inheritance</strong> — the effective limit is always the most
                    restrictive value across this group and all its ancestors.
                </p>
                {hasParent && inheritedLimits && (
                    <p className="mt-1">
                        Fields marked with <ArrowDown className="w-3 h-3 inline text-amber-600" /> have inherited limits from parent groups.
                        You can only set equal or more restrictive values. Values exceeding the parent limit are shown in <span className="text-red-600 font-semibold">red</span>.
                    </p>
                )}
            </InfoBanner>

            {/* Limits Form */}
            <MCPLimitsForm
                value={limits}
                onChange={handleChange}
                inherited={inheritedLimits}
                effective={effectiveLimits}
                capacity={capacity}
            />
        </div>
    );
};

export default GroupMCPGlobalLimitsTab;
