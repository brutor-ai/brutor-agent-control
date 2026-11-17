/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Incident windows.
 *
 * A declared period whose runs are **excluded from judgement but kept as
 * evidence**: baselines, drift, envelope terms, conformance and the health
 * rates do not grade them, and they stay recorded, sealed and exportable.
 * Declared by a `cause_fixed` resolution or directly (a planned
 * maintenance, a provider outage nobody resolved a finding for).
 *
 * One component, two scopes: on an AI System (Signals and Lifecycle tabs)
 * it lists the windows that apply to that system — its own and the
 * tenant-wide ones; under Governance it lists every window in the tenant.
 * Every card says who declared it and why, its category, how many runs it
 * excluded, and whether the declaration is sealed.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CalendarX, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { useGateway } from '../../hooks/useGateway';
import { useAISystemNames } from '../../hooks/useAISystemNames';
import AISystemNamesNotice from './AISystemNamesNotice';
import {
    INCIDENT_RESOURCE_LABELS,
    type IncidentWindow,
    type IncidentWindowCreate,
    type IncidentWindowUpdate,
} from '../../lib/driftResolutionTypes';
import {
    incidentCauseLabel,
    sealText,
    windowDeclaredText,
    windowRangeText,
    windowScopeText,
} from '../../lib/driftResolution';
import { errorMessage } from '../../lib/narrow';
import IncidentWindowDialog from './IncidentWindowDialog';

interface IncidentWindowsPanelProps {
    /** The AI System whose windows to show; omitted = the whole tenant. */
    aiSystemId?: string;
    /** Fired after a declare / edit / withdraw — charts re-shade. */
    onChanged?: () => void;
}

export function IncidentWindowsPanel({ aiSystemId, onChanged }: IncidentWindowsPanelProps) {
    const {
        fetchAISystemIncidentWindows,
        fetchIncidentWindows,
        createIncidentWindow,
        updateIncidentWindow,
        deleteIncidentWindow,
    } = useGateway();

    const [windows, setWindows] = useState<IncidentWindow[] | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [editing, setEditing] = useState<IncidentWindow | 'new' | null>(null);
    const [busy, setBusy] = useState('');

    // The tenant page offers the systems to scope a new window to.
    const systemNames = useAISystemNames(!aiSystemId, 'Failed to load the AI Systems a window can be scoped to');
    const { names } = systemNames;
    const systems = useMemo(
        () =>
            Object.entries(names)
                .map(([id, name]) => ({ id, name }))
                .sort((a, b) => a.name.localeCompare(b.name)),
        [names],
    );

    // Bumped to reload: after a write, or the refresh button.
    const [rev, setRev] = useState(0);
    const load = useCallback(() => {
        setLoading(true);
        setRev((n) => n + 1);
    }, []);

    useEffect(() => {
        let cancelled = false;
        const request = aiSystemId ? fetchAISystemIncidentWindows(aiSystemId) : fetchIncidentWindows();
        request.then(
            (res) => {
                if (cancelled) return;
                setWindows(res.windows);
                setError('');
                setLoading(false);
            },
            (err: unknown) => {
                if (cancelled) return;
                setError(errorMessage(err, 'Failed to load incident windows'));
                setWindows(null);
                setLoading(false);
            },
        );
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, fetchAISystemIncidentWindows, fetchIncidentWindows, rev]);

    /** A write saved but not sealed says so — it is not evidence yet. */
    const sealNotice = (seal: { sealed: boolean; seal_error?: string | null }, what: string) =>
        setNotice(seal.sealed ? '' : `${what} saved, but not sealed (${seal.seal_error ?? 'unknown reason'}).`);

    const afterWrite = () => {
        setEditing(null);
        load();
        onChanged?.();
    };

    const create = async (body: IncidentWindowCreate) => {
        const res = await createIncidentWindow(body, aiSystemId);
        sealNotice(res.seal, 'Window');
        afterWrite();
    };

    const update = async (id: string, body: IncidentWindowUpdate) => {
        const res = await updateIncidentWindow(id, body);
        sealNotice(res.seal, 'Edit');
        afterWrite();
    };

    const withdraw = async (w: IncidentWindow) => {
        if (
            !window.confirm(
                `Withdraw this incident window (${windowRangeText(w)})? Its ${w.runs_excluded} run(s) will be judged again. The withdrawal is sealed.`,
            )
        ) {
            return;
        }
        try {
            setBusy(w.id);
            setError('');
            const res = await deleteIncidentWindow(w.id);
            sealNotice(res.seal, 'Withdrawal');
            load();
            onChanged?.();
        } catch (err: unknown) {
            setError(errorMessage(err, 'Failed to withdraw the window'));
        } finally {
            setBusy('');
        }
    };

    return (
        <div className="space-y-3" data-testid="incident-windows-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <CalendarX className="w-4 h-4 text-muted mt-0.5" />
                    <h3 className="text-sm font-semibold text-default">Incident windows</h3>
                    <HelpTip text="Declared periods whose runs are excluded from judgement — baselines, drift, envelope terms, conformance and health rates do not grade them — but kept as evidence: recorded, sealed and listed in the assurance report. That period is unmeasured, not passed." />
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={() => setEditing('new')}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg"
                        data-testid="iw-declare"
                    >
                        <Plus className="w-4 h-4" />
                        Declare window
                    </button>
                    <button
                        type="button"
                        onClick={load}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                        aria-label="Refresh incident windows"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                </div>
            </div>

            <AISystemNamesNotice names={systemNames} className="block" />
            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}
            {notice && (
                <p className="text-xs text-amber-800" data-testid="iw-seal-notice">
                    {notice}
                </p>
            )}

            {windows && windows.length === 0 && !error && (
                <p className="text-sm text-muted p-4 border border-dashed border-strong rounded-lg" data-testid="iw-empty">
                    No incident window declared{aiSystemId ? ' for this system' : ''} — every run is judged.
                </p>
            )}

            {windows && windows.length > 0 && (
                <ul className="space-y-2" data-testid="iw-list">
                    {windows.map((w) => (
                        <li
                            key={w.id}
                            className="bg-surface border border-default rounded-lg p-3 space-y-1"
                            data-testid={`iw-${w.id}`}
                        >
                            <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0">
                                    <p className="text-sm font-medium text-default" data-testid="iw-range">
                                        {windowRangeText(w)}
                                    </p>
                                    <p className="text-xs text-secondary">
                                        <span className="inline-flex px-1.5 py-0.5 mr-1 rounded bg-surface-subtle text-secondary">
                                            {incidentCauseLabel(w.cause_category)}
                                        </span>
                                        <span data-testid="iw-scope-text">{windowScopeText(w)}</span>
                                        {w.resource_type && (
                                            <span>
                                                {' · '}
                                                {INCIDENT_RESOURCE_LABELS[w.resource_type]}
                                                {w.resource_id ? ` ${w.resource_id}` : ''}
                                            </span>
                                        )}
                                    </p>
                                </div>
                                <div className="flex shrink-0 items-center gap-1">
                                    <button
                                        type="button"
                                        onClick={() => setEditing(w)}
                                        className="rounded p-1 text-muted hover:bg-surface-hover"
                                        aria-label="Edit incident window"
                                        data-testid="iw-edit"
                                    >
                                        <Pencil className="w-4 h-4" />
                                    </button>
                                    <button
                                        type="button"
                                        disabled={busy === w.id}
                                        onClick={() => void withdraw(w)}
                                        className="rounded p-1 text-muted hover:bg-surface-hover disabled:opacity-50"
                                        aria-label="Withdraw incident window"
                                        data-testid="iw-delete"
                                    >
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                            <p className="text-sm text-default" data-testid="iw-reason-text">
                                {w.reason}
                            </p>
                            <p className="text-xs text-muted">
                                <span data-testid="iw-declared">{windowDeclaredText(w)}</span>
                                {' · '}
                                <span data-testid="iw-runs-excluded">{w.runs_excluded} run(s) excluded from judgement</span>
                                {' · '}
                                <span data-testid="iw-sealed" className={w.record_id ? '' : 'text-amber-800'}>
                                    {sealText(w.record_id)}
                                </span>
                            </p>
                        </li>
                    ))}
                </ul>
            )}

            {editing && (
                <IncidentWindowDialog
                    window={editing === 'new' ? null : editing}
                    aiSystemId={aiSystemId}
                    systems={systems}
                    onCreate={create}
                    onUpdate={update}
                    onCancel={() => setEditing(null)}
                />
            )}
        </div>
    );
}

export default IncidentWindowsPanel;
