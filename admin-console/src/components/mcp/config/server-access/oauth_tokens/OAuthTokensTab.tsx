/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuthTokensTab — read-only diagnostic view of user-held OAuth tokens
 * per MCP server. Lets a tenant admin answer "why is this MCP call
 * 403-ing?" without touching the DB.
 *
 * The backend never returns encrypted token material; we rely on the
 * server-computed `status` field to classify each row. The five buckets
 * (active, expired_with_refresh, expired_no_refresh, revoked,
 * idp_unavailable) each map to a color + action the admin should take.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    ShieldCheck,
    Clock,
    AlertTriangle,
    Ban,
    Unplug,
    RefreshCw,
    Search,
    Info,
    Loader,
    Key,
    ShieldOff,
} from 'lucide-react';
import { useGateway } from '../../../../../hooks/useGateway';
import { useConfirm } from '../../../../common/ConfirmationDialog';
import { useToast } from '../../../../common/Toast';
import { OAuthTokenInfo, OAuthTokenStatus, OAuthTokensSummary } from '../../../../../lib/groupTypes';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../../../../ui/tableStyles';
import { errorMessage, groupedReasons } from '../../../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../../../lib/asyncSafe';

// ---------------------------------------------------------------------------
// Status → UI mapping. Kept in one place so a typo in a Tailwind class
// doesn't drift across the table + summary bar.
// ---------------------------------------------------------------------------

interface StatusVisual {
    label: string;
    pillClass: string;   // bg + text chip
    iconClass: string;   // icon color
    Icon: React.FC<{ className?: string }>;
    /** One-line "what the operator should do". Shown under the row status. */
    guidance: string;
}

const STATUS_VISUAL: Record<OAuthTokenStatus, StatusVisual> = {
    active: {
        label: 'Active',
        pillClass: 'bg-green-100 text-green-800',
        iconClass: 'text-green-600',
        Icon: ShieldCheck,
        guidance: 'Token is valid and will be used on the next call.',
    },
    expired_with_refresh: {
        label: 'Expired (will refresh)',
        pillClass: 'bg-yellow-100 text-yellow-800',
        iconClass: 'text-yellow-600',
        Icon: Clock,
        guidance:
            'Access token expired but a refresh token is stored. The proxy will attempt silent refresh on the next MCP call.',
    },
    expired_no_refresh: {
        label: 'Expired — re-auth required',
        pillClass: 'bg-red-100 text-red-800',
        iconClass: 'text-red-600',
        Icon: AlertTriangle,
        guidance:
            'Access token expired with no refresh token stored. The user must reconnect this MCP server from the portal.',
    },
    revoked: {
        label: 'Revoked',
        pillClass: 'bg-red-100 text-red-800',
        iconClass: 'text-red-600',
        Icon: Ban,
        guidance:
            'Token was revoked — either by the user, admin, or the IdP. User must reconnect to restore access.',
    },
    idp_unavailable: {
        label: 'IdP unavailable',
        pillClass: 'bg-orange-100 text-orange-800',
        iconClass: 'text-orange-600',
        Icon: Unplug,
        guidance:
            'The Identity Provider is missing or disabled. Re-enable the IdP before this token can be refreshed.',
    },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatRelative(iso: string | null): string {
    if (!iso) return '—';
    const ts = Date.parse(iso);
    if (Number.isNaN(ts)) return iso;
    const secs = Math.round((ts - Date.now()) / 1000);
    const abs = Math.abs(secs);
    const fmt = (n: number, unit: string) => `${n}${unit}`;
    let rel: string;
    if (abs < 60) rel = fmt(abs, 's');
    else if (abs < 3600) rel = fmt(Math.round(abs / 60), 'm');
    else if (abs < 86400) rel = fmt(Math.round(abs / 3600), 'h');
    else rel = fmt(Math.round(abs / 86400), 'd');
    return secs >= 0 ? `in ${rel}` : `${rel} ago`;
}

function formatAbsolute(iso: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.valueOf()) ? iso : d.toLocaleString();
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

const OAuthTokensTab: React.FC = () => {
    const { fetchOAuthTokens, revokeOAuthToken, revokeOAuthTokensBulk } = useGateway();
    const confirm = useConfirm();
    const toast = useToast();

    const [tokens, setTokens] = useState<OAuthTokenInfo[]>([]);
    const [summary, setSummary] = useState<OAuthTokensSummary>({
        total: 0,
        active: 0,
        expired_with_refresh: 0,
        expired_no_refresh: 0,
        revoked: 0,
        idp_unavailable: 0,
    });
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [statusFilter, setStatusFilter] = useState<OAuthTokenStatus | 'all'>('all');
    const [searchTerm, setSearchTerm] = useState('');

    const load = useCallback(async (manual: boolean = false) => {
        if (manual) setRefreshing(true);
        else setLoading(true);
        setError(null);
        try {
            const params =
                statusFilter === 'all' ? {} : { status: statusFilter };
            const res = await fetchOAuthTokens(params);
            setTokens(res.tokens || []);
            setSummary(res.summary || {
                total: 0,
                active: 0,
                expired_with_refresh: 0,
                expired_no_refresh: 0,
                revoked: 0,
                idp_unavailable: 0,
            });
        } catch (e) {
            setError(errorMessage(e, 'Failed to load OAuth tokens'));
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, [fetchOAuthTokens, statusFilter]);

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    // Client-side search against the already-fetched set (avoids a
    // round-trip per keystroke). Backend supports filters too, but for
    // this table the usable cardinality is tens, maybe low hundreds —
    // doing it in memory is fine and snappier.
    const visibleTokens = useMemo(() => {
        if (!searchTerm.trim()) return tokens;
        const q = searchTerm.trim().toLowerCase();
        return tokens.filter(t =>
            (t.mcp_server_name || t.mcp_server_id || '').toLowerCase().includes(q)
            || (t.idp_name || t.idp_id || '').toLowerCase().includes(q)
            || (t.gateway_username || '').toLowerCase().includes(q)
            || t.idp_user_id.toLowerCase().includes(q)
            || (t.scopes || '').toLowerCase().includes(q),
        );
    }, [tokens, searchTerm]);

    // ---- Revoke handlers ----
    //
    // `revokingIds` lets us disable the per-row button + show a
    // spinner while the async revoke is in flight. Multiple rows may
    // be revoking at once from rapid clicks, so it's a Set not a
    // single id.
    const [revokingIds, setRevokingIds] = useState<Set<number>>(new Set());
    const [bulkBusy, setBulkBusy] = useState(false);

    const setRowBusy = (id: number, busy: boolean) => {
        setRevokingIds(prev => {
            const next = new Set(prev);
            if (busy) next.add(id);
            else next.delete(id);
            return next;
        });
    };

    const handleRevokeOne = useCallback(async (token: OAuthTokenInfo) => {
        const label =
            token.mcp_server_name
                ? `${token.mcp_server_name} (${token.gateway_username || token.idp_user_id})`
                : token.mcp_server_id;
        const ok = await confirm({
            title: 'Revoke this OAuth token?',
            message: `The user's access to "${label}" will be cut off immediately. Their next MCP call will prompt them to re-authenticate.`,
            note:
                'This destroys both the access and refresh tokens held by the gateway and (best-effort) '
                + 'calls the IdP\u2019s revocation endpoint to invalidate copies of the token at the source. '
                + 'Not reversible without a fresh OAuth flow.',
            confirmLabel: 'Revoke token',
            variant: 'danger',
        });
        if (!ok) return;
        setRowBusy(token.id, true);
        try {
            const res = await revokeOAuthToken(token.id);
            const failures = res.results.filter(r => !r.idp_revoked);
            if (failures.length === 0) {
                toast.success('Token revoked', `Gateway and IdP revocation both succeeded for ${label}.`);
            } else {
                // DB revocation still succeeded — the token is dead from
                // the proxy's perspective. Downgrade to warning rather
                // than error so the operator knows action was taken.
                toast.warning(
                    'Token revoked locally',
                    `${label} revoked on the gateway; IdP revocation failed: ${failures[0].idp_error || 'unknown'}.`,
                );
            }
            await load(true);
        } catch (e) {
            toast.error('Revoke failed', errorMessage(e, 'Could not revoke token.'));
        } finally {
            setRowBusy(token.id, false);
        }
    }, [confirm, revokeOAuthToken, toast, load]);

    /**
     * Bulk revoke reuses whatever filters are currently active — the
     * backend accepts the same predicate shape. Confirmation shows the
     * count of rows that *will* be affected, derived from what the
     * user already sees in the table. If the operator cleared the
     * status filter they get "revoke every active token" — we call
     * that out in the confirmation.
     */
    const handleBulkRevoke = useCallback(async () => {
        // Target the visible set. If the user has typed a search term
        // the backend won't see it (we do search client-side) — so we
        // only bulk-revoke when the narrowing is a status filter,
        // which the backend can honor. If a search term is active,
        // we fall through to a rows-based loop.
        const targetsByFilter = searchTerm.trim().length === 0;
        const targetCount = visibleTokens.filter(t => t.status !== 'revoked').length;

        if (targetCount === 0) {
            toast.info('Nothing to revoke', 'All visible tokens are already revoked.');
            return;
        }

        const scopeLabel =
            statusFilter === 'all'
                ? 'every non-revoked token in this tenant'
                : `every token currently in "${statusFilter.replace(/_/g, ' ')}"`;

        const ok = await confirm({
            title: `Revoke ${targetCount} OAuth token${targetCount === 1 ? '' : 's'}?`,
            message:
                `You are about to revoke ${scopeLabel}. Each affected user will be prompted to re-authenticate on their next MCP call.`,
            note:
                'This destroys access + refresh tokens held by the gateway and (best-effort) calls the IdP\u2019s '
                + 'revocation endpoint for every token in the set. Not reversible without a fresh OAuth flow.',
            confirmLabel: `Revoke ${targetCount} token${targetCount === 1 ? '' : 's'}`,
            variant: 'danger',
        });
        if (!ok) return;

        setBulkBusy(true);
        try {
            let summary: { revoked_count: number; idp_failures: number; failures?: unknown[] };
            if (targetsByFilter) {
                const res = await revokeOAuthTokensBulk({
                    status_filter: statusFilter === 'all' ? undefined : statusFilter,
                });
                summary = {
                    revoked_count: res.revoked_count,
                    idp_failures: res.results.filter(r => !r.idp_revoked).length,
                };
            } else {
                // Search is active — fall back to per-row revoke for
                // the client-filtered set. Sequential to avoid
                // hammering the IdP; the dialog warned of scope so
                // users won't be surprised by the wait.
                let revoked = 0;
                let idpFailures = 0;
                const failures: unknown[] = [];
                for (const t of visibleTokens) {
                    if (t.status === 'revoked') continue;
                    try {
                        const r = await revokeOAuthToken(t.id);
                        revoked += r.revoked_count;
                        idpFailures += r.results.filter(x => !x.idp_revoked).length;
                    } catch (err) {
                        // Keep going — the whole-batch failure case
                        // would strand the operator. Kept, because these
                        // tokens are still live and they must be told why.
                        failures.push(err);
                    }
                }
                summary = { revoked_count: revoked, idp_failures: idpFailures, failures };
            }

            const failed = summary.failures?.length ?? 0;
            if (failed > 0) {
                toast.error(
                    'Bulk revoke incomplete',
                    `${failed} token${failed === 1 ? ' is' : 's are'} still active: revocation failed (${groupedReasons(summary.failures ?? [])}). ${summary.revoked_count} revoked.`,
                );
            } else if (summary.idp_failures === 0) {
                toast.success(
                    'Bulk revoke complete',
                    `Revoked ${summary.revoked_count} token${summary.revoked_count === 1 ? '' : 's'} (gateway + IdP).`,
                );
            } else {
                toast.warning(
                    'Bulk revoke complete with IdP errors',
                    `${summary.revoked_count} revoked locally; ${summary.idp_failures} IdP revocation${summary.idp_failures === 1 ? '' : 's'} failed (tokens are still dead from our proxy).`,
                );
            }
            await load(true);
        } catch (e) {
            toast.error('Bulk revoke failed', errorMessage(e, 'Operation could not complete.'));
        } finally {
            setBulkBusy(false);
        }
    }, [
        confirm,
        load,
        revokeOAuthToken,
        revokeOAuthTokensBulk,
        searchTerm,
        statusFilter,
        toast,
        visibleTokens,
    ]);

    // ---- Render helpers ----

    const renderStatusPill = (s: OAuthTokenStatus) => {
        const v = STATUS_VISUAL[s];
        const Icon = v.Icon;
        return (
            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${v.pillClass}`}>
                <Icon className={`w-3 h-3 ${v.iconClass}`} />
                {v.label}
            </span>
        );
    };

    const renderSummaryCard = (
        bucket: OAuthTokenStatus,
        count: number,
    ) => {
        const v = STATUS_VISUAL[bucket];
        const Icon = v.Icon;
        const active = statusFilter === bucket;
        return (
            <button
                key={bucket}
                onClick={() => setStatusFilter(active ? 'all' : bucket)}
                className={`flex items-center gap-3 p-3 rounded-lg border transition-all text-left ${
                    active
                        ? 'border-primary-500 bg-primary-50 shadow-sm'
                        : 'border-default bg-surface hover:bg-surface-alt'
                }`}
                title={active ? 'Clear filter' : `Show only ${v.label}`}
            >
                <Icon className={`w-5 h-5 ${v.iconClass}`} />
                <div className="flex-1 min-w-0 min-h-0">
                    <div className="text-lg font-semibold text-default">{count}</div>
                    <div className="text-xs text-muted truncate">{v.label}</div>
                </div>
            </button>
        );
    };

    // ---- Render ----

    return (
        <div className="p-6 space-y-5">
            {/* Heading */}
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-xl font-bold text-default flex items-center gap-2">
                        <Key className="w-5 h-5 text-primary-600" />
                        OAuth Tokens
                    </h2>
                    <p className="text-sm text-muted mt-1 max-w-3xl">
                        User-held OAuth tokens for MCP servers in this tenant.
                        Encrypted token material is never shown — each row is
                        classified by status so you can tell at a glance whether
                        a failing MCP call is due to an expired token, a missing
                        refresh, a revocation, or an unavailable IdP.
                    </p>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                    {/* Bulk-revoke respects the currently-applied status
                        and search filters. Disabled when there is
                        nothing visible to revoke — avoids the
                        "accidentally revoke everything" footgun when
                        the table is empty due to a narrow filter. */}
                    {visibleTokens.some(t => t.status !== 'revoked') && (
                        <button
                            onClick={asyncHandler(handleBulkRevoke, 'click')}
                            disabled={bulkBusy || loading || refreshing}
                            className="inline-flex items-center gap-2 px-3 py-2 text-sm text-white bg-red-600 hover:bg-red-700 rounded-md disabled:opacity-50 transition-colors"
                            title="Revoke every token matching the current filters"
                        >
                            <ShieldOff className="w-4 h-4" />
                            {bulkBusy
                                ? 'Revoking…'
                                : `Revoke ${visibleTokens.filter(t => t.status !== 'revoked').length}`}
                        </button>
                    )}
                    <button
                        onClick={asyncHandler(() => load(true), 'click')}
                        disabled={refreshing || loading}
                        className="inline-flex items-center gap-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt disabled:opacity-50 transition-colors"
                    >
                        <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>
            </div>

            {/* Summary row — clickable filters */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                {renderSummaryCard('active', summary.active)}
                {renderSummaryCard('expired_with_refresh', summary.expired_with_refresh)}
                {renderSummaryCard('expired_no_refresh', summary.expired_no_refresh)}
                {renderSummaryCard('revoked', summary.revoked)}
                {renderSummaryCard('idp_unavailable', summary.idp_unavailable)}
            </div>

            {/* Filters */}
            <div className="flex items-center gap-3">
                <div className="relative flex-1 max-w-md min-h-0 min-w-0">
                    <Search className="w-4 h-4 absolute left-3 top-1/2 transform -translate-y-1/2 text-muted" />
                    <input
                        type="text"
                        placeholder="Search by MCP server, IdP, user..."
                        value={searchTerm}
                        onChange={e => setSearchTerm(e.target.value)}
                        className="w-full pl-9 pr-3 py-2 border border-strong bg-surface rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                    />
                </div>
                {statusFilter !== 'all' && (
                    <button
                        onClick={() => setStatusFilter('all')}
                        className="text-sm text-primary-600 hover:underline"
                    >
                        Clear status filter
                    </button>
                )}
            </div>

            {/* Body */}
            {loading ? (
                <div className="flex items-center justify-center py-16">
                    <Loader className="w-8 h-8 animate-spin text-primary-600" />
                </div>
            ) : error ? (
                <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-2">
                    <AlertTriangle className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                    <div>
                        <p className="text-sm font-medium text-red-900">Failed to load OAuth tokens</p>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                    </div>
                </div>
            ) : visibleTokens.length === 0 ? (
                <div className="bg-surface rounded-lg border border-default py-16 text-center">
                    <Key className="w-10 h-10 text-muted mx-auto mb-3" />
                    <p className="text-sm text-default font-medium">No OAuth tokens</p>
                    <p className="text-xs text-muted mt-1">
                        {searchTerm || statusFilter !== 'all'
                            ? 'No tokens match the current filters.'
                            : 'No user has authorized an MCP server in this tenant yet.'}
                    </p>
                </div>
            ) : (
                <div className="bg-surface rounded-lg border border-default overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>MCP Server</th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>IdP</th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>User</th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Status</th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Expires</th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Refresh</th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Scopes</th>
                                    <th className={`text-right px-4 py-2 w-20 ${TABLE_TH_TYPE}`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {visibleTokens.map(t => (
                                    <tr
                                        key={t.id}
                                        className="border-b border-default last:border-0 hover:bg-surface-alt/60"
                                    >
                                        <td className="px-4 py-3 align-top">
                                            <div className="font-medium text-default">
                                                {t.mcp_server_name || <span className="text-muted italic">unknown</span>}
                                            </div>
                                            <div className="text-xs text-muted font-mono truncate max-w-[20ch]" title={t.mcp_server_id}>
                                                {t.mcp_server_id}
                                            </div>
                                        </td>
                                        <td className="px-4 py-3 align-top">
                                            <div className="text-default">
                                                {t.idp_name || <span className="text-muted italic">unknown</span>}
                                            </div>
                                            <div className="text-xs text-muted font-mono truncate max-w-[20ch]" title={t.idp_id}>
                                                {t.idp_id}
                                            </div>
                                        </td>
                                        <td className="px-4 py-3 align-top">
                                            <div className="text-default">
                                                {t.gateway_username || t.idp_user_id}
                                            </div>
                                            {t.gateway_username && (
                                                <div className="text-xs text-muted truncate max-w-[20ch]" title={t.idp_user_id}>
                                                    {t.idp_user_id}
                                                </div>
                                            )}
                                        </td>
                                        <td className="px-4 py-3 align-top">
                                            <div className="flex flex-col gap-1">
                                                {renderStatusPill(t.status)}
                                                <div
                                                    className="text-xs text-muted max-w-xs"
                                                    title={t.status_reason}
                                                >
                                                    {t.status_reason}
                                                </div>
                                            </div>
                                        </td>
                                        <td className="px-4 py-3 align-top">
                                            <div className="text-default">{formatRelative(t.expires_at)}</div>
                                            <div className="text-xs text-muted">{formatAbsolute(t.expires_at)}</div>
                                        </td>
                                        <td className="px-4 py-3 align-top">
                                            {t.has_refresh_token ? (
                                                <div className="text-default text-xs">
                                                    <span className="inline-flex items-center gap-1 text-green-700">
                                                        <ShieldCheck className="w-3 h-3" /> Stored
                                                    </span>
                                                    <div className="text-muted mt-0.5">
                                                        refreshed × {t.refresh_count}
                                                    </div>
                                                    {t.last_refreshed_at && (
                                                        <div className="text-muted">
                                                            last {formatRelative(t.last_refreshed_at)}
                                                        </div>
                                                    )}
                                                </div>
                                            ) : (
                                                <span className="inline-flex items-center gap-1 text-xs text-muted">
                                                    <Info className="w-3 h-3" /> None
                                                </span>
                                            )}
                                        </td>
                                        <td className="px-4 py-3 align-top max-w-xs">
                                            <div
                                                className="text-xs text-secondary truncate"
                                                title={t.scopes || ''}
                                            >
                                                {t.scopes || <span className="text-muted italic">—</span>}
                                            </div>
                                        </td>
                                        <td className="px-4 py-3 align-top text-right">
                                            {t.status === 'revoked' ? (
                                                // Already revoked — show disabled state for clarity.
                                                // A revoked row is immutable from the UI; re-auth
                                                // (by the user) creates a new row, it doesn't
                                                // "un-revoke" this one.
                                                <span className="text-xs text-muted italic">revoked</span>
                                            ) : (
                                                <button
                                                    onClick={asyncHandler(() => handleRevokeOne(t), 'click')}
                                                    disabled={revokingIds.has(t.id) || bulkBusy}
                                                    className="inline-flex items-center gap-1 px-2 py-1 text-xs text-red-700 bg-red-50 hover:bg-red-100 rounded-md disabled:opacity-50 transition-colors"
                                                    title="Revoke this token — user must re-authenticate to restore access"
                                                >
                                                    {revokingIds.has(t.id) ? (
                                                        <Loader className="w-3 h-3 animate-spin" />
                                                    ) : (
                                                        <ShieldOff className="w-3 h-3" />
                                                    )}
                                                    Revoke
                                                </button>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}
        </div>
    );
};

export default OAuthTokensTab;
