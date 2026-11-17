/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GroupApprovalWindowTab — Governance → Approvals.
 *
 * How long a human-approval hold raised for this group stays open when the
 * policy that raised it names no window of its own: skill runs, A2A calls,
 * agent-grant holds and every hold raised while the system is demoted to
 * `approval_required`. Min-wins down the tree; the tenant default and the
 * platform's 5 minutes sit behind it. A tool's own `approval_timeout_seconds`
 * (MCP capability filter) still overrides for that tool.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { AlertCircle, Loader2, RotateCcw, Save } from 'lucide-react';

import { useAuth } from '../../../hooks/useAuth';
import { useToast } from '../../common/Toast';
import { ResourceGroup } from '../../../lib/groupTypes';
import InfoBanner from '../../common/InfoBanner';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import {
    APPROVAL_WINDOW_PRESETS,
    ApprovalWindow,
    effectiveText,
    windowProblem,
    windowText,
} from '../../../lib/approvalWindow';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupApprovalWindowTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { gatewayClient } = useAuth();
    const toast = useToast();

    const [data, setData] = useState<ApprovalWindow | null>(null);
    /** Minutes as typed; '' = not stated on this group (inherit). */
    const [minutes, setMinutes] = useState('');
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // The read is a promise chain, so every setState happens after the
    // response (none synchronously inside the effect); Revert reuses it.
    const load = useCallback(
        () =>
            gatewayClient.getApprovalWindow(group.id).then(
                (resp) => {
                    setError(null);
                    setData(resp);
                    setMinutes(resp.approval_window_seconds != null ? String(resp.approval_window_seconds / 60) : '');
                    setLoading(false);
                },
                (e: unknown) => {
                    setError(errorMessage(e) ?? 'Failed to load the approval window');
                    setLoading(false);
                },
            ),
        [gatewayClient, group.id],
    );

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    const seconds = minutes.trim() === '' ? null : Math.round(Number(minutes) * 60);
    const stored = data?.approval_window_seconds ?? null;
    const dirty = seconds !== stored;
    const problem =
        minutes.trim() !== '' && !Number.isFinite(Number(minutes))
            ? 'Enter a number of minutes.'
            : windowProblem(seconds, data?.inherited_window_seconds);

    async function save() {
        setSaving(true);
        try {
            const resp = await gatewayClient.updateApprovalWindow(group.id, {
                approval_window_seconds: seconds,
            });
            setData(resp);
            toast.success('Approval window saved');
            onUpdate();
        } catch (e) {
            toast.error('Save failed', errorMessage(e) ?? 'Failed to update the approval window');
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
        <div className="p-6 space-y-5" data-testid="approval-window-tab">
            <InfoBanner>
                <p>
                    How long a hold waits for a human before it expires, when the policy that raised it
                    names no window of its own — skill runs, agent calls, agent grants, and every hold
                    raised while the AI System is demoted to <em>approval required</em>. A tool&apos;s own
                    approval timeout (MCP capability filter) still wins for that tool. Inherited
                    most-restrictively: a group may shorten the window it inherits, never lengthen it.
                </p>
            </InfoBanner>

            {error && (
                <div className="rounded-lg bg-surface-alt border border-default p-3 flex items-center space-x-2 text-sm text-red-700">
                    <AlertCircle className="w-4 h-4" />
                    <span>{error}</span>
                </div>
            )}

            {data && (
                <p className="text-sm text-default" data-testid="approval-window-effective">
                    {effectiveText(data)}
                </p>
            )}

            <div className="space-y-2">
                <label htmlFor="approval-window-minutes" className="block text-sm font-medium text-default">
                    Default approval window on this group (minutes)
                </label>
                <div className="flex items-center gap-2 flex-wrap">
                    <input
                        id="approval-window-minutes"
                        type="number"
                        min={1}
                        max={10080}
                        step={1}
                        value={minutes}
                        placeholder="inherit"
                        onChange={(e) => setMinutes(e.target.value)}
                        className="w-40 px-3 py-2 text-sm border border-strong rounded-lg bg-surface text-default"
                    />
                    {seconds != null && !problem && (
                        <span className="text-xs text-secondary">= {windowText(seconds)}</span>
                    )}
                    {APPROVAL_WINDOW_PRESETS.map((p) => (
                        <button
                            key={p.seconds}
                            type="button"
                            onClick={() => setMinutes(String(p.seconds / 60))}
                            className="px-2.5 py-1 text-xs border border-default rounded-md text-secondary hover:bg-surface-alt"
                        >
                            {p.label}
                        </button>
                    ))}
                    <button
                        type="button"
                        onClick={() => setMinutes('')}
                        className="px-2.5 py-1 text-xs border border-default rounded-md text-secondary hover:bg-surface-alt"
                    >
                        Inherit
                    </button>
                </div>
                {data?.inherited_window_seconds != null && (
                    <p className="text-xs text-secondary">
                        Inherited ceiling: {windowText(data.inherited_window_seconds)} from &apos;
                        {data.inherited_from_group_name ?? data.inherited_from_group_id}&apos;.
                    </p>
                )}
                {data && (
                    <p className="text-xs text-secondary">
                        Tenant default:{' '}
                        {data.tenant_default_seconds != null ? windowText(data.tenant_default_seconds) : 'not set'} ·
                        platform default: {windowText(data.platform_default_seconds)}
                    </p>
                )}
                {problem && (
                    <p className="text-xs text-red-700" data-testid="approval-window-problem">
                        {problem}
                    </p>
                )}
            </div>

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
                    disabled={!dirty || saving || problem != null}
                    className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors disabled:opacity-50"
                >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    <span>Save</span>
                </button>
            </div>
        </div>
    );
};

export default GroupApprovalWindowTab;
