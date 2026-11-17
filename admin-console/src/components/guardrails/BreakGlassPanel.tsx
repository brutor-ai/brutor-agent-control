/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrail break-glass.
 *
 * The escape hatch at the top of the guardrail resilience ladder. When a
 * detector provider is genuinely down and availability outranks the control,
 * an operator declares it here and checks degrade to log-and-allow.
 *
 * The runtime effect is identical to the hardcoded fail-open every competing
 * gateway ships. What makes it acceptable is that it is **attributed**,
 * **explained**, and **expiring** — so this panel renders all three, loudly.
 * A break-glass banner without a visible countdown is how a one-hour incident
 * action quietly becomes the permanent configuration.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Loader2, ShieldOff, ShieldCheck, History } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import { useConfirm } from '../common/ConfirmationDialog';
import type { BreakGlassState } from '../../lib/guardrailTypes';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

const MIN_REASON = 10;

function formatRemaining(seconds: number): string {
    if (seconds <= 0) return 'expired';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m`;
    return `${seconds}s`;
}

const BreakGlassPanel: React.FC = () => {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const confirm = useConfirm();

    const [state, setState] = useState<BreakGlassState | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [showForm, setShowForm] = useState(false);
    const [reason, setReason] = useState('');
    const [minutes, setMinutes] = useState<string>('');
    const [readFailure, reads] = useReadFailures<'state'>();

    const load = useCallback(async () => {
        try {
            setState(await gatewayClient.fetchGuardrailBreakGlass());
            reads.loaded('state');
        } catch (err) {
            // A failed read must not render as "no break-glass" — that would
            // imply enforcement is on when the page does not know. The
            // previous state stays, marked as stale, with a Retry.
            reads.failed('state', 'Failed to load the break-glass state', err);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, reads]);

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    // Tick while a window is live so the countdown is honest rather than
    // frozen at whatever it read when the page loaded.
    useEffect(() => {
        if (!state?.active) return;
        const t = setInterval(() => runBackground(load(), 'timer'), 30_000);
        return () => clearInterval(t);
    }, [state?.active, load]);

    const activate = async () => {
        setBusy(true);
        try {
            await gatewayClient.activateGuardrailBreakGlass(
                reason.trim(),
                minutes ? Number(minutes) : undefined,
            );
            toast.warning('Break-glass active — guardrail checks are now log-and-allow');
            setShowForm(false);
            setReason('');
            setMinutes('');
            await load();
        } catch (e: unknown) {
            toast.error(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const deactivate = async () => {
        const ok = await confirm({
            title: 'Restore guardrail enforcement?',
            message: 'Checks will start blocking again immediately.',
            confirmLabel: 'Restore enforcement',
            variant: 'warning',
        });
        if (!ok) return;
        setBusy(true);
        try {
            await gatewayClient.deactivateGuardrailBreakGlass();
            toast.success('Guardrail enforcement restored');
            await load();
        } catch (e: unknown) {
            toast.error(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    if (loading) {
        return (
            <div className="flex items-center gap-2 text-sm text-muted" data-testid="bg-loading">
                <Loader2 className="w-4 h-4 animate-spin" /> Checking break-glass…
            </div>
        );
    }
    const notice = <ReadFailureNotice failure={readFailure('state')} onRetry={() => runBackground(load(), 'load')} />;
    if (!state) return notice;

    // --- ACTIVE: this must be impossible to miss ---------------------------
    if (state.active && state.current) {
        const c = state.current;
        return (
            <div
                className="rounded-xl border-2 border-red-400 bg-red-50 p-4"
                data-testid="break-glass-active"
            >
                {notice}
                <div className="flex items-start gap-3">
                    <ShieldOff className="w-6 h-6 text-red-600 shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <div className="text-sm font-bold text-red-900">
                            Break-glass active — guardrail checks are NOT blocking
                        </div>
                        <p className="mt-1 text-sm text-red-900">
                            Traffic is passing with checks degraded to log-and-allow.
                            Every affected call is recorded in the audit trail and disclosed
                            in the Assurance Report.
                        </p>
                        <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-red-900 sm:grid-cols-3">
                            <div>
                                <dt className="font-semibold">Declared by</dt>
                                <dd data-testid="bg-who">{c.activated_by}</dd>
                            </div>
                            <div>
                                <dt className="font-semibold">Expires in</dt>
                                <dd className="tabular-nums" data-testid="bg-remaining">
                                    {formatRemaining(c.remaining_seconds)}
                                </dd>
                            </div>
                            <div className="sm:col-span-1">
                                <dt className="font-semibold">Reason</dt>
                                <dd data-testid="bg-reason">{c.reason}</dd>
                            </div>
                        </dl>
                    </div>
                    <button
                        type="button"
                        onClick={asyncHandler(deactivate, 'click')}
                        disabled={busy}
                        className="shrink-0 rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
                    >
                        Restore enforcement
                    </button>
                </div>
            </div>
        );
    }

    // --- INACTIVE ----------------------------------------------------------
    return (
        <div className="rounded-xl border border-default bg-surface p-4" data-testid="break-glass-idle">
            {notice}
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-3">
                    <ShieldCheck className="w-5 h-5 text-green-600 shrink-0 mt-0.5" />
                    <div>
                        <div className="text-sm font-semibold text-default">
                            Guardrail enforcement active
                        </div>
                        <p className="mt-0.5 text-xs text-muted">
                            Checks that cannot run refuse the call. If a detector provider is
                            down and availability must win, declare a break-glass — it is
                            recorded, attributed, and expires on its own.
                        </p>
                    </div>
                </div>
                {!showForm && (
                    <button
                        type="button"
                        onClick={() => setShowForm(true)}
                        className="shrink-0 rounded-md border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50"
                        data-testid="bg-declare"
                    >
                        Declare outage…
                    </button>
                )}
            </div>

            {showForm && (
                <div className="mt-4 space-y-3 border-t border-default pt-4" data-testid="bg-form">
                    <div className="flex items-start gap-2 rounded-md bg-amber-50 p-2.5 text-xs text-amber-900">
                        <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                        <span>
                            This lets unchecked traffic through for the whole window. Prefer the
                            shortest duration that covers the incident — it can be renewed.
                        </span>
                    </div>
                    <label className="block">
                        <span className="text-xs font-medium text-secondary">
                            Reason (required, min {MIN_REASON} characters)
                        </span>
                        <textarea
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            rows={2}
                            placeholder="e.g. Presidio returning 503s fleet-wide — incident INC-4471"
                            className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                            data-testid="bg-reason-input"
                        />
                    </label>
                    <label className="block">
                        <span className="text-xs font-medium text-secondary">
                            Duration in minutes (default {state.default_minutes}, max{' '}
                            {state.max_hours * 60})
                        </span>
                        <input
                            type="number"
                            min={1}
                            value={minutes}
                            onChange={(e) => setMinutes(e.target.value)}
                            placeholder={String(state.default_minutes)}
                            className="mt-1 w-40 rounded-md border border-default bg-surface px-2 py-1.5 text-sm tabular-nums"
                            data-testid="bg-minutes-input"
                        />
                    </label>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={asyncHandler(activate, 'click')}
                            disabled={busy || reason.trim().length < MIN_REASON}
                            className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
                            data-testid="bg-confirm"
                        >
                            Declare outage
                        </button>
                        <button
                            type="button"
                            onClick={() => setShowForm(false)}
                            className="rounded-md border border-default px-3 py-1.5 text-sm text-secondary"
                        >
                            Cancel
                        </button>
                    </div>
                </div>
            )}

            {state.history.length > 0 && (
                <details className="mt-4 border-t border-default pt-3" data-testid="bg-history">
                    <summary className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-secondary">
                        <History className="w-3.5 h-3.5" />
                        Past break-glass windows ({state.history.length})
                    </summary>
                    <ul className="mt-2 space-y-1.5 text-xs text-muted">
                        {state.history.map((wnd) => (
                            <li key={wnd.id}>
                                <span className="font-medium text-secondary">
                                    {wnd.activated_by}
                                </span>{' '}
                                — {wnd.reason}
                                {wnd.activated_at && (
                                    <span className="ml-1 text-muted">
                                        ({new Date(wnd.activated_at).toLocaleString()})
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </div>
    );
};

export default BreakGlassPanel;
