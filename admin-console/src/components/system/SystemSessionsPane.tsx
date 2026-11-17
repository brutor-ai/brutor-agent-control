/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Users,
    Globe,
    Key,
    Lock,
    AlertTriangle,
    RefreshCw,
    Ban,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../ui/tableStyles';
import {
    SessionEntry,
    SessionKind,
    SessionListResponse,
    SessionRevokeResponse,
} from '../../lib/systemOpsTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemSessionsPane.
 *
 * Three tabs (Admin / Portal / API keys) each listing live credentials
 * with a per-row Revoke action. The active-tab choice drives a single
 * GET against the backend (`?kind=`) so we never load all three sets
 * at once — operator sessions are high-cardinality on busy tenants
 * and the per-tab fetch keeps the latency budget bounded.
 *
 * Permission gating: the parent SystemConsoleTab already verifies
 * `system:read` before rendering us. The destructive Revoke action
 * additionally gates on `system:operate`. When the operator lacks
 * the permission the button renders disabled with a tooltip — the
 * same shape SystemConsoleTab's placeholder uses, so the
 * defence-in-depth wire is consistent across the console.
 *
 * The revoke confirm modal collects a free-text reason. The reason
 * lands in the audit row payload and (for admin / api_key kinds) on
 * the DB-side reason column too — operators get prompted in the UI
 * because no-reason revokes are pointless at incident-review time.
 *
 * Poll cadence: 30s — long enough that operators can read the
 * column without rows shuffling under them, short enough that a
 * revoke another operator just made is visible by the next refresh.
 * Manual Refresh button bypasses the cadence.
 */

const POLL_INTERVAL_MS = 30_000;
const PERM_OPERATE = 'system:operate';

interface Props {
    /** Hook the parent passes so deep-link navigation stays consistent. */
    onTabChange?: (tab: string) => void;
}

const KIND_TABS: { id: SessionKind; label: string; icon: React.FC<{ className?: string }>; description: string }[] = [
    {
        id: 'admin',
        label: 'Admin',
        icon: Users,
        description:
            'Live tenant-admin sessions. Revoke ends the session and every Control Plane replica stops accepting it immediately.',
    },
    {
        id: 'portal',
        label: 'Portal',
        icon: Globe,
        description:
            'End-user portal sessions, inferred from request activity in the last 24 hours. Revoke puts the session token on the deny list of every gateway replica.',
    },
    {
        id: 'api_key',
        label: 'API keys',
        icon: Key,
        description:
            'API keys that pass authentication right now. Revoke disables the key and every gateway replica stops accepting it before the next request.',
    },
];

const SystemSessionsPane: React.FC<Props> = () => {
    const { gatewayClient } = useGateway();
    const { hasPermission } = useAuth();
    const canOperate = hasPermission(PERM_OPERATE);

    const [activeKind, setActiveKind] = useState<SessionKind>('admin');
    const [data, setData] = useState<SessionListResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);
    const [revokeTarget, setRevokeTarget] = useState<SessionEntry | null>(null);
    const [revokeReason, setRevokeReason] = useState('');
    const [revoking, setRevoking] = useState(false);
    const [revokeError, setRevokeError] = useState<string | null>(null);

    const fetchSessions = useCallback(
        async (kind: SessionKind) => {
            try {
                setLoading(true);
                const resp = await gatewayClient.listSystemSessions(kind);
                setData(resp);
                setError(null);
                setLastFetchedAt(new Date());
            } catch (e) {
                setError(errorMessage(e) || String(e));
            } finally {
                setLoading(false);
            }
        },
        [gatewayClient],
    );

    // Refetch on kind switch + 30s polling per-tab. Visibility check
    // mirrors SystemOverviewPane — background tabs don't poll.
    // `fetchSessions` is intentionally omitted from the deps; its
    // identity flips with `gatewayClient` each render, which would
    // loop the effect.
     
    useEffect(() => {
        runBackground(fetchSessions(activeKind), 'sessions');
        const id = setInterval(() => {
            if (document.visibilityState === 'visible') {
                runBackground(fetchSessions(activeKind), 'sessions');
            }
        }, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, [activeKind]);

    const activeMeta = useMemo(
        () => KIND_TABS.find(t => t.id === activeKind) ?? KIND_TABS[0],
        [activeKind],
    );

    const onConfirmRevoke = useCallback(async () => {
        if (!revokeTarget) return;
        try {
            setRevoking(true);
            setRevokeError(null);
            const resp: SessionRevokeResponse = await gatewayClient.revokeSystemSession({
                kind: revokeTarget.kind,
                id: revokeTarget.id,
                reason: revokeReason || undefined,
            });
            // Optimistic local drop — the next poll would have removed
            // the row anyway, but reflecting it immediately keeps the
            // operator's mental model honest ("the row I clicked is now
            // gone"). The status string is unused but useful in dev tools.
            if (resp.status === 'queued' && data) {
                setData({
                    ...data,
                    items: data.items.filter(it => it.id !== revokeTarget.id),
                });
            }
            setRevokeTarget(null);
            setRevokeReason('');
        } catch (e) {
            setRevokeError(errorMessage(e) || String(e));
        } finally {
            setRevoking(false);
        }
    }, [revokeTarget, revokeReason, gatewayClient, data]);

    return (
        <div className="space-y-4 max-w-6xl">
            {/* ── Kind tabs ── */}
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <div
                    data-testid="system-sessions-kind-tabs"
                    className="inline-flex items-center rounded-md border border-default overflow-hidden"
                >
                    {KIND_TABS.map(({ id, label, icon: Icon }) => {
                        const isActive = id === activeKind;
                        return (
                            <button
                                key={id}
                                type="button"
                                data-testid={`system-sessions-kind-${id}`}
                                onClick={() => setActiveKind(id)}
                                className={`inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium border-r border-default last:border-r-0 ${
                                    isActive
                                        ? 'bg-primary-50 text-primary-700 dark:bg-primary-900/30 dark:text-primary-200'
                                        : 'bg-surface text-secondary hover:bg-surface-muted'
                                }`}
                            >
                                <Icon className="w-4 h-4" />
                                {label}
                            </button>
                        );
                    })}
                </div>
                <button
                    type="button"
                    onClick={asyncHandler(() => fetchSessions(activeKind), 'click')}
                    data-testid="system-sessions-refresh"
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
            </div>

            {/* ── Active tab description ── */}
            <p className="text-xs text-secondary" data-testid="system-sessions-description">
                {activeMeta.description}
            </p>

            {/* ── Error banner ── */}
            {error && (
                <div
                    data-testid="system-sessions-error"
                    className="bg-red-50 border border-red-200 text-red-800 p-3 rounded-md text-sm"
                >
                    <div className="font-semibold">Couldn&apos;t load sessions</div>
                    <div className="mt-1">{error}</div>
                </div>
            )}

            {/* ── Table ── */}
            <div
                data-testid="system-sessions-table"
                className="bg-surface rounded-lg border border-default overflow-hidden"
            >
                <table className="min-w-full text-sm">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Owner</th>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>ID</th>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Last seen</th>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Created</th>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Expires</th>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>IP</th>
                            <th className={`text-right px-3 py-2 ${TABLE_TH_TYPE}`}>Action</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {loading && !data && (
                            <tr>
                                <td colSpan={7} className="px-3 py-4 text-secondary text-center">
                                    Loading…
                                </td>
                            </tr>
                        )}
                        {!loading && data && data.items.length === 0 && (
                            <tr>
                                <td
                                    colSpan={7}
                                    className="px-3 py-4 text-tertiary text-center italic"
                                    data-testid="system-sessions-empty"
                                >
                                    No live {activeMeta.label.toLowerCase()} sessions.
                                </td>
                            </tr>
                        )}
                        {data?.items.map(item => (
                            <tr
                                key={`${item.kind}:${item.id}`}
                                data-testid={`system-sessions-row-${item.id}`}
                            >
                                <td className="px-3 py-2">
                                    {item.user_label || item.user_id || '—'}
                                </td>
                                <td className="px-3 py-2 font-mono text-xs">
                                    {truncateMiddle(item.id)}
                                </td>
                                <td className="px-3 py-2 text-secondary">
                                    {formatTimestamp(item.last_seen_at)}
                                </td>
                                <td className="px-3 py-2 text-secondary">
                                    {formatTimestamp(item.created_at)}
                                </td>
                                <td className="px-3 py-2 text-secondary">
                                    {formatTimestamp(item.expires_at)}
                                </td>
                                <td className="px-3 py-2 text-secondary">
                                    {item.ip || '—'}
                                </td>
                                <td className="px-3 py-2 text-right">
                                    <button
                                        type="button"
                                        disabled={!canOperate}
                                        data-testid={`system-sessions-revoke-${item.id}`}
                                        onClick={() => {
                                            setRevokeTarget(item);
                                            setRevokeReason('');
                                            setRevokeError(null);
                                        }}
                                        title={
                                            canOperate
                                                ? 'Revoke this session'
                                                : `Requires the ${PERM_OPERATE} permission`
                                        }
                                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-md border ${
                                            canOperate
                                                ? 'border-red-300 text-red-700 hover:bg-red-50'
                                                : 'border-default text-tertiary cursor-not-allowed opacity-60'
                                        }`}
                                    >
                                        {canOperate ? (
                                            <Ban className="w-3.5 h-3.5" />
                                        ) : (
                                            <Lock className="w-3.5 h-3.5" />
                                        )}
                                        Revoke
                                    </button>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            <footer className="text-xs text-tertiary pt-2 border-t border-default">
                {data && (
                    <>
                        Generated {formatTimestamp(data.generated_at)}
                        {lastFetchedAt && (
                            <> · client refreshed {lastFetchedAt.toLocaleTimeString()}</>
                        )}
                    </>
                )}
            </footer>

            {/* ── Confirm modal ── */}
            {revokeTarget && (
                <RevokeConfirmModal
                    target={revokeTarget}
                    reason={revokeReason}
                    onReasonChange={setRevokeReason}
                    onCancel={() => {
                        setRevokeTarget(null);
                        setRevokeReason('');
                        setRevokeError(null);
                    }}
                    onConfirm={asyncHandler(onConfirmRevoke, 'confirm')}
                    busy={revoking}
                    error={revokeError}
                />
            )}
        </div>
    );
};

// ─── Confirm modal ──────────────────────────────────────────────────

interface ConfirmProps {
    target: SessionEntry;
    reason: string;
    onReasonChange: (s: string) => void;
    onCancel: () => void;
    onConfirm: () => void;
    busy: boolean;
    error: string | null;
}

const RevokeConfirmModal: React.FC<ConfirmProps> = ({
    target,
    reason,
    onReasonChange,
    onCancel,
    onConfirm,
    busy,
    error,
}) => (
    <div
        data-testid="system-sessions-revoke-modal"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        onClick={onCancel}
    >
        <div
            className="bg-surface rounded-lg shadow-xl border border-default p-5 w-full max-w-md"
            onClick={e => e.stopPropagation()}
        >
            <div className="flex items-start gap-3 mb-4">
                <div className="flex-shrink-0 p-2 rounded-md bg-red-50 dark:bg-red-900/30">
                    <AlertTriangle className="w-5 h-5 text-red-600" />
                </div>
                <div>
                    <h3 className="text-base font-semibold text-default">
                        Revoke {target.kind} session?
                    </h3>
                    <p className="text-sm text-secondary mt-1">
                        This will deactivate the credential and propagate the revoke to
                        every gateway replica. The action is audited.
                    </p>
                </div>
            </div>

            <div className="text-xs text-secondary mb-3">
                <div>
                    <strong>ID:</strong>{' '}
                    <span className="font-mono break-all">{target.id}</span>
                </div>
                {target.user_label && (
                    <div>
                        <strong>Owner:</strong> {target.user_label}
                    </div>
                )}
            </div>

            <label className="block text-sm text-default mb-1">
                Reason
                <span className="text-tertiary"> (recommended)</span>
            </label>
            <textarea
                data-testid="system-sessions-revoke-reason"
                value={reason}
                onChange={e => onReasonChange(e.target.value)}
                rows={3}
                maxLength={4096}
                placeholder="e.g. suspected credential compromise; routine rotation"
                className="w-full px-2 py-1.5 text-sm rounded-md border border-default bg-surface focus:outline-none focus:ring-1 focus:ring-primary-500"
            />

            {error && (
                <div
                    data-testid="system-sessions-revoke-modal-error"
                    className="mt-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-md p-2"
                >
                    {error}
                </div>
            )}

            <div className="flex justify-end gap-2 mt-4">
                <button
                    type="button"
                    onClick={onCancel}
                    disabled={busy}
                    className="px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                >
                    Cancel
                </button>
                <button
                    type="button"
                    data-testid="system-sessions-revoke-confirm"
                    onClick={onConfirm}
                    disabled={busy}
                    className="px-3 py-1.5 text-sm rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-60"
                >
                    {busy ? 'Revoking…' : 'Revoke'}
                </button>
            </div>
        </div>
    </div>
);

// ─── Helpers ────────────────────────────────────────────────────────

function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

/** Compact long ids so the column doesn't dominate the row. */
function truncateMiddle(s: string, head = 10, tail = 6): string {
    if (s.length <= head + tail + 1) return s;
    return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

export default SystemSessionsPane;
