/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Top-level tab for managing A2A Agent Cards.
 *
 * Layout:
 *   - Master: list of cards for the current tenant + quick actions.
 *   - Detail panel: expands on click, shows card_json, signature state,
 *     capabilities with handler bindings, and lifecycle
 *     actions (sign, revoke, delete).
 *   - Header: "Tenant key" action opens a modal showing the public key
 *     so external verifiers can check signatures.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import { createPortal } from 'react-dom';
import {
    Activity,
    AlertCircle,
    Check,
    ChevronLeft,
    Copy,
    Download,
    Eye,
    FileSignature,
    HelpCircle,
    IdCard,
    KeyRound,
    Link2,
    Loader2,
    MoreVertical,
    Pencil,
    Plus,
    RefreshCw,
    ShieldOff,
    Trash2,
    XCircle,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import InfoBanner from '../common/InfoBanner';
import { useToast } from '../common/Toast';
import { useConfirm } from '../common/ConfirmationDialog';
import { env } from '../../env';
import type {
    AgentCardDetailResponse,
    AgentCardListItem,
    AgentCardListResponse,
    TenantA2APublicKeyResponse,
} from '../../lib/a2aTypes';
import type { GroupHierarchyTree } from '../../lib/groupTypes';
import AgentCardCreateDialog from './AgentCardCreateDialog';
import AgentCardEditDialog from './AgentCardEditDialog';
import AgentCardEndpointModal from './AgentCardEndpointModal';
import AgentCardImportModal, { CardIcon } from './AgentCardImportModal';
import GroupTreeDisplay from '../common/GroupTreeDisplay';
import { formatDate, trustTierBadge } from './a2aFormatters';
import { errorMessage } from '../../lib/narrow';
import { useReadFailures } from '../../hooks/useRead';
import { asyncHandler, runBackground, runUserAction } from '../../lib/asyncSafe';

type ViewMode = 'list' | 'detail';
/** Edit dialog target — only ever an existing card. The "Add" path
 *  goes through `creating` instead, which opens the URL-first
 *  AgentCardCreateDialog (manual JSON editing isn't a thing without
 *  a working remote endpoint). */
type EditTarget = null | AgentCardDetailResponse;

const AgentCardsTab: React.FC = () => {
    const { gatewayClient } = useAuth();
    const toast = useToast();
    const confirm = useConfirm();

    const [cards, setCards] = useState<AgentCardListItem[]>([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [enabledOnly, setEnabledOnly] = useState(false);

    const [view, setView] = useState<ViewMode>('list');
    const [activeCard, setActiveCard] = useState<AgentCardDetailResponse | null>(null);
    const [editing, setEditing] = useState<EditTarget>(null);
    const [tenantKey, setTenantKey] = useState<TenantA2APublicKeyResponse | null>(null);
    const [tenantKeyOpen, setTenantKeyOpen] = useState(false);
    const [tenantKeyLoading, setTenantKeyLoading] = useState(false);
    const [importOpen, setImportOpen] = useState(false);
    const [creating, setCreating] = useState(false);
    const [endpointTarget, setEndpointTarget] = useState<AgentCardListItem | null>(null);
    const [healthChecking, setHealthChecking] = useState(false);

    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [groupsLoading, setGroupsLoading] = useState(false);

    // Lazy-load the group tree when the admin first opens an edit
    // panel so the list view doesn't pay the cost when it isn't needed.
    // (The Create dialog is URL-first and doesn't show the group
    // selector — bindings are configured via Edit after the card
    // exists.)
    //
    // Guarded by a ref, not by `groupTree.length`: a tenant with no groups, or a
    // failed fetch, leaves the tree empty, and an effect keyed on the tree and
    // the loading flag would refetch in a tight loop for as long as the view is open.
    const groupsRequested = useRef(false);
    const [groupsAsked, setGroupsAsked] = useState(0);
    const [readFailure, reads] = useReadFailures<'groups'>();
    useEffect(() => {
        if (view === 'list' && editing === null) return;
        if (groupsRequested.current) return;
        groupsRequested.current = true;
        setGroupsLoading(true);
        gatewayClient
            .fetchGroupHierarchyTree()
            .then((resp) => {
                setGroupTree(resp.tree ?? []);
                reads.loaded('groups');
            })
            .catch((err: unknown) => {
                // Let the next open, or Retry, ask again.
                groupsRequested.current = false;
                reads.failed('groups', 'Failed to load the resource groups', err);
            })
            .finally(() => setGroupsLoading(false));
    }, [view, editing, gatewayClient, groupsAsked, reads]);

    const load = useCallback(async () => {
        setError(null);
        try {
            const res: AgentCardListResponse = await gatewayClient.listA2AAgentCards({
                enabled_only: enabledOnly || undefined,
            });
            setCards(res.cards);
        } catch (e) {
            setError(errorMessage(e) ?? 'Failed to load agent cards');
        }
    }, [gatewayClient, enabledOnly]);

    useEffect(() => {
        setLoading(true);
        runBackground(load().finally(() => setLoading(false)), 'load');
    }, [load]);

    async function reload() {
        setRefreshing(true);
        try {
            await load();
        } finally {
            setRefreshing(false);
        }
    }

    async function runBulkHealthCheck() {
        setHealthChecking(true);
        try {
            const result = await gatewayClient.bulkHealthCheckA2AAgentCards();
            await load();
            if (result.total === 0) {
                toast.info('No cards to check', 'There are no agent cards to probe.');
            } else {
                toast.success(
                    'Health check complete',
                    `${result.healthy} healthy, ${result.unhealthy} unhealthy, ${result.unknown} unknown`,
                );
            }
        } catch (e) {
            toast.error('Health check failed', errorMessage(e) ?? 'Unknown error');
        } finally {
            setHealthChecking(false);
        }
    }

    async function openDetail(card: AgentCardListItem) {
        setView('detail');
        setActiveCard(null);
        try {
            const detail = await gatewayClient.getA2AAgentCard(card.id);
            setActiveCard(detail);
        } catch (e) {
            toast.error('Load failed', errorMessage(e) ?? 'Failed to load card');
            setView('list');
        }
    }

    function goBackToList() {
        setView('list');
        setActiveCard(null);
    }

    // Shared post-save refresh: re-pull the list and re-pull the
    // detail so open UI reflects the write.
    async function handleDialogSaved() {
        const wasEditing = editing;
        setEditing(null);
        await load();
        if (wasEditing && activeCard) {
            try {
                const detail = await gatewayClient.getA2AAgentCard(activeCard.id);
                setActiveCard(detail);
            } catch {
                // If the card vanished (e.g. admin deleted it in a
                // different tab), just drop back to the list.
                goBackToList();
            }
        }
        toast.success('Card updated');
    }

    // After the URL-first Create dialog persists a card, jump straight
    // into its detail view — that's where Sign / Resource Group / Edit
    // live. Saves the operator a second click.
    async function handleCreated(detail: AgentCardDetailResponse) {
        setCreating(false);
        await load();
        setActiveCard(detail);
        setView('detail');
    }

    type CardLike = Pick<AgentCardListItem, 'id' | 'name' | 'version'>;

    async function handleSign(card: CardLike) {
        const ok = await confirm({
            title: 'Sign agent card',
            message: `Sign ${card.name}@${card.version} with the tenant\u2019s Ed25519 key?`,
            confirmLabel: 'Sign',
            variant: 'info',
        });
        if (!ok) return;
        try {
            const updated = await gatewayClient.signA2AAgentCard(card.id);
            toast.success('Card signed');
            if (activeCard?.id === card.id) setActiveCard(updated);
            await load();
        } catch (e) {
            toast.error('Sign failed', errorMessage(e) ?? 'Failed to sign card');
        }
    }

    async function handleRevoke(card: CardLike) {
        const reason = window.prompt('Revocation reason (required):', '');
        if (!reason?.trim()) return;
        const ok = await confirm({
            title: 'Revoke agent card',
            message: `Revoke ${card.name}? This is terminal \u2014 create a new card to replace it.`,
            confirmLabel: 'Revoke',
            variant: 'danger',
        });
        if (!ok) return;
        try {
            const updated = await gatewayClient.revokeA2AAgentCard(card.id, { reason: reason.trim() });
            toast.success('Card revoked');
            if (activeCard?.id === card.id) setActiveCard(updated);
            await load();
        } catch (e) {
            toast.error('Revoke failed', errorMessage(e) ?? 'Failed to revoke card');
        }
    }

    async function handleDelete(card: CardLike) {
        const ok = await confirm({
            title: 'Delete agent card',
            message: `Permanently delete ${card.name}? Capabilities will cascade. Prefer Revoke to keep the audit trail.`,
            confirmLabel: 'Delete',
            variant: 'danger',
        });
        if (!ok) return;
        try {
            await gatewayClient.deleteA2AAgentCard(card.id);
            toast.success('Card deleted');
            if (activeCard?.id === card.id) goBackToList();
            await load();
        } catch (e) {
            toast.error('Delete failed', errorMessage(e) ?? 'Failed to delete card');
        }
    }

    async function handleEdit(card: CardLike) {
        // Edit dialog needs the full detail (capabilities, signature,
        // card_json, group bindings). Lazy-load on demand so the row
        // menu doesn't pre-fetch detail for cards the operator never
        // touches.
        try {
            const detail = await gatewayClient.getA2AAgentCard(card.id);
            setEditing(detail);
        } catch (e) {
            toast.error('Load failed', errorMessage(e) ?? 'Failed to load card');
        }
    }

    async function handleRowHealthCheck(card: CardLike) {
        try {
            const result = await gatewayClient.healthCheckA2AAgentCard(card.id);
            await load();
            const detail = result.error
                ? `${result.status} \u2014 ${result.error}`
                : result.status;
            toast.success('Health check complete', `${card.name}: ${detail}`);
        } catch (e) {
            toast.error('Health check failed', errorMessage(e) ?? 'Unknown error');
        }
    }

    async function openTenantKey() {
        setTenantKeyOpen(true);
        if (tenantKey) return;
        setTenantKeyLoading(true);
        try {
            const resp = await gatewayClient.getA2ATenantPublicKey();
            setTenantKey(resp);
        } catch (e) {
            toast.error('Load failed', errorMessage(e) ?? 'Failed to load tenant key');
            setTenantKeyOpen(false);
        } finally {
            setTenantKeyLoading(false);
        }
    }

    const totalEnabled = cards.filter(c => c.enabled).length;

    return (
        <div className="p-6 space-y-6">
            {/* ============ HEADER ============ */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className={`p-2.5 rounded-xl ${totalEnabled > 0 ? 'bg-primary-100' : 'bg-surface-raised'}`}>
                        <IdCard className={`w-7 h-7 ${totalEnabled > 0 ? 'text-primary-600' : 'text-muted'}`} />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">Agent Cards</h1>
                        <p className="text-sm text-muted">
                            {cards.length === 0
                                ? 'Publishable A2A agent descriptors for inbound delegation'
                                : `${cards.length} card${cards.length !== 1 ? 's' : ''}, ${totalEnabled} enabled`
                            }
                        </p>
                    </div>
                </div>

                <div className="flex items-center space-x-3">
                    <button
                        onClick={asyncHandler(reload, 'click')}
                        disabled={refreshing}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                        title="Refresh"
                    >
                        <RefreshCw className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                        onClick={asyncHandler(runBulkHealthCheck, 'click')}
                        disabled={healthChecking || cards.length === 0}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Probe each card's interface URL and update its health status"
                    >
                        {healthChecking ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Activity className="w-4 h-4" />
                        )}
                        <span>{healthChecking ? 'Checking…' : 'Health Check'}</span>
                    </button>
                    <button
                        onClick={asyncHandler(openTenantKey, 'click')}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 transition-colors"
                    >
                        <KeyRound className="w-4 h-4" />
                        <span>Tenant Public Key</span>
                    </button>
                    <button
                        onClick={() => setImportOpen(true)}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                    >
                        <Download className="w-4 h-4" />
                        <span>Import</span>
                    </button>
                    <button
                        onClick={() => setCreating(true)}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                    >
                        <Plus className="w-4 h-4" />
                        <span>New Card</span>
                    </button>
                </div>
            </div>

            {/* ============ INFO BOX ============ */}
            <InfoBanner title="How Agent Cards work">
                <p>
                    An <strong>Agent Card</strong> is the A2A v0.3 descriptor other agents
                    read to discover what your agent can do and how to call it. The
                    gateway stores the card JSON verbatim, extracts each declared{' '}
                    <strong>capability</strong> into an indexed row for policy lookups, and
                    publishes the card at{' '}
                    <code>.well-known/agent-card/&lt;tenant&gt;/&lt;name&gt;.json</code>{' '}
                    for external discovery. Bind each capability to a local{' '}
                    <strong>handler</strong> (a skill, or <code>noop</code> for a
                    non-executing acknowledgement) so inbound calls dispatch correctly.{' '}
                    <strong>Sign</strong> a card with this tenant's Ed25519 key to
                    move it from <em>unsigned</em> to <em>tenant_signed</em> — only
                    signed cards are trusted under production policy. Verifiers use the
                    tenant public key (top-right) to validate signatures offline.{' '}
                    <strong>Revocation</strong> is terminal; replace a revoked card by
                    creating a new one.
                </p>
            </InfoBanner>

            {error && (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            )}

            {/* ============ DETAIL ============ */}
            {view === 'detail' && (
                <div className="bg-surface border border-default rounded-lg shadow-sm">
                    <div className="flex items-center justify-between border-b border-default px-5 py-3">
                        <button
                            onClick={goBackToList}
                            className="flex items-center space-x-1 text-sm text-secondary hover:text-default transition-colors"
                        >
                            <ChevronLeft className="w-4 h-4" />
                            <span>Back to list</span>
                        </button>
                        {activeCard && (
                            <div className="flex items-center space-x-2">
                                <button
                                    onClick={() => setEndpointTarget(activeCard)}
                                    className="flex items-center space-x-1.5 px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                                    title="Show A2A proxy URLs for this card"
                                >
                                    <Link2 className="w-4 h-4" />
                                    <span>Endpoint</span>
                                </button>
                                <button
                                    onClick={() => setEditing(activeCard)}
                                    className="flex items-center space-x-1.5 px-3 py-1.5 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 transition-colors"
                                >
                                    <Pencil className="w-4 h-4" />
                                    <span>Edit</span>
                                </button>
                                {activeCard.trust_tier !== 'revoked' && (
                                    <button
                                        onClick={asyncHandler(() => handleSign(activeCard), 'click')}
                                        className="flex items-center space-x-1.5 px-3 py-1.5 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 transition-colors"
                                    >
                                        <FileSignature className="w-4 h-4" />
                                        <span>Sign</span>
                                    </button>
                                )}
                                {activeCard.trust_tier !== 'revoked' && (
                                    <button
                                        onClick={asyncHandler(() => handleRevoke(activeCard), 'click')}
                                        className="flex items-center space-x-1.5 px-3 py-1.5 text-sm font-medium text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors"
                                    >
                                        <XCircle className="w-4 h-4" />
                                        <span>Revoke</span>
                                    </button>
                                )}
                                <button
                                    onClick={asyncHandler(() => handleDelete(activeCard), 'click')}
                                    className="flex items-center space-x-1.5 px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                                >
                                    <Trash2 className="w-4 h-4" />
                                    <span>Delete</span>
                                </button>
                            </div>
                        )}
                    </div>

                    <div className="p-5">
                        {!activeCard ? (
                            <div className="flex h-24 items-center justify-center text-secondary">
                                <Loader2 className="w-4 h-4 animate-spin mr-2" />
                                Loading card…
                            </div>
                        ) : (
                            <AgentCardDetailSection card={activeCard} />
                        )}
                    </div>
                </div>
            )}

            {/* ============ LIST ============ */}
            {view === 'list' && (
                <>
                    {loading ? (
                        <div className="flex items-center justify-center py-16">
                            <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                        </div>
                    ) : cards.length === 0 && !enabledOnly ? (
                        <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                            <IdCard className="w-10 h-10 text-muted mx-auto mb-3" />
                            <h3 className="text-base font-semibold text-default mb-1">No agent cards yet</h3>
                            <p className="text-sm text-muted max-w-md mx-auto">
                                Create a card to describe an agent you want to expose via A2A.
                                External agents can then discover it at{' '}
                                <code className="text-xs">.well-known/agent-card/&lt;tenant&gt;/&lt;name&gt;.json</code>.
                            </p>
                        </div>
                    ) : (
                        <div className="bg-surface border border-default rounded-lg shadow-sm overflow-hidden">
                            <div className="flex items-center justify-between border-b border-default px-5 py-3">
                                <span className="text-sm text-muted">
                                    {cards.length} card{cards.length === 1 ? '' : 's'}
                                </span>
                                <label className="inline-flex items-center space-x-2 text-sm text-secondary">
                                    <input
                                        type="checkbox"
                                        checked={enabledOnly}
                                        onChange={e => setEnabledOnly(e.target.checked)}
                                        className="h-4 w-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                                    />
                                    <span>Enabled only</span>
                                </label>
                            </div>
                            <div className="overflow-x-auto">
                                <table className="min-w-full divide-y divide-default">
                                    <thead className="bg-surface-alt">
                                        <tr>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Card
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Status
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Health
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Trust
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Capabilities
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Groups
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                                Updated
                                            </th>
                                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                                                Actions
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody className="bg-surface divide-y divide-default">
                                        {cards.map(card => {
                                            const tier = trustTierBadge(card.trust_tier);
                                            return (
                                                <tr
                                                    key={card.id}
                                                    className="hover:bg-surface-alt transition-colors"
                                                >
                                                    {/* Card */}
                                                    <td className="px-6 py-4">
                                                        <div className="flex items-start gap-3 min-w-0">
                                                            <CardIcon
                                                                src={card.icon_url}
                                                                alt={card.name}
                                                                size={36}
                                                            />
                                                            <div className="min-w-0">
                                                                <div className="flex items-center gap-2">
                                                                    <span className="font-medium text-default truncate">
                                                                        {card.name}
                                                                    </span>
                                                                    <span className="text-xs text-muted">
                                                                        v{card.version}
                                                                    </span>
                                                                </div>
                                                                <button
                                                                    type="button"
                                                                    onClick={asyncHandler(async () => {
                                                                        try {
                                                                            await navigator.clipboard.writeText(card.id);
                                                                            toast.success('Card ID copied');
                                                                        } catch {
                                                                            toast.error('Copy failed', 'Could not write to clipboard');
                                                                        }
                                                                    }, 'click')}
                                                                    title="Click to copy card ID"
                                                                    className="mt-0.5 inline-flex items-center gap-1 font-mono text-[10px] text-muted hover:text-default hover:bg-surface-alt px-1 py-0.5 rounded transition-colors max-w-md"
                                                                >
                                                                    <Copy className="w-3 h-3 flex-shrink-0" />
                                                                    <span className="truncate">{card.id}</span>
                                                                </button>
                                                                <p className="mt-0.5 text-xs text-secondary line-clamp-2 max-w-md">
                                                                    {card.description ?? '\u2014'}
                                                                </p>
                                                            </div>
                                                        </div>
                                                    </td>
                                                    {/* Status */}
                                                    <td className="px-6 py-4 whitespace-nowrap">
                                                        {card.enabled ? (
                                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-50 text-green-700 border border-green-200">
                                                                Enabled
                                                            </span>
                                                        ) : (
                                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-muted border border-default">
                                                                Disabled
                                                            </span>
                                                        )}
                                                    </td>
                                                    {/* Health */}
                                                    <td className="px-6 py-4 whitespace-nowrap">
                                                        <HealthBadge
                                                            status={card.health_status ?? 'unknown'}
                                                            error={card.health_check_error}
                                                            checkedAt={card.last_health_check_at}
                                                        />
                                                    </td>
                                                    {/* Trust */}
                                                    <td className="px-6 py-4 whitespace-nowrap">
                                                        <span
                                                            className={`px-2 py-0.5 rounded text-xs ${tier.bg} ${tier.text}`}
                                                            title={tier.title}
                                                        >
                                                            {tier.label}
                                                        </span>
                                                    </td>
                                                    {/* Capabilities */}
                                                    <td className="px-6 py-4 whitespace-nowrap text-xs text-secondary">
                                                        {card.capability_count}
                                                    </td>
                                                    {/* Groups */}
                                                    <td className="px-6 py-4">
                                                        <GroupTreeDisplay
                                                            groups={(card.groups || []).map(g => ({
                                                                id: g.id,
                                                                name: g.name,
                                                                display_name: g.display_name ?? undefined,
                                                            }))}
                                                            variant="compact"
                                                            maxVisible={2}
                                                        />
                                                    </td>
                                                    {/* Updated */}
                                                    <td className="px-6 py-4 whitespace-nowrap text-xs text-muted">
                                                        {formatDate(card.updated_at)}
                                                    </td>
                                                    {/* Actions */}
                                                    <td className="px-6 py-4 whitespace-nowrap text-right">
                                                        <RowActionMenu
                                                            card={card}
                                                            onView={asyncHandler(() => openDetail(card), 'view')}
                                                            onEdit={asyncHandler(() => handleEdit(card), 'edit')}
                                                            onHealthCheck={asyncHandler(() => handleRowHealthCheck(card), 'health check')}
                                                            onEndpoint={() => setEndpointTarget(card)}
                                                            onSign={asyncHandler(() => handleSign(card), 'sign')}
                                                            onRevoke={asyncHandler(() => handleRevoke(card), 'revoke')}
                                                            onDelete={asyncHandler(() => handleDelete(card), 'delete')}
                                                        />
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    )}
                </>
            )}

            {tenantKeyOpen && (
                <TenantKeyModal
                    loading={tenantKeyLoading}
                    data={tenantKey}
                    onClose={() => setTenantKeyOpen(false)}
                />
            )}

            {editing !== null && (
                <AgentCardEditDialog
                    initial={editing}
                    groupTree={groupTree}
                    groupsLoading={groupsLoading}
                    groupsFailure={readFailure('groups')}
                    onRetryGroups={() => setGroupsAsked((n) => n + 1)}
                    onClose={() => setEditing(null)}
                    onSaved={asyncHandler(handleDialogSaved, 'saved')}
                />
            )}

            {creating && (
                <AgentCardCreateDialog
                    onClose={() => setCreating(false)}
                    onCreated={asyncHandler(handleCreated, 'created')}
                />
            )}

            {importOpen && (
                <AgentCardImportModal
                    onClose={() => setImportOpen(false)}
                    onImported={() => {
                        // Background reload — modal stays open so the
                        // operator can see per-row results, then closes
                        // it themselves with the footer button.
                        runBackground(load(), 'load');
                    }}
                />
            )}

            {endpointTarget && (
                <AgentCardEndpointModal
                    cardName={endpointTarget.name}
                    tenantId={endpointTarget.tenant_id}
                    gatewayBaseUrl={env.BRUTOR_GATEWAY_PUBLIC_URL}
                    onClose={() => setEndpointTarget(null)}
                />
            )}
        </div>
    );
};

export default AgentCardsTab;

// ────────────────────────────────────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────────────────────────────────────

interface RowActionMenuProps {
    card: AgentCardListItem;
    onView: () => void;
    onEdit: () => void;
    onHealthCheck: () => void;
    onEndpoint: () => void;
    onSign: () => void;
    onRevoke: () => void;
    onDelete: () => void;
}

const RowActionMenu: React.FC<RowActionMenuProps> = ({
    card,
    onView,
    onEdit,
    onHealthCheck,
    onEndpoint,
    onSign,
    onRevoke,
    onDelete,
}) => {
    const [open, setOpen] = useState(false);
    const [pos, setPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
    const buttonRef = useRef<HTMLButtonElement>(null);
    const menuRef = useRef<HTMLDivElement>(null);

    const positionMenu = useCallback(() => {
        if (!buttonRef.current) return;
        const rect = buttonRef.current.getBoundingClientRect();
        const menuHeight = 280;
        const flipUp = window.innerHeight - rect.bottom < menuHeight;
        setPos({
            top: flipUp ? rect.top - menuHeight - 4 : rect.bottom + 4,
            left: rect.right - 192,
        });
    }, []);

    useEffect(() => {
        if (!open) return;
        const onClickOutside = (e: MouseEvent) => {
            if (
                menuRef.current && !menuRef.current.contains(e.target as Node) &&
                buttonRef.current && !buttonRef.current.contains(e.target as Node)
            ) {
                setOpen(false);
            }
        };
        const onScroll = () => setOpen(false);
        document.addEventListener('mousedown', onClickOutside);
        window.addEventListener('scroll', onScroll, true);
        return () => {
            document.removeEventListener('mousedown', onClickOutside);
            window.removeEventListener('scroll', onScroll, true);
        };
    }, [open]);

    const isRevoked = card.trust_tier === 'revoked';
    const canSign = !isRevoked && card.trust_tier === 'unsigned';
    const canRevoke = !isRevoked && card.trust_tier !== 'unsigned';

    const item = (
        Icon: typeof Eye,
        label: string,
        onClick: () => void,
        opts?: { destructive?: boolean; disabled?: boolean },
    ) => (
        <button
            type="button"
            disabled={opts?.disabled}
            onClick={() => { setOpen(false); onClick(); }}
            className={`w-full flex items-center space-x-2 px-4 py-2 text-sm hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed ${
                opts?.destructive ? 'text-red-600 hover:bg-red-50' : 'text-secondary'
            }`}
        >
            <Icon className="w-4 h-4" />
            <span>{label}</span>
        </button>
    );

    return (
        <div className="relative inline-block">
            <button
                ref={buttonRef}
                type="button"
                onClick={() => {
                    if (!open) positionMenu();
                    setOpen(!open);
                }}
                className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                aria-label="Card actions"
            >
                <MoreVertical className="w-4 h-4" />
            </button>
            {open && createPortal(
                <div
                    ref={menuRef}
                    className="w-48 bg-surface rounded-lg shadow-lg border border-default py-1"
                    style={{ position: 'fixed', top: pos.top, left: pos.left, zIndex: 9999 }}
                >
                    {item(Eye, 'View', onView)}
                    {item(Pencil, 'Edit', onEdit, { disabled: isRevoked })}
                    {item(Activity, 'Health Check', onHealthCheck)}
                    {item(Link2, 'Endpoint Info', onEndpoint)}
                    {canSign && item(FileSignature, 'Sign', onSign)}
                    {canRevoke && item(ShieldOff, 'Revoke', onRevoke)}
                    <div className="border-t border-default my-1" />
                    {item(Trash2, 'Delete', onDelete, { destructive: true })}
                </div>,
                document.body,
            )}
        </div>
    );
};

interface HealthBadgeProps {
    status: 'unknown' | 'healthy' | 'unhealthy';
    error?: string | null;
    checkedAt?: string | null;
}

const HealthBadge: React.FC<HealthBadgeProps> = ({ status, error, checkedAt }) => {
    const conf = (() => {
        switch (status) {
            case 'healthy':
                return {
                    bg: 'bg-green-50 text-green-700 border-green-200',
                    Icon: Check,
                    label: 'Healthy',
                };
            case 'unhealthy':
                return {
                    bg: 'bg-red-50 text-red-700 border-red-200',
                    Icon: XCircle,
                    label: 'Unhealthy',
                };
            default:
                return {
                    bg: 'bg-surface-raised text-muted border-default',
                    Icon: HelpCircle,
                    label: 'Unknown',
                };
        }
    })();

    const titleParts: string[] = [];
    if (error) titleParts.push(error);
    if (checkedAt) titleParts.push(`Checked ${formatDate(checkedAt)}`);
    const title = titleParts.join(' · ') || 'Health not yet probed';

    return (
        <span
            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium border ${conf.bg}`}
            title={title}
        >
            <conf.Icon className="w-3 h-3" />
            {conf.label}
        </span>
    );
};

interface DetailProps {
    card: AgentCardDetailResponse;
}

/**
 * Read-only summary of a card. Edits open the modal; there is no inline
 * editing, so there's only one source of truth for a card's shape on-screen.
 */
const AgentCardDetailSection: React.FC<DetailProps> = ({ card }) => {
    const tier = trustTierBadge(card.trust_tier);
    const interfaces = Array.isArray((card.card_json as any)?.supportedInterfaces)
        ? ((card.card_json as any).supportedInterfaces as Array<Record<string, unknown>>)
        : [];
    return (
        <div className="space-y-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-start gap-3">
                    <CardIcon src={card.icon_url} alt={card.name} size={48} />
                    <div>
                        <h3 className="text-lg font-semibold text-default">
                            {card.name}{' '}
                            <span className="text-sm font-normal text-muted">v{card.version}</span>
                        </h3>
                        <p className="text-sm text-secondary">{card.description ?? ''}</p>
                    </div>
                </div>
                <div className="flex flex-col items-end gap-1 text-xs text-muted">
                    <span
                        className={`px-2 py-0.5 rounded ${tier.bg} ${tier.text}`}
                        title={tier.title}
                    >
                        {tier.label}
                    </span>
                    {card.signature_valid === true && (
                        <span className="flex items-center gap-1 text-green-600">
                            <Check className="w-3.5 h-3.5" />
                            <span>Signature verifies</span>
                        </span>
                    )}
                    {card.signature_valid === false && (
                        <span className="flex items-center gap-1 text-red-600">
                            <AlertCircle className="w-3.5 h-3.5" />
                            <span>Signature invalid</span>
                        </span>
                    )}
                    {card.signed_at && <span>Signed {formatDate(card.signed_at)}</span>}
                    {card.revoked_at && (
                        <span className="text-red-600">
                            Revoked {formatDate(card.revoked_at)}
                            {card.revoked_reason ? ` \u2014 ${card.revoked_reason}` : ''}
                        </span>
                    )}
                </div>
            </div>

            <section>
                <h4 className="text-xs font-semibold uppercase text-muted tracking-wider mb-2">
                    Interfaces
                </h4>
                {interfaces.length === 0 ? (
                    <p className="text-sm text-muted">No supportedInterfaces declared.</p>
                ) : (
                    <ul className="space-y-1 text-sm">
                        {interfaces.map((iface, idx) => (
                            <li
                                key={idx}
                                className="flex items-center gap-2 font-mono text-xs bg-surface-alt rounded px-2 py-1"
                            >
                                <span className="text-muted">
                                    {String(iface.protocolBinding ?? 'JSONRPC')} /{' '}
                                    {String(iface.protocolVersion ?? '1.0')}
                                </span>
                                <span className="text-default truncate">
                                    {String(iface.url ?? '')}
                                </span>
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            <section>
                <h4 className="text-xs font-semibold uppercase text-muted tracking-wider mb-2">
                    Capabilities ({card.capabilities.length})
                </h4>
                {card.capabilities.length === 0 ? (
                    <p className="text-sm text-muted">None declared.</p>
                ) : (
                    <ul className="space-y-1 text-sm">
                        {card.capabilities.map(cap => (
                            <li
                                key={cap.id}
                                className="flex items-center justify-between bg-surface-alt rounded px-2 py-1 text-xs"
                            >
                                <span className="font-mono">{cap.capability_name}</span>
                                <span className="text-muted">
                                    {cap.handler_type || 'noop'}
                                    {cap.handler_ref ? ` → ${cap.handler_ref}` : ''}
                                </span>
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            {card.groups && card.groups.length > 0 && (
                <section>
                    <h4 className="text-xs font-semibold uppercase text-muted tracking-wider mb-2">
                        Resource Groups
                    </h4>
                    <ul className="flex flex-wrap gap-1 text-xs">
                        {card.groups.map(g => (
                            <li
                                key={g.id}
                                className="bg-surface-alt rounded px-2 py-1"
                                title={g.id}
                            >
                                {g.display_name || g.name}
                            </li>
                        ))}
                    </ul>
                </section>
            )}
        </div>
    );
};

interface TenantKeyModalProps {
    loading: boolean;
    data: TenantA2APublicKeyResponse | null;
    onClose: () => void;
}

const TenantKeyModal: React.FC<TenantKeyModalProps> = ({ loading, data, onClose }) => {
    const [copied, setCopied] = useState(false);
    function copy() {
        if (!data) return;
        runUserAction(navigator.clipboard?.writeText(data.public_key).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        }), 'write text');
    }
    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            onClick={onClose}
        >
            <div
                className="w-full max-w-lg bg-surface border border-default rounded-xl shadow-xl"
                onClick={e => e.stopPropagation()}
            >
                <div className="border-b border-default px-5 py-4 flex items-center space-x-2">
                    <KeyRound className="w-5 h-5 text-primary-600" />
                    <h3 className="text-base font-semibold text-default">
                        Tenant A2A public key
                    </h3>
                </div>
                <div className="p-5 space-y-4">
                    <p className="text-sm text-secondary">
                        External verifiers use this Ed25519 public key to check signatures on
                        signed agent cards.
                    </p>
                    {loading && (
                        <div className="flex items-center text-secondary">
                            <Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading…
                        </div>
                    )}
                    {data && (
                        <>
                            <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1.5 text-sm">
                                <dt className="text-muted">Tenant</dt>
                                <dd className="text-default font-mono">{data.tenant_id}</dd>
                                <dt className="text-muted">Algorithm</dt>
                                <dd className="text-default">{data.key_algorithm}</dd>
                                <dt className="text-muted">Created</dt>
                                <dd className="text-default">{formatDate(data.created_at)}</dd>
                                {data.rotated_at && (
                                    <>
                                        <dt className="text-muted">Rotated</dt>
                                        <dd className="text-default">{formatDate(data.rotated_at)}</dd>
                                    </>
                                )}
                            </dl>
                            <div>
                                <label className="block text-xs font-medium text-secondary mb-1">
                                    Public key (base64url)
                                </label>
                                <textarea
                                    readOnly
                                    rows={3}
                                    value={data.public_key}
                                    className="w-full px-3 py-2 rounded-lg border border-strong bg-surface-alt font-mono text-xs text-default"
                                />
                            </div>
                        </>
                    )}
                </div>
                <div className="border-t border-default px-5 py-3 flex justify-end space-x-2">
                    {data && (
                        <button
                            onClick={copy}
                            className="flex items-center space-x-1.5 px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        >
                            <Copy className="w-4 h-4" />
                            <span>{copied ? 'Copied' : 'Copy'}</span>
                        </button>
                    )}
                    <button
                        onClick={onClose}
                        className="px-4 py-1.5 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};
