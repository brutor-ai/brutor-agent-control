/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GroupA2ALimitsTab — A2A invocation + delegation-chain limits for a
 * resource group.
 *
 * Shape mirrors the backend `a2a_global_limits` JSON:
 *     {
 *         "invocations": {daily_limit, monthly_limit},
 *         "chain": {max_delegation_depth}
 *     }
 *
 * Inheritance is surfaced the same way as the skill/MCP/LLM limit
 * panels — the "Inherited" chip shows the ancestor ceiling when the
 * group itself has no explicit value, and any number the admin sets
 * here can only tighten (min) what came down the chain.
 *
 * The field rendering lives in the reusable presentational `A2ALimitsForm`;
 * this tab owns the fetch/save/dirty-tracking and the info banner + footer.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    AlertCircle,
    Loader2,
    RotateCcw,
    Save,
} from 'lucide-react';

import { useAuth } from '../../../hooks/useAuth';
import { useToast } from '../../common/Toast';
import { ResourceGroup } from '../../../lib/groupTypes';
import InfoBanner from '../../common/InfoBanner';
import A2ALimitsForm, {
    ABSOLUTE_MAX_DEPTH,
    A2ALimits,
    ChainLimits,
    InvocationLimits,
} from './A2ALimitsForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { withoutRefused } from '../../../lib/refusedSettings';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupA2ALimitsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { gatewayClient } = useAuth();
    const toast = useToast();

    const [limits, setLimits] = useState<A2ALimits>({});
    const [inherited, setInherited] = useState<A2ALimits | null>(null);
    const [effective, setEffective] = useState<A2ALimits | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [dirty, setDirty] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const resp = await gatewayClient.getGroupA2ALimits(group.id);
            setLimits((resp.a2a_global_limits as A2ALimits) ?? {});
            setInherited((resp.inherited_a2a_limits) ?? null);
            setEffective((resp.effective_a2a_limits) ?? null);
            setDirty(false);
        } catch (e) {
            setError(errorMessage(e) ?? 'Failed to load A2A limits');
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, group.id]);

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    // Apply an edit from the presentational form
    const handleChange = (next: A2ALimits | Record<string, any>) => {
        setLimits(next as A2ALimits);
        setDirty(true);
    };

    async function save() {
        setSaving(true);
        try {
            // PATCH the resource group with the new a2a_global_limits JSON.
            // The backend validator enforces shape + ranges and the restrictive
            // inheritance re-runs on every subsequent effective-view fetch.
            await gatewayClient.updateResourceGroup(group.id, {
                a2a_global_limits: withoutRefused('a2a_limits', stripNulls(limits)),
            });
            toast.success('A2A limits saved');
            setDirty(false);
            onUpdate();
            await load();
        } catch (e) {
            toast.error('Save failed', errorMessage(e) ?? 'Failed to update A2A limits');
        } finally {
            setSaving(false);
        }
    }

    if (loading) {
        return (
            <div className="flex items-center justify-center py-16">
                <Loader2 className="w-6 h-6 animate-spin text-muted" />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-5">
            {/* Info box */}
            <InfoBanner>
                <p>
                    Limits cap how many A2A invocations this group can make and
                    how deep its delegation chains can grow. Values inherit
                    restrictively from ancestor groups (min wins; children can
                    tighten but never relax). The proxy refuses any chain above{' '}
                    {ABSOLUTE_MAX_DEPTH} delegations regardless of this setting.
                </p>
            </InfoBanner>

            {error && (
                <div className="rounded-lg bg-surface-alt border border-default p-3 flex items-center space-x-2 text-sm text-red-700">
                    <AlertCircle className="w-4 h-4" />
                    <span>{error}</span>
                </div>
            )}

            <A2ALimitsForm
                value={limits}
                onChange={handleChange}
                inherited={inherited}
                effective={effective}
            />

            {/* Footer */}
            <div className="flex justify-end space-x-2 pt-2">
                <button
                    type="button"
                    onClick={asyncHandler(load, 'click')}
                    disabled={!dirty || saving}
                    className="flex items-center space-x-1.5 px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                >
                    <RotateCcw className="w-4 h-4" />
                    <span>Revert</span>
                </button>
                <button
                    type="button"
                    onClick={asyncHandler(save, 'click')}
                    disabled={!dirty || saving}
                    className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors disabled:opacity-50"
                >
                    {saving ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                        <Save className="w-4 h-4" />
                    )}
                    <span>Save</span>
                </button>
            </div>
        </div>
    );
};

export default GroupA2ALimitsTab;

// ─── helpers ───────────────────────────────────────────────────────────

function stripNulls(obj: A2ALimits): A2ALimits {
    const out: A2ALimits = {};
    if (obj.invocations) {
        const inv: InvocationLimits = {};
        for (const k of Object.keys(obj.invocations) as (keyof InvocationLimits)[]) {
            const v = obj.invocations[k];
            if (v !== null && v !== undefined) (inv as any)[k] = v;
        }
        if (Object.keys(inv).length) out.invocations = inv;
    }
    if (obj.chain) {
        const c: ChainLimits = {};
        for (const k of Object.keys(obj.chain) as (keyof ChainLimits)[]) {
            const v = obj.chain[k];
            if (v !== null && v !== undefined) (c as any)[k] = v;
        }
        if (Object.keys(c).length) out.chain = c;
    }
    return out;
}
