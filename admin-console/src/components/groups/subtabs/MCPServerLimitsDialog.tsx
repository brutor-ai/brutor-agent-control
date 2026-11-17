/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Server-wide per-server MCP limits — execution / data / frequency.
 *
 * Per-tool rate-limit overrides live under the same `limits` JSON blob
 * (`per_tool`) but are edited in the separate MCPToolLimitsDialog. This
 * dialog loads that key and writes it back untouched so the two dialogs
 * share the blob without clobbering each other.
 */

import { useState, useEffect, useCallback } from 'react';
import { ArrowDown, Info } from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import type { ServerConfigInfo, PerServerCapacityInfo, MCPGlobalLimits, MCPFrequencyLimits } from '../../../lib/groupTypes';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { errorMessage } from '../../../lib/narrow';
import UnavailableSettings from '../../common/UnavailableSettings';
import { withoutRefused } from '../../../lib/refusedSettings';
import { MCP_LIMITS_NOT_AVAILABLE } from './MCPLimitsForm';
import { MCP_LIMIT_HELP, mcpExecutionLimitsProblem } from '../../../lib/mcpExecutionLimits';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSave?: () => void;
    groupId: string;
    groupName: string;
    server: ServerConfigInfo;
}

/**
 * The binding's limits: an MCP limits document (the contract's
 * `McpLimitsDocument`). `per_tool` holds the per-tool rate-limit overrides,
 * owned by MCPToolLimitsDialog: loaded and saved back untouched so this
 * dialog never clobbers them.
 */
type MCPServerLimitsData = MCPGlobalLimits;
type ExecutionLimits = NonNullable<MCPGlobalLimits['execution']>;
type DataLimits = NonNullable<MCPGlobalLimits['data']>;

/** Get a nested value from a limits object by section.field path */
const getNestedValue = (limits: Record<string, any> | null | undefined, section: string, field: string): number | undefined => {
    if (!limits) return undefined;
    const sec = limits[section] as Record<string, any> | undefined;
    return sec?.[field] ?? undefined;
};

function MCPServerLimitsDialog({ isOpen, onClose, onSave, groupId, groupName, server }: Props) {
    const { fetchMCPServerLimits, updateMCPServerLimits, clearMCPServerLimits } = useGateway();
    const toast = useToast();

    const [limits, setLimits] = useState<MCPServerLimitsData>({});
    const [globalLimits, setGlobalLimits] = useState<MCPServerLimitsData>({});
    const [capacity, setCapacity] = useState<PerServerCapacityInfo | null>(null);
    const [capacityPopoverField, setCapacityPopoverField] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    // The Control Plane's refusal of the last save, as it words it.
    const [saveError, setSaveError] = useState('');
    // A limit the gateway cannot read would refuse every call: never sent.
    const problem = mcpExecutionLimitsProblem(limits);

    const loadLimits = useCallback(async () => {
        setLoading(true);
        try {
            const response = await fetchMCPServerLimits(groupId, server.id);
            setLimits(response.limits || {});
            setGlobalLimits(response.effective_global_limits || {});
            setCapacity(response.capacity || null);
        } catch (err) {
            toast.error('Failed to load server limits', errorMessage(err, '') || undefined);
        } finally {
            setLoading(false);
        }
    }, [groupId, server.id, fetchMCPServerLimits]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadLimits(), 'limits');
            setCapacityPopoverField(null);
            setSaveError('');
        }
    }, [isOpen, loadLimits]);

    const handleSave = async () => {
        // `limits` carries any per_tool overrides loaded from the server;
        // object spreads in the field updaters preserve them, so they are
        // written back untouched (per-tool limits are edited separately).
        if (problem) return;
        setSaving(true);
        setSaveError('');
        try {
            // A limit the gateway does not act on in this release is not
            // sent: the save removes one stored before.
            await updateMCPServerLimits(groupId, server.id, withoutRefused('mcp_limits', { ...limits }));
            toast.success('Saved', 'Server limits saved successfully');
            onSave?.();
            onClose();
        } catch (err) {
            const said = errorMessage(err, '');
            setSaveError(said || 'Failed to save server limits');
            toast.error('Failed to save server limits', said || undefined);
        } finally {
            setSaving(false);
        }
    };

    const handleClear = async () => {
        setSaving(true);
        try {
            await clearMCPServerLimits(groupId, server.id);
            toast.success('Cleared', 'Server limits cleared');
            setLimits({});
            onSave?.();
            onClose();
        } catch (err) {
            toast.error('Failed to clear server limits', errorMessage(err, '') || undefined);
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

    const updateData = (field: keyof DataLimits, value: string) => {
        setLimits(prev => ({
            ...prev,
            data: {
                ...prev.data,
                [field]: value === '' ? undefined : Number(value),
            },
        }));
    };

    const updateFrequency = (field: keyof MCPFrequencyLimits, value: string) => {
        setLimits(prev => ({
            ...prev,
            frequency: {
                ...prev.frequency,
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
        current: unknown,
        global: unknown,
        onChange: (value: string) => void,
        opts: { isPercent?: boolean } = {}
    ) => {
        // `frequency` is an open record in the document, and a stated limit may be null (unset).
        const currentValue = typeof current === 'number' ? current : undefined;
        const globalValue = typeof global === 'number' ? global : undefined;
        const available = capacity ? getNestedValue(capacity.available, section, field) : undefined;
        const otherServersTotal = capacity ? getNestedValue(capacity.other_servers_total, section, field) : undefined;
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
                        Exceeds available capacity ({available}{suffix}) — other servers already allocated {otherServersTotal ?? 0}{suffix} of group's {groupGlobalLimit}{suffix}
                    </p>
                )}
                <span className="text-xs text-muted mt-0.5">Effective: {effectiveValue(globalValue, currentValue)}{suffix}</span>
                {MCP_LIMIT_HELP.binding[fieldKey] && (
                    <p className="text-xs text-muted mt-1" data-testid={`limit-help-${fieldKey}`}>
                        {MCP_LIMIT_HELP.binding[fieldKey]}
                    </p>
                )}
                {showPopover && hasCapacity && capacity && (
                    <div className="mt-1.5 p-3 bg-surface-alt border border-default rounded-lg text-xs space-y-1.5 shadow-sm">
                        <div className="flex justify-between">
                            <span className="text-secondary">Group Global Limit ({capacity.group_name ?? groupName}):</span>
                            <span className="font-medium text-default">{groupGlobalLimit}{suffix}</span>
                        </div>
                        {capacity.other_servers.length > 0 && (
                            <>
                                <div className="text-muted font-medium pt-1">Other Servers:</div>
                                {capacity.other_servers.map(s => {
                                    const sVal = getNestedValue(s.limits, section, field);
                                    return (
                                        <div key={s.server_config_id} className="flex justify-between pl-3">
                                            <span className="text-secondary">{s.server_name}:</span>
                                            <span className="text-default">{sVal !== undefined ? `${sVal}${suffix}` : '—'}</span>
                                        </div>
                                    );
                                })}
                                <div className="flex justify-between border-t border-default pt-1.5">
                                    <span className="text-secondary">Other servers total:</span>
                                    <span className="font-medium text-default">{otherServersTotal ?? 0}{suffix}</span>
                                </div>
                            </>
                        )}
                        <div className="flex justify-between border-t border-default pt-1.5">
                            <span className="font-semibold text-default">Available for this server:</span>
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
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-3xl max-h-[90vh] overflow-y-auto">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <h2 className="text-lg font-semibold text-default">
                        Per-Server Limits &mdash; {server.name}
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
                    These server-wide limits apply to <strong>{server.name}</strong> in <strong>{groupName}</strong>.
                    The effective limit is min(global, per-server). Per-tool rate-limit
                    overrides are configured from the <strong>Limits</strong> column.
                    {capacity && (
                        <span className="ml-1">
                            Fields marked with <ArrowDown className="w-3 h-3 inline text-amber-600" /> show remaining capacity from the group's global limit after other servers' allocations.
                        </span>
                    )}
                </div>

                {loading ? (
                    <div className="p-6 text-center text-muted">Loading limits...</div>
                ) : (
                    <>
                    <div className="px-6 pt-6">
                        <UnavailableSettings doc={limits} settings={MCP_LIMITS_NOT_AVAILABLE} />
                    </div>
                    <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-4">
                        {/* Execution Card */}
                        <div className="border border-default rounded-lg p-4">
                            <h3 className="text-sm font-semibold text-default mb-3">Execution</h3>
                            <div className="space-y-3">
                                {renderLimitInput(
                                    'Timeout (seconds)', 'execution', 'timeout_seconds',
                                    limits.execution?.timeout_seconds, globalLimits.execution?.timeout_seconds,
                                    v => updateExecution('timeout_seconds', v)
                                )}
                                {renderLimitInput(
                                    'Max Concurrency / User', 'execution', 'max_concurrency_per_user',
                                    limits.execution?.max_concurrency_per_user, globalLimits.execution?.max_concurrency_per_user,
                                    v => updateExecution('max_concurrency_per_user', v)
                                )}
                                {renderLimitInput(
                                    'Max Concurrency / Group', 'execution', 'max_concurrency_per_group',
                                    limits.execution?.max_concurrency_per_group, globalLimits.execution?.max_concurrency_per_group,
                                    v => updateExecution('max_concurrency_per_group', v)
                                )}
                            </div>
                        </div>

                        {/* Data Card */}
                        <div className="border border-default rounded-lg p-4">
                            <h3 className="text-sm font-semibold text-default mb-3">Data</h3>
                            <div className="space-y-3">
                                {renderLimitInput(
                                    'Max Payload Size (MB)', 'data', 'max_payload_size_mb',
                                    limits.data?.max_payload_size_mb, globalLimits.data?.max_payload_size_mb,
                                    v => updateData('max_payload_size_mb', v)
                                )}
                            </div>
                        </div>

                        {/* Frequency Card */}
                        <div className="border border-default rounded-lg p-4">
                            <h3 className="text-sm font-semibold text-default mb-1">Frequency</h3>
                            <p className="text-xs text-muted mb-3">
                                Per-minute + burst form a token bucket; per-hour / per-day are
                                fixed-window quotas; cooldown is a penalty lockout.
                            </p>
                            <div className="space-y-3">
                                {renderLimitInput(
                                    'Max Calls / Minute', 'frequency', 'max_calls_per_minute',
                                    limits.frequency?.max_calls_per_minute, globalLimits.frequency?.max_calls_per_minute,
                                    v => updateFrequency('max_calls_per_minute', v)
                                )}
                                {renderLimitInput(
                                    'Max Calls / Hour', 'frequency', 'max_calls_per_hour',
                                    limits.frequency?.max_calls_per_hour, globalLimits.frequency?.max_calls_per_hour,
                                    v => updateFrequency('max_calls_per_hour', v)
                                )}
                                {renderLimitInput(
                                    'Max Calls / Day', 'frequency', 'max_calls_per_day',
                                    limits.frequency?.max_calls_per_day, globalLimits.frequency?.max_calls_per_day,
                                    v => updateFrequency('max_calls_per_day', v)
                                )}
                                {renderLimitInput(
                                    'Burst Limit', 'frequency', 'burst_limit',
                                    limits.frequency?.burst_limit, globalLimits.frequency?.burst_limit,
                                    v => updateFrequency('burst_limit', v)
                                )}
                                {renderLimitInput(
                                    'Cooldown (seconds)', 'frequency', 'cooldown_seconds',
                                    limits.frequency?.cooldown_seconds, globalLimits.frequency?.cooldown_seconds,
                                    v => updateFrequency('cooldown_seconds', v)
                                )}
                            </div>
                        </div>
                    </div>
                    </>
                )}

                {(problem || saveError) && (
                    <div className="mx-6 mb-2 p-3 bg-surface-alt border border-default rounded-lg text-sm text-red-800">
                        {problem ? <p data-testid="mcp-limits-problem">{problem}</p> : <p>{saveError}</p>}
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
                            disabled={saving || problem !== null}
                            title={problem ?? undefined}
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

export default MCPServerLimitsDialog;
