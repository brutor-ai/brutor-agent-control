/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    PlayCircle,
    PauseCircle,
    Zap,
    RefreshCw,
    Lock,
    AlertCircle,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import { PERM_OPERATE } from './systemPermissions';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../ui/tableStyles';
import {
    SchedulerActionRequest,
    SchedulerListResponse,
    SchedulerState,
    SchedulerStatus,
} from '../../lib/systemOpsTypes';
import { errorMessage, getNumber } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemTasksPane.
 *
 * Operator surface for the platform's singleton background loops
 * (KB connector sync, audit export, …). Each
 * row maps to one `KNOWN_SCHEDULERS` constant on the backend.
 *
 * Per-row actions:
 *   * Trigger Now    — fires NOTIFY brutor:scheduler:trigger
 *   * Pause          — upserts paused=TRUE, fires NOTIFY brutor:scheduler:pause
 *   * Resume         — upserts paused=FALSE, fires NOTIFY brutor:scheduler:resume
 *
 * All three are for the installation's operator who holds
 * `system:operate`. A scheduler runs once for every tenant, so the
 * operator is the system administrator where the installation has
 * one, and the tenant administrator where it has none. The session's
 * own answer says which the signed-in administrator is
 * (`/v1/admin/users/me`: `is_installation_operator`). To anyone else
 * the buttons are disabled and say why; the Control Plane refuses the
 * action itself, and its sentence is shown as it words it.
 *
 * Why a confirm modal: schedulers control long-running side effects
 * (KB sync writes documents into the index; audit export ships
 * compliance data to S3). An accidental Trigger Now during business
 * hours could double-process work. The modal collects a free-text
 * reason that lands in the system_operation_log payload so the
 * Audit tab shows WHY the operator did it.
 *
 * Polling cadence: 15s, with the Page Visibility API skip — same
 * pattern as SystemOverviewPane. Background tasks rarely change
 * state in real time; the auto-refresh exists so an in-progress
 * pause from another operator shows up without a manual refresh.
 */

const POLL_INTERVAL_MS = 15_000;

const NEEDS_PERMISSION = 'Requires the system:operate permission';
const NEEDS_SYSTEM_ADMINISTRATOR = 'For the system administrator: a scheduler runs once for the whole installation';

type ActionKind = 'trigger' | 'pause' | 'resume';

interface PendingAction {
    schedulerName: string;
    kind: ActionKind;
}

const SystemTasksPane: React.FC = () => {
    const { hasPermission, user } = useAuth();
    const { gatewayClient } = useGateway();
    // The session says whether the signed-in administrator operates what
    // the installation shares; a session that does not say is not taken
    // for the operator.
    const isInstallationOperator = user?.is_installation_operator === true;
    const whyBlocked = !isInstallationOperator ? NEEDS_SYSTEM_ADMINISTRATOR : !hasPermission(PERM_OPERATE) ? NEEDS_PERMISSION : null;

    const [items, setItems] = useState<SchedulerStatus[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

    const [pending, setPending] = useState<PendingAction | null>(null);
    const [reason, setReason] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [actionError, setActionError] = useState<string | null>(null);
    const [feedback, setFeedback] = useState<string | null>(null);

    const fetchList = useCallback(async () => {
        try {
            const resp: SchedulerListResponse =
                await gatewayClient.listSystemTasks();
            setItems(resp.items);
            setError(null);
            setLastFetchedAt(new Date());
        } catch (e) {
            setError(errorMessage(e) || String(e));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    // `fetchList` is intentionally omitted — gatewayClient identity
    // flips each render, which would loop the effect.
     
    useEffect(() => {
        runBackground(fetchList(), 'list');
        const id = setInterval(() => {
            if (document.visibilityState === 'visible') {
                runBackground(fetchList(), 'list');
            }
        }, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, []);

    // ─── Confirm-modal flow ──────────────────────────────────────

    const openConfirm = (kind: ActionKind, schedulerName: string) => {
        setPending({ kind, schedulerName });
        setReason('');
        setActionError(null);
    };

    const closeConfirm = () => {
        if (submitting) return; // don't close mid-flight
        setPending(null);
        setReason('');
        setActionError(null);
    };

    const submitAction = async () => {
        if (!pending) return;
        setSubmitting(true);
        setActionError(null);
        const body: SchedulerActionRequest = reason.trim()
            ? { scheduler_name: pending.schedulerName, reason: reason.trim() }
            : { scheduler_name: pending.schedulerName };
        try {
            if (pending.kind === 'trigger') {
                await gatewayClient.triggerSystemTask(body);
            } else if (pending.kind === 'pause') {
                await gatewayClient.pauseSystemTask(body);
            } else {
                await gatewayClient.resumeSystemTask(body);
            }
            setFeedback(
                `${actionLabel(pending.kind)} queued for ${pending.schedulerName}.`,
            );
            setPending(null);
            setReason('');
            // Refresh immediately so the operator sees the new badge
            // colour instead of waiting up to 15s for the next poll.
            await fetchList();
        } catch (e) {
            // A 422 is the Control Plane refusing the shape of the request,
            // which the operator did not write and cannot correct: say so,
            // and that nothing changed, before its own words.
            const said = errorMessage(e) || String(e);
            setActionError(
                getNumber(e, 'status') === 422
                    ? `The Control Plane did not accept this request, so ${pending.schedulerName} was not changed. `
                      + 'This is a fault in the Control Plane, not in what you entered: report it to whoever runs this installation. '
                      + `What it answered: ${said}`
                    : said,
            );
        } finally {
            setSubmitting(false);
        }
    };

    // ─── Render ──────────────────────────────────────────────────

    const sorted = useMemo(
        () =>
            [...items].sort((a, b) =>
                a.scheduler_name.localeCompare(b.scheduler_name),
            ),
        [items],
    );

    if (loading && items.length === 0) {
        return <div className="text-secondary text-sm">Loading schedulers…</div>;
    }

    if (error && items.length === 0) {
        return (
            <div
                data-testid="system-tasks-error"
                className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-md max-w-2xl"
            >
                <div className="font-semibold">Couldn't load schedulers</div>
                <div className="text-sm mt-1">{error}</div>
                <button
                    type="button"
                    className="mt-3 px-3 py-1.5 text-sm rounded border border-red-300 hover:bg-red-100"
                    onClick={asyncHandler(fetchList, 'click')}
                >
                    Retry
                </button>
            </div>
        );
    }

    return (
        <div className="space-y-4 max-w-6xl">
            {/* ── Header strip ── */}
            <header className="flex items-center justify-between flex-wrap gap-3">
                <div>
                    <h2 className="text-base font-semibold text-default">
                        Background Tasks
                    </h2>
                    <p className="text-xs text-secondary">
                        Singleton control-plane loops. Trigger fires an
                        out-of-band tick on whichever replica holds the
                        scheduler. Pause/Resume persist across restarts and apply on every replica.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={asyncHandler(fetchList, 'click')}
                    data-testid="system-tasks-refresh"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                    title={
                        lastFetchedAt
                            ? `Last refreshed ${lastFetchedAt.toLocaleTimeString()}`
                            : 'Refresh'
                    }
                >
                    <RefreshCw className="w-4 h-4" />
                    Refresh
                </button>
            </header>

            {!isInstallationOperator && (
                <p data-testid="system-tasks-operator-note" className="text-xs text-secondary">
                    A scheduler runs once for the whole installation, so triggering, pausing and resuming one is for the system administrator.
                </p>
            )}

            {feedback && (
                <div
                    data-testid="system-tasks-feedback"
                    className="bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm p-3 rounded-md"
                >
                    {feedback}
                </div>
            )}

            {/* ── Table ── */}
            <div className="overflow-x-auto border border-default rounded-md">
                <table
                    data-testid="system-tasks-table"
                    className="min-w-full text-sm"
                >
                    <thead className={`${TABLE_THEAD} text-left`}>
                        <tr>
                            <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Scheduler</th>
                            <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>State</th>
                            <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Last run</th>
                            <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Paused at</th>
                            <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Paused by</th>
                            <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Reason</th>
                            <th className={`px-3 py-2 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {sorted.map(row => (
                            <SchedulerRow
                                key={row.scheduler_name}
                                row={row}
                                whyBlocked={whyBlocked}
                                onAction={openConfirm}
                            />
                        ))}
                        {sorted.length === 0 && (
                            <tr>
                                <td
                                    colSpan={7}
                                    className="px-3 py-6 text-center text-tertiary italic"
                                    data-testid="system-tasks-empty"
                                >
                                    No schedulers registered.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>

            <footer className="text-xs text-tertiary">
                {lastFetchedAt && (
                    <>Client refreshed {lastFetchedAt.toLocaleTimeString()}</>
                )}
            </footer>

            {/* ── Confirm modal ── */}
            {pending && (
                <ConfirmModal
                    pending={pending}
                    reason={reason}
                    onReasonChange={setReason}
                    onCancel={closeConfirm}
                    onSubmit={asyncHandler(submitAction, 'submit')}
                    submitting={submitting}
                    error={actionError}
                />
            )}
        </div>
    );
};

// ─── Row component ───────────────────────────────────────────────

interface SchedulerRowProps {
    row: SchedulerStatus;
    /** Why the administrator may not act; null when they may. */
    whyBlocked: string | null;
    onAction: (kind: ActionKind, schedulerName: string) => void;
}

const SchedulerRow: React.FC<SchedulerRowProps> = ({
    row,
    whyBlocked,
    onAction,
}) => {
    return (
        <tr
            data-testid={`system-tasks-row-${row.scheduler_name}`}
        >
            <td className="px-3 py-2 font-mono text-[13px]">
                {row.scheduler_name}
            </td>
            <td className="px-3 py-2">
                <StateBadge state={row.state} />
            </td>
            <td className="px-3 py-2 text-secondary">
                {formatTimestamp(row.last_run_at)}
            </td>
            <td className="px-3 py-2 text-secondary">
                {formatTimestamp(row.paused_at)}
            </td>
            <td className="px-3 py-2 text-secondary">{row.paused_by ?? '—'}</td>
            <td className="px-3 py-2 text-secondary" data-testid={`system-tasks-reason-${row.scheduler_name}`}>
                {row.paused ? (row.pause_reason ?? '—') : '—'}
            </td>
            <td className="px-3 py-2">
                <div className="flex items-center gap-2 justify-end">
                    <RowActionButton
                        kind="trigger"
                        schedulerName={row.scheduler_name}
                        whyBlocked={whyBlocked}
                        onClick={onAction}
                        disabled={false}
                    />
                    {row.paused ? (
                        <RowActionButton
                            kind="resume"
                            schedulerName={row.scheduler_name}
                            whyBlocked={whyBlocked}
                            onClick={onAction}
                            disabled={false}
                        />
                    ) : (
                        <RowActionButton
                            kind="pause"
                            schedulerName={row.scheduler_name}
                            whyBlocked={whyBlocked}
                            onClick={onAction}
                            disabled={false}
                        />
                    )}
                </div>
            </td>
        </tr>
    );
};

// ─── Per-row action button ───────────────────────────────────────

interface RowActionButtonProps {
    kind: ActionKind;
    schedulerName: string;
    /** Why the administrator may not act; null when they may. */
    whyBlocked: string | null;
    disabled: boolean;
    onClick: (kind: ActionKind, schedulerName: string) => void;
}

const RowActionButton: React.FC<RowActionButtonProps> = ({
    kind,
    schedulerName,
    whyBlocked,
    disabled,
    onClick,
}) => {
    const labelMap: Record<ActionKind, string> = {
        trigger: 'Trigger',
        pause: 'Pause',
        resume: 'Resume',
    };
    const iconMap: Record<ActionKind, React.ReactNode> = {
        trigger: <Zap className="w-3.5 h-3.5" />,
        pause: <PauseCircle className="w-3.5 h-3.5" />,
        resume: <PlayCircle className="w-3.5 h-3.5" />,
    };
    const colourMap: Record<ActionKind, string> = {
        trigger:
            'border-default text-default hover:bg-surface-muted',
        pause: 'border-amber-300 text-amber-800 hover:bg-amber-50',
        resume:
            'border-emerald-300 text-emerald-800 hover:bg-emerald-50',
    };

    const canOperate = whyBlocked === null;
    const blocked = !canOperate || disabled;

    return (
        <button
            type="button"
            data-testid={`system-tasks-action-${kind}-${schedulerName}`}
            disabled={blocked}
            onClick={() => onClick(kind, schedulerName)}
            className={`inline-flex items-center gap-1 px-2 py-1 rounded-md border text-xs font-medium ${
                blocked
                    ? 'border-default text-tertiary bg-surface-muted opacity-50 cursor-not-allowed'
                    : colourMap[kind]
            }`}
            title={
                whyBlocked !== null
                    ? whyBlocked
                    : `${labelMap[kind]} ${schedulerName}`
            }
        >
            {!canOperate && <Lock className="w-3 h-3" />}
            {canOperate && iconMap[kind]}
            <span>{labelMap[kind]}</span>
        </button>
    );
};

// ─── State badge ─────────────────────────────────────────────────

const StateBadge: React.FC<{ state: SchedulerState }> = ({ state }) => {
    const styles: Record<SchedulerState, string> = {
        running: 'bg-emerald-100 text-emerald-800 border-emerald-200',
        paused: 'bg-amber-100 text-amber-800 border-amber-200',
        unknown: 'bg-slate-100 text-slate-700 border-slate-200',
    };
    return (
        <span
            data-testid={`system-tasks-state-${state}`}
            className={`inline-block px-2 py-0.5 text-[11px] rounded border ${styles[state]}`}
        >
            {state}
        </span>
    );
};

// ─── Confirm modal ───────────────────────────────────────────────

interface ConfirmModalProps {
    pending: PendingAction;
    reason: string;
    onReasonChange: (v: string) => void;
    onCancel: () => void;
    onSubmit: () => void;
    submitting: boolean;
    error: string | null;
}

const ConfirmModal: React.FC<ConfirmModalProps> = ({
    pending,
    reason,
    onReasonChange,
    onCancel,
    onSubmit,
    submitting,
    error,
}) => {
    return (
        <div
            data-testid="system-tasks-confirm-modal"
            role="dialog"
            aria-modal="true"
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
        >
            <div className="bg-surface border border-default rounded-md shadow-lg w-full max-w-md p-5 space-y-4">
                <div>
                    <h3 className="text-base font-semibold text-default">
                        {actionLabel(pending.kind)} {pending.schedulerName}?
                    </h3>
                    <p className="text-xs text-secondary mt-1">
                        {actionExplanation(pending.kind)} This action writes
                        an entry to the System Audit log.
                    </p>
                </div>
                <label className="block">
                    <span className="text-xs text-secondary">
                        Reason (optional — persisted to the audit row)
                    </span>
                    <textarea
                        data-testid="system-tasks-confirm-reason"
                        value={reason}
                        onChange={e => onReasonChange(e.target.value)}
                        rows={3}
                        maxLength={4096}
                        disabled={submitting}
                        className="mt-1 w-full text-sm border border-default rounded px-2 py-1 bg-surface"
                        placeholder="e.g. backfill after upstream outage"
                    />
                </label>
                {error && (
                    <div
                        data-testid="system-tasks-confirm-error"
                        className="flex items-start gap-2 text-sm text-red-700 bg-red-50 border border-red-200 rounded p-2"
                    >
                        <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                        <span>{error}</span>
                    </div>
                )}
                <div className="flex justify-end gap-2">
                    <button
                        type="button"
                        onClick={onCancel}
                        disabled={submitting}
                        data-testid="system-tasks-confirm-cancel"
                        className="px-3 py-1.5 text-sm rounded border border-default hover:bg-surface-muted disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={onSubmit}
                        disabled={submitting}
                        data-testid="system-tasks-confirm-submit"
                        className="px-3 py-1.5 text-sm rounded bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
                    >
                        {submitting ? 'Working…' : actionLabel(pending.kind)}
                    </button>
                </div>
            </div>
        </div>
    );
};

// ─── Helpers ─────────────────────────────────────────────────────

function actionLabel(kind: ActionKind): string {
    switch (kind) {
        case 'trigger':
            return 'Trigger now';
        case 'pause':
            return 'Pause';
        case 'resume':
            return 'Resume';
    }
}

function actionExplanation(kind: ActionKind): string {
    switch (kind) {
        case 'trigger':
            return 'Fires an immediate out-of-band tick on the replica that holds the scheduler. The scheduler will still run its regular interval afterwards.';
        case 'pause':
            return 'Stops the scheduler from running new ticks on every replica until it is resumed. Work currently in flight is allowed to complete.';
        case 'resume':
            return "Allows the scheduler's next regular tick to proceed on every replica.";
    }
}

function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

export default SystemTasksPane;
