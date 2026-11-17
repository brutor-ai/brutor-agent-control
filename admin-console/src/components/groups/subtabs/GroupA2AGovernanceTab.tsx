/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GroupA2AGovernanceTab — A2A capability / data / remote governance
 * for a resource group.
 *
 * Shape mirrors the backend `a2a_global_governance` JSON:
 *     {
 *         "capability": {"default_access": "enabled|approval_required|disabled",
 *                        "approval_timeout_seconds": <int>},
 *         "data": {"deny_classifications": [...]},
 *         "remote": {"allowed_globs": [...]}
 *     }
 *
 * The gateway judges every A2A call by the document of each level on its
 * own (this group, every group above it, the agent card) and the strictest
 * verdict is the call's. Nothing is merged first: an outbound address has
 * to match the allow-list of every level that states one, so the tab shows
 * the list of each group above by that group.
 *
 * The field rendering lives in the reusable presentational
 * `A2AGovernanceForm`; this tab owns the fetch/save/dirty-tracking and the
 * info banner + footer.
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
import A2AGovernanceForm, { A2AGovernance, type AllowListLevel } from './A2AGovernanceForm';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { governanceApprovalTimeoutProblem } from '../../../lib/approvalTimeout';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupA2AGovernanceTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { gatewayClient } = useAuth();
    const toast = useToast();

    const [gov, setGov] = useState<A2AGovernance>({});
    const [inherited, setInherited] = useState<A2AGovernance | null>(null);
    const [effective, setEffective] = useState<A2AGovernance | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [dirty, setDirty] = useState(false);
    // The allow-list of each group above that states one, from the top down,
    // and the Control Plane's sentence on how the lists apply.
    const [allowListsAbove, setAllowListsAbove] = useState<AllowListLevel[]>([]);
    const [allowListsRule, setAllowListsRule] = useState<string | undefined>(undefined);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const resp = await gatewayClient.getGroupA2AGovernance(group.id);
            setGov((resp.a2a_global_governance as A2AGovernance) ?? {});
            setInherited((resp.inherited_a2a_governance) ?? null);
            setEffective((resp.effective_a2a_governance) ?? null);
            setAllowListsAbove(allowListsAboveGroup(resp.remote_allow_lists ?? [], group.id));
            setAllowListsRule(resp.remote_allow_lists_rule);
            setDirty(false);
        } catch (e) {
            setError(errorMessage(e, 'Failed to load A2A governance'));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, group.id]);

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    // Apply an edit from the presentational form
    const handleChange = (next: A2AGovernance | Record<string, any>) => {
        setGov(next as A2AGovernance);
        setDirty(true);
    };

    // An approval timeout the Control Plane refuses is not sent: Save stays off while there is one.
    const timeoutProblem = governanceApprovalTimeoutProblem(gov);

    async function save() {
        if (timeoutProblem !== null) return;
        setSaving(true);
        try {
            await gatewayClient.updateResourceGroup(group.id, {
                a2a_global_governance: stripEmpty(gov),
            });
            toast.success('A2A governance saved');
            setDirty(false);
            onUpdate();
            await load();
        } catch (e) {
            toast.error('Save failed', errorMessage(e, 'Failed to update A2A governance'));
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
            <InfoBanner>
                <p data-testid="a2a-governance-banner">
                    The gateway judges every A2A call by this policy, by the policy of every group above, and by the
                    policy of the agent card the call addresses. Each is judged on its own and the strictest verdict
                    wins, so a group can tighten what is set above it and never relax it. An outbound address must
                    match the allow-list of every level that states one.
                </p>
            </InfoBanner>

            {error && (
                <div className="rounded-lg bg-surface-alt border border-default p-3 flex items-center space-x-2 text-sm text-red-700">
                    <AlertCircle className="w-4 h-4" />
                    <span>{error}</span>
                </div>
            )}

            <A2AGovernanceForm
                value={gov}
                onChange={handleChange}
                inherited={inherited}
                effective={effective}
                allowListsAbove={allowListsAbove}
                allowListsRule={allowListsRule}
            />

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
                    disabled={!dirty || saving || timeoutProblem !== null}
                    title={timeoutProblem ?? undefined}
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

export default GroupA2AGovernanceTab;

// ─── helpers ───────────────────────────────────────────────────────────

function stripEmpty(gov: A2AGovernance): A2AGovernance {
    const out: A2AGovernance = {};
    if (gov.capability) {
        const cap: NonNullable<A2AGovernance['capability']> = {};
        if (gov.capability.default_access) cap.default_access = gov.capability.default_access;
        if (
            gov.capability.approval_timeout_seconds !== null &&
            gov.capability.approval_timeout_seconds !== undefined
        ) {
            cap.approval_timeout_seconds = gov.capability.approval_timeout_seconds;
        }
        if (Object.keys(cap).length) out.capability = cap;
    }
    if (gov.data?.deny_classifications && gov.data.deny_classifications.length) {
        out.data = { deny_classifications: [...gov.data.deny_classifications] };
    }
    if (gov.remote?.allowed_globs && gov.remote.allowed_globs.length) {
        out.remote = { allowed_globs: [...gov.remote.allowed_globs] };
    }
    return out;
}

/**
 * The allow-lists of the groups above `groupId`, from the answer's
 * `remote_allow_lists`: every level that states a list, root first, the
 * group's own included. The group's own list is the one the form edits, so
 * it is left out; so is a row that names no group or no pattern.
 */
function allowListsAboveGroup(levels: readonly Record<string, unknown>[], groupId: string): AllowListLevel[] {
    return levels.flatMap((row) => {
        const globs = Array.isArray(row.allowed_globs) ? row.allowed_globs.filter((glob): glob is string => typeof glob === 'string') : [];
        if (row.group_id === groupId || typeof row.group_name !== 'string' || globs.length === 0) return [];
        return [{ level: row.group_name, globs }];
    });
}
