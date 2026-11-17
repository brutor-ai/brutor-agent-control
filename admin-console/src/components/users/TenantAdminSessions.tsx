/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tenant Admin Sessions — list + revoke.
 *
 * Reads from `GET /v1/admin/users/tenant/sessions` (tenant-scoped on
 * the server for tenant admins) and kills a session via
 * `POST /v1/admin/users/tenant/sessions/{id}/revoke`.
 *
 * The backend marks the caller's own session with `is_current=true`
 * so we can both badge it in the table and fire a sterner confirmation
 * before the user revokes themselves mid-click.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
    Activity,
    AlertCircle,
    Check,
    Loader,
    RefreshCw,
    X,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useAuth } from '../../hooks/useAuth';
import { useConfirm } from '../common/ConfirmationDialog';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import { errorMessage } from '../../lib/narrow';
import type { TenantAdminSession } from '../../lib/gatewayApi/adminUsers';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

type Session = TenantAdminSession;

const TenantAdminSessions: React.FC = () => {
    const { gatewayClient } = useGateway();
    const { logout } = useAuth();
    const confirm = useConfirm();

    const [sessions, setSessions] = useState<Session[]>([]);
    const [loading, setLoading] = useState(true);
    const [revokingId, setRevokingId] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [includeInactive, setIncludeInactive] = useState(false);

    const fetchSessions = async (silent = false) => {
        if (!gatewayClient) return;
        if (!silent) setLoading(true);
        setError(null);
        try {
            const resp = await gatewayClient.listTenantAdminSessions(
                includeInactive ? { include_inactive: true } : undefined,
            );
            setSessions(resp?.sessions ?? []);
        } catch (e) {
            setError(errorMessage(e, 'Failed to load sessions'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        runBackground(fetchSessions(), 'sessions');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [includeInactive]);

    const handleRevoke = async (s: Session) => {
        if (!gatewayClient) return;

        // Revoking your own current session signs you out immediately —
        // confirm more sternly than a "someone else's session" revoke.
        const ok = await confirm(
            s.is_current
                ? {
                      title: 'Sign out of this device?',
                      message: (
                          <>
                              This is the session you are currently using. Revoking it will
                              sign you out <strong>immediately</strong> and take you back to
                              the login screen.
                          </>
                      ),
                      note: 'You will need to sign in again to continue.',
                      variant: 'warning',
                      confirmLabel: 'Sign out',
                  }
                : {
                      title: 'Revoke session?',
                      message: (
                          <>
                              Revoke the active session for{' '}
                              <strong>{s.admin_username || s.admin_id}</strong>
                              {s.ip_address ? (
                                  <>
                                      {' '}from <code>{s.ip_address}</code>
                                  </>
                              ) : null}
                              ? The associated JWT will be rejected on its next use.
                          </>
                      ),
                      variant: 'danger',
                      confirmLabel: 'Revoke',
                  },
        );
        if (!ok) return;

        setRevokingId(s.id);
        try {
            await gatewayClient.revokeTenantAdminSession(s.id);
            if (s.is_current) {
                // Bail out to the login screen — the backend just invalidated
                // our own session; any follow-up call would 401 anyway.
                await logout();
                return;
            }
            await fetchSessions(true);
        } catch (e) {
            setError(errorMessage(e, 'Failed to revoke session'));
        } finally {
            setRevokingId(null);
        }
    };

    const visible = useMemo(() => {
        // Sort: current first, then active sessions newest-first, then inactive.
        return [...sessions].sort((a, b) => {
            if (a.is_current && !b.is_current) return -1;
            if (b.is_current && !a.is_current) return 1;
            if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
            const at = a.created_at ? Date.parse(a.created_at) : 0;
            const bt = b.created_at ? Date.parse(b.created_at) : 0;
            return bt - at;
        });
    }, [sessions]);

    return (
        <div className="p-6 space-y-4">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-2xl font-bold text-default flex items-center gap-2">
                        <Activity className="w-6 h-6 text-primary-500" />
                        Tenant Admin Sessions
                    </h2>
                    <p className="text-sm text-secondary mt-1 max-w-2xl">
                        Active sign-ins for admins in this tenant. Revoke any session to
                        invalidate its JWT on the next request — useful when an account is
                        compromised or a device is lost.
                    </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                    <label className="flex items-center gap-2 text-sm text-secondary cursor-pointer">
                        <input
                            type="checkbox"
                            checked={includeInactive}
                            onChange={(e) => setIncludeInactive(e.target.checked)}
                            className="rounded border-strong"
                        />
                        Include inactive
                    </label>
                    <button
                        onClick={asyncHandler(() => fetchSessions(), 'click')}
                        disabled={loading}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-strong rounded-md text-sm hover:bg-surface-alt disabled:opacity-50"
                    >
                        <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200">
                    <AlertCircle className="w-4 h-4 text-red-600 mt-0.5 flex-shrink-0" />
                    <p className="text-sm text-red-700">{error}</p>
                </div>
            )}

            {loading && sessions.length === 0 ? (
                <div className="flex items-center gap-2 text-muted p-6">
                    <Loader className="w-4 h-4 animate-spin" /> Loading sessions…
                </div>
            ) : visible.length === 0 ? (
                <div className="p-6 text-center text-muted border border-default rounded-lg bg-surface">
                    No sessions to show.
                </div>
            ) : (
                <div className="overflow-x-auto border border-default rounded-lg bg-surface">
                    <table className="w-full text-sm">
                        <thead className={`${TABLE_THEAD} text-left`}>
                            <tr>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Admin</th>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>IP</th>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>User Agent</th>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Created</th>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Last Activity</th>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Expires</th>
                                <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Status</th>
                                <th className={`px-4 py-2 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {visible.map((s) => (
                                <tr
                                    key={s.id}
                                    className={`border-t border-default ${
                                        s.is_current ? 'bg-primary-50/40' : ''
                                    }`}
                                >
                                    <td className="px-4 py-2">
                                        <div className="font-medium text-default">
                                            {s.admin_username || s.admin_id}
                                        </div>
                                        {s.admin_email && (
                                            <div className="text-xs text-muted">
                                                {s.admin_email}
                                            </div>
                                        )}
                                    </td>
                                    <td className="px-4 py-2 font-mono text-xs">
                                        {s.ip_address || '—'}
                                    </td>
                                    <td
                                        className="px-4 py-2 text-xs text-secondary max-w-[240px] truncate"
                                        title={s.user_agent || ''}
                                    >
                                        {s.user_agent || '—'}
                                    </td>
                                    <td className="px-4 py-2 text-xs">{fmt(s.created_at)}</td>
                                    <td className="px-4 py-2 text-xs">
                                        {fmt(s.last_activity)}
                                    </td>
                                    <td className="px-4 py-2 text-xs">{fmt(s.expires_at)}</td>
                                    <td className="px-4 py-2">
                                        <StatusBadge session={s} />
                                    </td>
                                    <td className="px-4 py-2 text-right">
                                        {s.is_active ? (
                                            <button
                                                onClick={asyncHandler(() => handleRevoke(s), 'click')}
                                                disabled={revokingId === s.id}
                                                className="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-red-300 text-red-700 hover:bg-red-50 disabled:opacity-50"
                                            >
                                                {revokingId === s.id ? (
                                                    <Loader className="w-3 h-3 animate-spin" />
                                                ) : (
                                                    <X className="w-3 h-3" />
                                                )}
                                                Revoke
                                            </button>
                                        ) : (
                                            <span className="inline-flex items-center gap-1 text-xs text-muted">
                                                <Check className="w-3 h-3" />
                                                Inactive
                                            </span>
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
};

const StatusBadge: React.FC<{ session: Session }> = ({ session: s }) => {
    if (s.is_current) {
        return (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-700 border border-primary-200">
                This device
            </span>
        );
    }
    if (!s.is_active) {
        return (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-surface-alt text-muted border border-default">
                {s.deactivation_reason || 'Inactive'}
            </span>
        );
    }
    if (s.is_expired) {
        return (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-50 text-amber-700 border border-amber-200">
                Expired
            </span>
        );
    }
    return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-50 text-green-700 border border-green-200">
            Active
        </span>
    );
};

const fmt = (iso: string | null): string => {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleString();
    } catch {
        return iso;
    }
};

export default TenantAdminSessions;
