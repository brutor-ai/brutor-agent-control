/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Per-tool MCP rate limits — modelled after MCPCapabilityFilterDialog.
 *
 * Lists every discovered tool on an MCP server; each row carries its own
 * token-bucket / quota / cooldown override. Per-tool limits are stored
 * under `resource_group_server_configs.limits.per_tool` — the same JSON
 * blob the server-wide MCPServerLimitsDialog writes — so save is a
 * read-modify-write that touches only the `per_tool` section and leaves
 * the server-wide execution / data / frequency limits untouched.
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, Loader, Gauge, Info, AlertTriangle, Trash2, Plus } from 'lucide-react';
import { ServerConfigInfo, MCPFrequencyLimits } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import { TABLE_THEAD, TABLE_TH_TYPE } from '../../ui/tableStyles';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';
import { withoutRefused } from '../../../lib/refusedSettings';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSave?: () => void;
    groupId: string;
    groupName: string;
    server: ServerConfigInfo;
}

/**
 * One entry per editable column. `help` is the operator-facing
 * explanation surfaced via the column-header info popover — see
 * `usage.rs::enforce_rate_scope` for what each field actually does
 * at enforcement time. Keep this copy in lockstep with that code
 * (token bucket = /min + burst; /hour and /day are independent
 * fixed-window quotas; cooldown is a penalty lockout).
 */
const FREQUENCY_FIELDS: {
    key: keyof MCPFrequencyLimits;
    label: string;
    help: {
        title: string;
        algorithm: string;
        whenToUse: string;
        example: string;
    };
}[] = [
    {
        key: 'max_calls_per_minute',
        label: '/min',
        help: {
            title: 'Calls per minute (token-bucket refill rate)',
            algorithm:
                'Sustained rate ceiling enforced by a token bucket. One token per call; the bucket refills at this rate. Combined with Burst (bucket size). Counter is per-tool, per-resource-group, exact across gateway replicas.',
            whenToUse:
                'Protect against runaway agents, scrapers, and short-window abuse. Tighter values for tools that are expensive or hit fragile downstream APIs (databases, slow upstream MCPs, paid third-party APIs).',
            example:
                '/min 60 + Burst 10 → caller can do 10 calls back-to-back, then is throttled to ~1/sec. On trip: HTTP 429 with Retry-After.',
        },
    },
    {
        key: 'max_calls_per_hour',
        label: '/hour',
        help: {
            title: 'Calls per hour (fixed-window quota)',
            algorithm:
                'Independent fixed-window quota. The window is epoch-aligned (resets every clock hour boundary), counts every accepted call, and is enforced on top of /min — both must allow the request.',
            whenToUse:
                'Catch slow-burn abuse that stays under the /min ceiling (e.g. 50 calls/min sustained for an hour = 3000 calls). Also the natural place to set a sustained-load contract for fair-use plans.',
            example:
                '/hour 1000 → on the 1001st call within the same clock hour, the caller gets HTTP 429 + Retry-After = seconds until the next hour starts.',
        },
    },
    {
        key: 'max_calls_per_day',
        label: '/day',
        help: {
            title: 'Calls per day (fixed-window quota)',
            algorithm:
                'Independent fixed-window quota; rolls at the epoch-aligned day boundary (UTC). Same enforcement layer as /hour. All three windows are checked separately — the strictest applicable one wins.',
            whenToUse:
                'Cost / budget ceiling for expensive tools (LLM-backed search, large queries, paid third-party APIs). Fair-use cap per tenant / user. Compliance limits where a tool can only be invoked N times per day.',
            example:
                '/day 5000 → predictable upper bound on cost and downstream load. Retry-After on trip = seconds until midnight UTC.',
        },
    },
    {
        key: 'burst_limit',
        label: 'Burst',
        help: {
            title: 'Burst (token-bucket capacity)',
            algorithm:
                'Maximum number of tokens the bucket can hold. Sets how many calls can fire back-to-back before the /min refill rate becomes the binding constraint. Inert without /min.',
            whenToUse:
                'Tune for legitimate spike traffic. A single LLM prompt may produce N parallel tool calls — set Burst ≥ the largest realistic parallel fan-out so legitimate use does not get rejected on the first request. Default to ~10–20% of /min.',
            example:
                '/min 120 + Burst 20 → caller can do 20 quickly, then is throttled to 2/sec refill until the bucket fills again.',
        },
    },
    {
        key: 'cooldown_seconds',
        label: 'Cooldown s',
        help: {
            title: 'Cooldown seconds (penalty lockout)',
            algorithm:
                'After ANY rate trip on this scope, the caller is locked out for this many seconds — no calls accepted regardless of bucket state. Modeled on GitHub "secondary rate limits" / Cloudflare actions. Inert without /min.',
            whenToUse:
                'Anti-abuse signal: the caller crossed a line, stop trying. Use on destructive or sensitive tools where you want misbehaving callers to back off completely, not just slow down. Skip for ordinary read-only tools.',
            example:
                'Cooldown 60 → first 429 arms a 60-second lockout. Every subsequent attempt during the lockout returns 429 + Retry-After = remaining seconds, regardless of whether the bucket has refilled.',
        },
    },
];

/** One editable per-tool row. `rid` is a stable React key. */
interface ToolRow {
    rid: number;
    tool: string;
    limits: MCPFrequencyLimits;
    /** True when the tool came from the server's discovered capability list. */
    discovered: boolean;
    description?: string;
}

const MCPToolLimitsDialog: React.FC<Props> = ({
    isOpen,
    onClose,
    onSave,
    groupId,
    groupName,
    server,
}) => {
    const { fetchMCPServerLimits, updateMCPServerLimits, getMCPCapabilityFilters } = useGateway();
    const toast = useToast();

    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [readFailure, reads] = useReadFailures<'tools'>();
    const [error, setError] = useState('');
    const [rows, setRows] = useState<ToolRow[]>([]);
    const [newToolName, setNewToolName] = useState('');
    // The server-wide sections (execution / data / frequency) are preserved
    // verbatim on save — this dialog only edits `per_tool`.
    const [otherLimits, setOtherLimits] = useState<Record<string, any>>({});
    // Which column header's help popover is open; null = none.
    const [helpKey, setHelpKey] = useState<keyof MCPFrequencyLimits | null>(null);
    const helpRef = useRef<HTMLDivElement | null>(null);
    const ridCounter = useRef(0);

    // Close the help popover when clicking outside of it. Mounted only
    // while a popover is open so we don't carry a listener for free.
    useEffect(() => {
        if (!helpKey) return;
        const onDocClick = (e: MouseEvent) => {
            if (helpRef.current && !helpRef.current.contains(e.target as Node)) {
                setHelpKey(null);
            }
        };
        const onEsc = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setHelpKey(null);
        };
        document.addEventListener('mousedown', onDocClick);
        document.addEventListener('keydown', onEsc);
        return () => {
            document.removeEventListener('mousedown', onDocClick);
            document.removeEventListener('keydown', onEsc);
        };
    }, [helpKey]);

    const loadLimits = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const [limitsResp, caps] = await Promise.all([
                fetchMCPServerLimits(groupId, server.id),
                getMCPCapabilityFilters(groupId, server.id).then(
                    (answer) => {
                        reads.loaded('tools');
                        return answer;
                    },
                    (err: unknown) => {
                        reads.lost('tools', 'Failed to load the tools of this server; only tools that already have a limit are listed', err);
                        return null;
                    },
                ),
            ]);

            const limits = (limitsResp.limits || {});
            const perTool = (limits.per_tool || {}) as Record<string, MCPFrequencyLimits>;
            // Keep everything except per_tool so save can write it back intact.
            const rest: Record<string, any> = { ...limits };
            delete rest.per_tool;
            setOtherLimits(rest);

            const discoveredTools = (caps?.discovered_capabilities || [])
                .filter(c => c.capability_type === 'tool');
            const discoveredNames = new Set(discoveredTools.map(t => t.name));

            const next: ToolRow[] = [];
            // One row per discovered tool, pre-filled with any saved limits.
            for (const t of discoveredTools) {
                next.push({
                    rid: ridCounter.current++,
                    tool: t.name,
                    limits: perTool[t.name] || {},
                    discovered: true,
                    description: t.description,
                });
            }
            // Saved per-tool limits for tools that are not (or no longer)
            // discovered still need a row so they remain editable.
            for (const [name, lim] of Object.entries(perTool)) {
                if (!discoveredNames.has(name)) {
                    next.push({
                        rid: ridCounter.current++,
                        tool: name,
                        limits: lim || {},
                        discovered: false,
                    });
                }
            }
            setRows(next);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load tool limits');
        } finally {
            setLoading(false);
        }
    }, [groupId, server.id, fetchMCPServerLimits, getMCPCapabilityFilters, reads]);

    useEffect(() => {
        if (isOpen) runBackground(loadLimits(), 'limits');
    }, [isOpen, loadLimits]);

    const updateLimit = (rid: number, field: keyof MCPFrequencyLimits, value: string) => {
        setRows(prev =>
            prev.map(r =>
                r.rid === rid
                    ? { ...r, limits: { ...r.limits, [field]: value === '' ? undefined : Number(value) } }
                    : r,
            ),
        );
    };

    const removeRow = (rid: number) => {
        setRows(prev => prev.filter(r => r.rid !== rid));
    };

    const addManualTool = () => {
        const name = newToolName.trim();
        if (!name) return;
        if (rows.some(r => r.tool === name)) {
            toast.error('Duplicate tool', `"${name}" is already in the list`);
            return;
        }
        setRows(prev => [
            ...prev,
            { rid: ridCounter.current++, tool: name, limits: {}, discovered: false },
        ]);
        setNewToolName('');
    };

    const handleSave = async () => {
        // Roll the rows back into a per_tool map, dropping rows with no
        // values; reject duplicate tool names so none is silently lost.
        const perTool: Record<string, MCPFrequencyLimits> = {};
        for (const row of rows) {
            const name = row.tool.trim();
            if (!name) continue;
            const clean: MCPFrequencyLimits = {};
            FREQUENCY_FIELDS.forEach(({ key }) => {
                const v = row.limits[key];
                if (v !== undefined && v !== null && !Number.isNaN(v)) clean[key] = v;
            });
            if (Object.keys(clean).length === 0) continue;
            if (perTool[name]) {
                toast.error('Duplicate tool', `"${name}" is listed more than once`);
                return;
            }
            perTool[name] = clean;
        }

        const payload: Record<string, any> = { ...otherLimits };
        if (Object.keys(perTool).length > 0) payload.per_tool = perTool;
        else delete payload.per_tool;

        setSaving(true);
        try {
            await updateMCPServerLimits(groupId, server.id, withoutRefused('mcp_limits', payload));
            toast.success('Saved', 'Per-tool rate limits saved');
            onSave?.();
            onClose();
        } catch (err) {
            toast.error(
                'Failed to save tool limits',
                err instanceof Error ? err.message : undefined,
            );
        } finally {
            setSaving(false);
        }
    };

    const handleClearAll = () => setRows(prev => prev.map(r => ({ ...r, limits: {} })));

    if (!isOpen) return null;

    const anyLimitSet = rows.some(r => Object.values(r.limits).some(v => v !== undefined));

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-5xl w-full max-h-[85vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <div className="p-2 bg-primary-100 rounded-lg">
                            <Gauge className="w-5 h-5 text-primary-600" />
                        </div>
                        <div>
                            <h2 className="text-lg font-semibold text-default">Tool Rate Limits</h2>
                            <p className="text-sm text-muted">
                                {server.name} &middot; {groupName}
                            </p>
                        </div>
                    </div>
                    <button onClick={onClose} className="text-muted hover:text-secondary transition-colors">
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-6 space-y-4">
                    <ReadFailureNotice failure={readFailure('tools')} onRetry={() => runBackground(loadLimits(), 'limits')} />
                    {error && (
                        <div className="flex items-start space-x-3 p-3 bg-surface-alt border border-default rounded-lg">
                            <AlertTriangle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
                            <p className="text-sm text-red-700">{error}</p>
                        </div>
                    )}

                    <div className="flex items-start space-x-3 p-3 bg-primary-pale border border-primary rounded-lg">
                        <Info className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                        <div className="text-sm text-primary">
                            Each tool gets its own rate limit, enforced on top of the
                            server-wide and group-wide limits. Per-minute + burst form a
                            token bucket; per-hour / per-day are fixed-window quotas;
                            cooldown is a penalty lockout. Leave a field blank for no limit.
                        </div>
                    </div>

                    {loading ? (
                        <div className="flex items-center justify-center h-48">
                            <Loader className="w-8 h-8 text-primary-600 animate-spin" />
                        </div>
                    ) : (
                        <>
                            {/* Manual add */}
                            <div className="flex items-center space-x-2">
                                <input
                                    type="text"
                                    value={newToolName}
                                    onChange={e => setNewToolName(e.target.value)}
                                    onKeyDown={e => e.key === 'Enter' && addManualTool()}
                                    placeholder="Add tool name manually..."
                                    className="flex-1 px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent font-mono min-h-0 min-w-0"
                                />
                                <button
                                    onClick={addManualTool}
                                    disabled={!newToolName.trim()}
                                    className="flex items-center space-x-1 px-3 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    <Plus className="w-4 h-4" />
                                    <span>Add</span>
                                </button>
                            </div>

                            {rows.length === 0 ? (
                                <div className="text-center py-8 text-muted text-sm">
                                    No tools discovered for this server. Add a tool name above
                                    to set a limit.
                                </div>
                            ) : (
                                <div className="space-y-1">
                                    {/* Column headers — each carries an Info popover
                                        explaining the limit semantics + when to use it.
                                        Click toggles; click-outside / Escape closes. */}
                                    <div className={`grid grid-cols-[1.8fr_repeat(5,1fr)_auto] gap-2 px-3 py-2 ${TABLE_THEAD} ${TABLE_TH_TYPE}`}>
                                        <span>Tool</span>
                                        {FREQUENCY_FIELDS.map(f => {
                                            const open = helpKey === f.key;
                                            return (
                                                <div key={f.key} className="relative">
                                                    <button
                                                        type="button"
                                                        onClick={() => setHelpKey(open ? null : f.key)}
                                                        className={`inline-flex items-center gap-1 text-muted hover:text-primary transition-colors uppercase ${
                                                            open ? 'text-primary' : ''
                                                        }`}
                                                        aria-expanded={open}
                                                        aria-controls={`help-${f.key}`}
                                                        title={`What does ${f.label} mean?`}
                                                    >
                                                        <span>{f.label}</span>
                                                        <Info className="w-3 h-3" />
                                                    </button>
                                                    {open && (
                                                        <div
                                                            id={`help-${f.key}`}
                                                            ref={helpRef}
                                                            role="dialog"
                                                            aria-label={f.help.title}
                                                            className="absolute z-20 left-0 top-full mt-1 w-80 p-3 bg-surface border border-default rounded-lg shadow-lg normal-case tracking-normal"
                                                        >
                                                            <div className="text-sm font-semibold text-default mb-2">
                                                                {f.help.title}
                                                            </div>
                                                            <div className="text-xs text-secondary mb-2">
                                                                <span className="font-medium text-default">Algorithm: </span>
                                                                {f.help.algorithm}
                                                            </div>
                                                            <div className="text-xs text-secondary mb-2">
                                                                <span className="font-medium text-default">When to use: </span>
                                                                {f.help.whenToUse}
                                                            </div>
                                                            <div className="text-xs text-secondary">
                                                                <span className="font-medium text-default">Example: </span>
                                                                {f.help.example}
                                                            </div>
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                        <span />
                                    </div>
                                    {rows.map(row => (
                                        <div
                                            key={row.rid}
                                            className="grid grid-cols-[1.8fr_repeat(5,1fr)_auto] gap-2 items-center bg-surface-alt rounded-lg px-3 py-2"
                                        >
                                            <div className="min-w-0">
                                                <span className="text-sm font-mono text-default truncate block">
                                                    {row.tool}
                                                </span>
                                                {row.description && (
                                                    <span className="text-xs text-muted truncate block">
                                                        {row.description}
                                                    </span>
                                                )}
                                                {!row.discovered && (
                                                    <span className="text-[10px] text-amber-600">
                                                        not discovered
                                                    </span>
                                                )}
                                            </div>
                                            {FREQUENCY_FIELDS.map(({ key }) => (
                                                <input
                                                    key={key}
                                                    type="number"
                                                    min={1}
                                                    value={row.limits[key] ?? ''}
                                                    onChange={e => updateLimit(row.rid, key, e.target.value)}
                                                    className="min-w-0 px-2 py-1.5 text-sm border border-strong rounded bg-surface text-default"
                                                />
                                            ))}
                                            {row.discovered ? (
                                                <span className="w-6" />
                                            ) : (
                                                <button
                                                    onClick={() => removeRow(row.rid)}
                                                    className="w-6 h-6 rounded flex items-center justify-center text-muted hover:text-red-600 hover:bg-surface-raised transition-colors"
                                                    title="Remove tool"
                                                >
                                                    <Trash2 className="w-4 h-4" />
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between p-6 border-t border-default bg-surface-alt">
                    <div>
                        {anyLimitSet && (
                            <button
                                onClick={handleClearAll}
                                className="text-sm text-muted hover:text-red-600 transition-colors"
                            >
                                Clear all tool limits
                            </button>
                        )}
                    </div>
                    <div className="flex items-center space-x-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
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

export default MCPToolLimitsDialog;
