/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    AlertTriangle,
    Wrench,
    PowerOff,
    Power,
    BookOpenCheck,
    Repeat,
    Database,
    Lock,
    AlertCircle,
    CheckCircle2,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import { PERM_MAINTENANCE } from './systemPermissions';
import {
    MaintenanceMode,
    MaintenanceStateResponse,
} from '../../lib/systemOpsTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemMaintenancePane.
 *
 * The most dangerous tab in the System Operations Console. Three
 * destructive surfaces, each gated identically:
 *
 *   1. Maintenance mode toggle (off / read_only / drain / paused).
 *      Flipping to "paused" cuts the tenant off from every request
 *      until an operator flips it back.
 *   2. Rolling restart — audit-only marker today (operators trigger
 *      the actual rollout via kubectl / ECS); the Audit tab shows
 *      who clicked when.
 *   3. Database hygiene — VACUUM ANALYZE on a whitelisted table. Long-
 *      running on large tables; the operator is expected to know
 *      what they're doing.
 *
 * Defence-in-depth:
 *   * Server enforces `system:maintenance:toggle` on every endpoint.
 *   * UI hides the buttons (renders disabled state with Lock icon)
 *     when the permission is missing, so an operator without the
 *     permission sees the page but can't fire anything.
 *   * Every destructive action requires a typed-confirmation in the
 *     modal — the operator types the tenant id verbatim. The
 *     pattern protects against accidental double-clicks (e.g. an
 *     enter-key after the modal opens).
 */

const POLL_INTERVAL_MS = 30_000;

// Whitelist of allowed VACUUM tables — must stay in lockstep with
// ALLOWED_VACUUM_TABLES in app/services/system_ops/maintenance.py.
// The endpoint enforces; this list just feeds the dropdown so the
// operator can't request something that'd 400.
const VACUUMABLE_TABLES = [
    'proxy_logs',
    'usage_logs',
    'system_operation_log',
    'admin_audit_log',
    'proxy_log_request_chunks',
    'proxy_log_response_chunks',
    'tool_approvals',
] as const;

type VacuumableTable = (typeof VACUUMABLE_TABLES)[number];

interface PendingAction {
    /** Identifier for which card opened the modal. */
    kind: 'mode' | 'restart' | 'vacuum';
    /** Mode being set, when kind === 'mode'. */
    nextMode?: MaintenanceMode;
    /** Optional grace seconds override, set on mode === 'drain'. */
    drainGraceSecs?: number;
    /** Selected table, when kind === 'vacuum'. */
    table?: VacuumableTable;
    /** Free-text reason persisted into the audit row. */
    reason: string;
    /** Operator must type the tenant id verbatim to enable the submit. */
    confirmText: string;
}

const SystemMaintenancePane: React.FC = () => {
    const { hasPermission } = useAuth();
    const { gatewayClient } = useGateway();
    const canMaintenance = hasPermission(PERM_MAINTENANCE);

    const [state, setState] = useState<MaintenanceStateResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [pending, setPending] = useState<PendingAction | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [actionError, setActionError] = useState<string | null>(null);
    const [feedback, setFeedback] = useState<string | null>(null);
    // Local override for the drain-grace field on the mode card; kept
    // separate from `state.drain_grace_secs` so editing the field
    // doesn't fight with the polling refresh.
    const [drainGraceInput, setDrainGraceInput] = useState<string>('');

    const fetchState = useCallback(async () => {
        try {
            const resp = await gatewayClient.getMaintenance();
            setState(resp);
            setError(null);
            if (resp.drain_grace_secs != null && drainGraceInput === '') {
                setDrainGraceInput(String(resp.drain_grace_secs));
            }
        } catch (e) {
            setError(errorMessage(e) || String(e));
        } finally {
            setLoading(false);
        }
        // Intentionally exclude drainGraceInput — we only seed it
        // when the field is empty; subsequent operator edits stay
        // sticky across polls.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [gatewayClient]);

    // `fetchState` is intentionally omitted — gatewayClient identity
    // flips each render, which would loop the effect.
     
    useEffect(() => {
        runBackground(fetchState(), 'state');
        const id = setInterval(() => {
            if (document.visibilityState === 'visible') runBackground(fetchState(), 'state');
        }, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, []);

    // ─── Action handlers ─────────────────────────────────────────

    const openModeModal = (nextMode: MaintenanceMode) => {
        const drainGraceSecs =
            nextMode === 'drain' && drainGraceInput
                ? Number(drainGraceInput)
                : undefined;
        setPending({
            kind: 'mode',
            nextMode,
            drainGraceSecs,
            reason: '',
            confirmText: '',
        });
        setActionError(null);
    };

    const openRestartModal = () => {
        setPending({ kind: 'restart', reason: '', confirmText: '' });
        setActionError(null);
    };

    const openVacuumModal = (table: VacuumableTable) => {
        setPending({ kind: 'vacuum', table, reason: '', confirmText: '' });
        setActionError(null);
    };

    const closeModal = () => {
        if (submitting) return;
        setPending(null);
        setActionError(null);
    };

    const onPendingChange = (patch: Partial<PendingAction>) => {
        setPending(prev => (prev ? { ...prev, ...patch } : prev));
    };

    const submit = async () => {
        if (!pending || !state) return;
        // Typed-confirmation gate. The operator must have typed the
        // tenant id verbatim — case-sensitive — before the button
        // unlocks. This is the safety check that protects against a
        // pressing-enter-on-an-open-modal misclick.
        if (pending.confirmText !== state.tenant_id) {
            setActionError(
                `Type the tenant id (${state.tenant_id}) exactly to confirm.`,
            );
            return;
        }

        setSubmitting(true);
        setActionError(null);

        try {
            if (pending.kind === 'mode' && pending.nextMode) {
                const updated = await gatewayClient.patchMaintenance({
                    tenant_id: state.tenant_id,
                    mode: pending.nextMode,
                    drain_grace_secs: pending.drainGraceSecs,
                    reason: pending.reason.trim() || undefined,
                });
                setState(updated);
                setFeedback(`Maintenance mode set to '${pending.nextMode}'.`);
            } else if (pending.kind === 'restart') {
                await gatewayClient.restartRolling({
                    tenant_id: state.tenant_id,
                    reason: pending.reason.trim() || undefined,
                });
                setFeedback(
                    'Rolling restart recorded. Trigger the actual rollout via your deploy tooling.',
                );
            } else if (pending.kind === 'vacuum' && pending.table) {
                const res = await gatewayClient.dbVacuum({
                    table: pending.table,
                    tenant_id: state.tenant_id,
                    reason: pending.reason.trim() || undefined,
                });
                setFeedback(
                    `VACUUM ANALYZE ${pending.table} complete (${Math.round(
                        res.duration_ms / 1000,
                    )}s).`,
                );
            }
            setPending(null);
        } catch (e) {
            setActionError(errorMessage(e) || String(e));
        } finally {
            setSubmitting(false);
        }
    };

    // ─── Render ──────────────────────────────────────────────────

    if (loading && !state) {
        return <div className="text-secondary text-sm">Loading maintenance…</div>;
    }

    if (error && !state) {
        return (
            <div
                data-testid="system-maintenance-error"
                className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-md max-w-2xl"
            >
                <div className="font-semibold">Couldn't load maintenance state</div>
                <div className="text-sm mt-1">{error}</div>
                <button
                    type="button"
                    className="mt-3 px-3 py-1.5 text-sm rounded border border-red-300 hover:bg-red-100"
                    onClick={asyncHandler(fetchState, 'click')}
                >
                    Retry
                </button>
            </div>
        );
    }

    if (!state) return null;

    return (
        <div className="space-y-6 max-w-5xl">
            {/* ── Fat warning banner ────────────────────────────── */}
            <BlastRadiusBanner />

            {feedback && (
                <div
                    data-testid="system-maintenance-feedback"
                    className="bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm p-3 rounded-md flex items-start gap-2"
                >
                    <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0" />
                    <span>{feedback}</span>
                </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <ModeCard
                    state={state}
                    canMaintenance={canMaintenance}
                    drainGraceInput={drainGraceInput}
                    onDrainGraceChange={setDrainGraceInput}
                    onSelect={openModeModal}
                />
                <RestartCard
                    canMaintenance={canMaintenance}
                    onClick={openRestartModal}
                />
                <VacuumCard
                    canMaintenance={canMaintenance}
                    onSelect={openVacuumModal}
                />
            </div>

            {pending && (
                <ConfirmModal
                    pending={pending}
                    tenantId={state.tenant_id}
                    onChange={onPendingChange}
                    onCancel={closeModal}
                    onSubmit={asyncHandler(submit, 'submit')}
                    submitting={submitting}
                    error={actionError}
                />
            )}
        </div>
    );
};

// ─── Banner ───────────────────────────────────────────────────────

const BlastRadiusBanner: React.FC = () => (
    <div
        data-testid="system-maintenance-blast-banner"
        className="bg-red-50 dark:bg-red-900/20 border-l-4 border-red-500 text-red-900 dark:text-red-200 p-4 rounded-md"
    >
        <div className="flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 flex-shrink-0 mt-0.5" />
            <div>
                <h2 className="text-sm font-bold uppercase tracking-wide">
                    High-blast-radius surface
                </h2>
                <p className="text-sm mt-1">
                    Every action on this page affects all live traffic for this
                    tenant. <strong>paused</strong> cuts requests off completely.
                    <strong> drain</strong> rejects new writes and signals
                    in-flight connections to close. <strong>VACUUM</strong> can
                    block writers on a large table for minutes. Confirm the
                    tenant id verbatim before submitting.
                </p>
            </div>
        </div>
    </div>
);

// ─── Mode card ────────────────────────────────────────────────────

interface ModeCardProps {
    state: MaintenanceStateResponse;
    canMaintenance: boolean;
    drainGraceInput: string;
    onDrainGraceChange: (v: string) => void;
    onSelect: (mode: MaintenanceMode) => void;
}

const ModeCard: React.FC<ModeCardProps> = ({
    state,
    canMaintenance,
    drainGraceInput,
    onDrainGraceChange,
    onSelect,
}) => {
    const modes: { mode: MaintenanceMode; label: string; tone: string }[] = [
        { mode: 'off', label: 'Off', tone: 'emerald' },
        { mode: 'read_only', label: 'Read-only', tone: 'amber' },
        { mode: 'drain', label: 'Drain', tone: 'orange' },
        { mode: 'paused', label: 'Paused', tone: 'red' },
    ];

    return (
        <section
            data-testid="system-maintenance-mode-card"
            className="bg-surface border border-default rounded-md p-4 space-y-3"
        >
            <header className="flex items-center gap-2">
                <Wrench className="w-4 h-4 text-secondary" />
                <h3 className="text-base font-semibold text-default">
                    Maintenance mode
                </h3>
                <span
                    data-testid="system-maintenance-current-mode"
                    className={`ml-auto inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${currentModeClass(state.mode)}`}
                >
                    {state.mode}
                </span>
            </header>
            <p className="text-xs text-secondary">
                Off = normal. Read-only = reject POST/PATCH/PUT/DELETE. Drain =
                same as read-only PLUS Connection: close, used during rolling
                restarts. Paused = reject every request (incident response).
            </p>
            <div className="grid grid-cols-2 gap-2">
                {modes.map(m => (
                    <button
                        key={m.mode}
                        type="button"
                        data-testid={`system-maintenance-set-${m.mode}`}
                        onClick={() => onSelect(m.mode)}
                        disabled={!canMaintenance || state.mode === m.mode}
                        title={
                            !canMaintenance
                                ? `Requires the ${PERM_MAINTENANCE} permission`
                                : state.mode === m.mode
                                  ? 'Already in this mode'
                                  : `Switch to ${m.label}`
                        }
                        className={`flex items-center justify-center gap-1.5 px-3 py-2 text-sm rounded-md border ${modeButtonClass(m.tone, state.mode === m.mode, !canMaintenance)}`}
                    >
                        {!canMaintenance && <Lock className="w-3.5 h-3.5" />}
                        {m.mode === 'paused' ? (
                            <PowerOff className="w-3.5 h-3.5" />
                        ) : (
                            <Power className="w-3.5 h-3.5" />
                        )}
                        {m.label}
                    </button>
                ))}
            </div>
            <label className="block">
                <span className="text-xs text-secondary">
                    Drain grace (seconds, 0–3600). Used when switching to drain;
                    becomes Retry-After on rejected writes.
                </span>
                <input
                    data-testid="system-maintenance-drain-grace"
                    type="number"
                    min={0}
                    max={3600}
                    value={drainGraceInput}
                    onChange={e => onDrainGraceChange(e.target.value)}
                    disabled={!canMaintenance}
                    className="mt-1 w-32 text-sm border border-default rounded px-2 py-1 bg-surface"
                    placeholder="e.g. 60"
                />
            </label>
            {state.maintenance_mode_at && (
                <div className="text-[11px] text-tertiary">
                    Last toggled {formatTimestamp(state.maintenance_mode_at)}
                    {state.maintenance_mode_by && (
                        <> by {state.maintenance_mode_by}</>
                    )}
                </div>
            )}
        </section>
    );
};

// ─── Restart card ─────────────────────────────────────────────────

interface RestartCardProps {
    canMaintenance: boolean;
    onClick: () => void;
}

const RestartCard: React.FC<RestartCardProps> = ({
    canMaintenance,
    onClick,
}) => (
    <section
        data-testid="system-maintenance-restart-card"
        className="bg-surface border border-default rounded-md p-4 space-y-3"
    >
        <header className="flex items-center gap-2">
            <Repeat className="w-4 h-4 text-secondary" />
            <h3 className="text-base font-semibold text-default">
                Rolling restart
            </h3>
        </header>
        <p className="text-xs text-secondary">
            Records the restart in the audit trail and announces it to every replica. The
            actual rollout is operator-driven (kubectl rollout, ECS new
            deployment). Use this surface so the Audit tab shows who initiated
            the restart and when.
        </p>
        <button
            type="button"
            data-testid="system-maintenance-restart-trigger"
            onClick={onClick}
            disabled={!canMaintenance}
            title={
                canMaintenance
                    ? 'Record a rolling restart'
                    : `Requires the ${PERM_MAINTENANCE} permission`
            }
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border ${canMaintenance ? 'border-amber-300 text-amber-800 hover:bg-amber-50' : 'border-default text-tertiary bg-surface-muted cursor-not-allowed opacity-60'}`}
        >
            {!canMaintenance && <Lock className="w-4 h-4" />}
            <Repeat className="w-4 h-4" />
            Record rolling restart
        </button>
    </section>
);

// ─── Vacuum card ──────────────────────────────────────────────────

interface VacuumCardProps {
    canMaintenance: boolean;
    onSelect: (table: VacuumableTable) => void;
}

const VacuumCard: React.FC<VacuumCardProps> = ({
    canMaintenance,
    onSelect,
}) => {
    const [table, setTable] = useState<VacuumableTable>(VACUUMABLE_TABLES[0]);
    return (
        <section
            data-testid="system-maintenance-vacuum-card"
            className="bg-surface border border-default rounded-md p-4 space-y-3 lg:col-span-2"
        >
            <header className="flex items-center gap-2">
                <Database className="w-4 h-4 text-secondary" />
                <h3 className="text-base font-semibold text-default">
                    Database hygiene — VACUUM ANALYZE
                </h3>
            </header>
            <p className="text-xs text-secondary">
                Reclaims dead row space and refreshes table statistics on the
                selected table. Long-running on multi-GB tables — Postgres holds
                a ShareUpdateExclusiveLock that blocks DDL but allows reads and
                writes. Only the whitelist of high-churn tables is exposed here;
                others must be vacuumed directly on the database.
            </p>
            <div className="flex items-end gap-2 flex-wrap">
                <label className="block">
                    <span className="text-xs text-secondary">Table</span>
                    <select
                        data-testid="system-maintenance-vacuum-table"
                        value={table}
                        onChange={e => setTable(e.target.value as VacuumableTable)}
                        disabled={!canMaintenance}
                        className="mt-1 text-sm border border-default rounded px-2 py-1 bg-surface"
                    >
                        {VACUUMABLE_TABLES.map(t => (
                            <option key={t} value={t}>
                                {t}
                            </option>
                        ))}
                    </select>
                </label>
                <button
                    type="button"
                    data-testid="system-maintenance-vacuum-submit"
                    onClick={() => onSelect(table)}
                    disabled={!canMaintenance}
                    title={
                        canMaintenance
                            ? `VACUUM ANALYZE ${table}`
                            : `Requires the ${PERM_MAINTENANCE} permission`
                    }
                    className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border ${canMaintenance ? 'border-amber-300 text-amber-800 hover:bg-amber-50' : 'border-default text-tertiary bg-surface-muted cursor-not-allowed opacity-60'}`}
                >
                    {!canMaintenance && <Lock className="w-4 h-4" />}
                    <BookOpenCheck className="w-4 h-4" />
                    Run VACUUM ANALYZE
                </button>
            </div>
        </section>
    );
};

// ─── Confirm modal ────────────────────────────────────────────────

interface ConfirmModalProps {
    pending: PendingAction;
    tenantId: string;
    onChange: (patch: Partial<PendingAction>) => void;
    onCancel: () => void;
    onSubmit: () => void;
    submitting: boolean;
    error: string | null;
}

const ConfirmModal: React.FC<ConfirmModalProps> = ({
    pending,
    tenantId,
    onChange,
    onCancel,
    onSubmit,
    submitting,
    error,
}) => {
    const confirmed = pending.confirmText === tenantId;

    const title = useMemo(() => {
        if (pending.kind === 'mode') {
            return `Switch maintenance mode to '${pending.nextMode}'?`;
        }
        if (pending.kind === 'restart') {
            return 'Record rolling restart?';
        }
        return `VACUUM ANALYZE ${pending.table}?`;
    }, [pending]);

    const description = useMemo(() => {
        if (pending.kind === 'mode' && pending.nextMode === 'paused') {
            return (
                'PAUSED mode rejects EVERY request to this tenant with ' +
                'HTTP 503 — chat, embeddings, MCP calls, portal logins, ' +
                'admin API. End users see "service unavailable". The ' +
                'block lasts until an operator with this same permission ' +
                'toggles the mode back to Off. Use ONLY for incident ' +
                'response. To restore service: come back to this page and ' +
                'click Off (also typed-confirmed).'
            );
        }
        if (pending.kind === 'mode' && pending.nextMode === 'drain') {
            return (
                'DRAIN rejects new POST / PATCH / PUT / DELETE with 503. ' +
                'GET requests still pass. Every response carries ' +
                'Connection: close so HTTP/1.1 keep-alives shut after ' +
                'responding — in-flight requests finish but no new sockets ' +
                'are accepted. The right gate before a rolling restart. ' +
                'Lasts until toggled Off. Click Off to restore writes.'
            );
        }
        if (pending.kind === 'mode' && pending.nextMode === 'read_only') {
            return (
                'READ-ONLY rejects POST / PATCH / PUT / DELETE with 503. ' +
                'GET passes. Use for planned maintenance windows where ' +
                'writes need to stop but discovery / monitoring still ' +
                'works. Lasts until toggled Off. Toggle to Off to ' +
                're-enable writes.'
            );
        }
        if (pending.kind === 'mode' && pending.nextMode === 'off') {
            return (
                'Returns the tenant to normal operation. All HTTP verbs ' +
                'accepted; sockets stay open. Effective within ~1s on ' +
                'every gateway replica.'
            );
        }
        if (pending.kind === 'restart') {
            return (
                'Records the restart in the audit trail and announces it to every replica. ' +
                'NO PROCESSES RESTART from this button — this is the ' +
                'audit-trail surface so the System → Audit feed shows ' +
                'who initiated the rollout. You still need to kick the ' +
                'actual restart via kubectl rollout / ECS new deployment ' +
                'in your own tooling. Operators using this without then ' +
                'running the real restart is a common gotcha.'
            );
        }
        return (
            `Runs VACUUM ANALYZE on ${pending.table}. Reclaims dead ` +
            'row space + refreshes table statistics. Postgres holds a ' +
            'ShareUpdateExclusiveLock for the duration: allows reads + ' +
            'writes, but blocks DDL (CREATE INDEX, ALTER TABLE) on this ' +
            'table until done. On multi-GB tables this can take minutes ' +
            'to hours; the command is not cancellable from this UI once ' +
            'submitted. Run during low-traffic windows for very large ' +
            'tables. Audit row records who, what table, and duration.'
        );
    }, [pending]);

    return (
        <div
            data-testid="system-maintenance-confirm-modal"
            role="dialog"
            aria-modal="true"
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        >
            <div className="bg-surface border border-default rounded-md shadow-lg w-full max-w-lg p-5 space-y-4">
                <div>
                    <h3 className="text-base font-semibold text-default">
                        {title}
                    </h3>
                    <p className="text-xs text-secondary mt-1 leading-relaxed whitespace-pre-line">
                        {description}
                    </p>
                </div>
                <label className="block">
                    <span className="text-xs text-secondary">
                        Type the tenant id (<code>{tenantId}</code>) to confirm
                    </span>
                    <input
                        data-testid="system-maintenance-confirm-tenant"
                        type="text"
                        value={pending.confirmText}
                        onChange={e =>
                            onChange({ confirmText: e.target.value })
                        }
                        disabled={submitting}
                        autoComplete="off"
                        className="mt-1 w-full text-sm border border-default rounded px-2 py-1 bg-surface font-mono"
                        placeholder={tenantId}
                    />
                </label>
                <label className="block">
                    <span className="text-xs text-secondary">
                        Reason (optional — persisted to the audit row)
                    </span>
                    <textarea
                        data-testid="system-maintenance-confirm-reason"
                        value={pending.reason}
                        onChange={e => onChange({ reason: e.target.value })}
                        rows={2}
                        maxLength={4096}
                        disabled={submitting}
                        className="mt-1 w-full text-sm border border-default rounded px-2 py-1 bg-surface"
                        placeholder="e.g. incident #4523 — proxy crash loop"
                    />
                </label>
                {error && (
                    <div
                        data-testid="system-maintenance-confirm-error"
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
                        data-testid="system-maintenance-confirm-cancel"
                        className="px-3 py-1.5 text-sm rounded border border-default hover:bg-surface-muted disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={onSubmit}
                        disabled={submitting || !confirmed}
                        data-testid="system-maintenance-confirm-submit"
                        className="px-3 py-1.5 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
                        title={
                            !confirmed
                                ? `Type ${tenantId} to enable`
                                : 'Submit'
                        }
                    >
                        {submitting ? 'Working…' : 'Confirm'}
                    </button>
                </div>
            </div>
        </div>
    );
};

// ─── Helpers ──────────────────────────────────────────────────────

function currentModeClass(mode: MaintenanceMode): string {
    switch (mode) {
        case 'off':
            return 'bg-emerald-100 text-emerald-800 border border-emerald-200';
        case 'read_only':
            return 'bg-amber-100 text-amber-800 border border-amber-200';
        case 'drain':
            return 'bg-orange-100 text-orange-800 border border-orange-200';
        case 'paused':
            return 'bg-red-100 text-red-800 border border-red-200';
    }
}

function modeButtonClass(
    tone: string,
    selected: boolean,
    locked: boolean,
): string {
    if (locked) {
        return 'border-default text-tertiary bg-surface-muted cursor-not-allowed opacity-60';
    }
    if (selected) {
        return `bg-${tone}-100 text-${tone}-800 border-${tone}-300 cursor-not-allowed opacity-70`;
    }
    return `border-${tone}-300 text-${tone}-800 hover:bg-${tone}-50`;
}

function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

export default SystemMaintenancePane;
