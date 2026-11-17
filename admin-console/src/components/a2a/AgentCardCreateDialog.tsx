/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * "Add Agent Card" wizard — mirrors the MCP "Add Server" flow.
 *
 * Why URL-first: an Agent Card without a working remote endpoint is
 * indistinguishable from no card at all (we cannot route to it, sign
 * meaningfully, or run policy checks). So like MCP servers, the
 * operator must Connect successfully before the Create button enables.
 *
 * Two phases:
 *   1. **Discover** — POST /v1/admin/agent-cards/discover — fetches
 *      the well-known card, parses skills, returns a preview. No DB
 *      write. Re-clicking Connect re-runs discovery (handy for
 *      "did the upstream just deploy a new skill?").
 *   2. **Create** — POST /v1/admin/agent-cards/import — same URL,
 *      this time persisting. The operator's Name / Description
 *      overrides ride along.
 *
 * Power-user manual JSON authoring (the tabbed editor) is reachable
 * after Create — open the new card and click Edit.
 */

import React, { useState } from 'react';
import {
    AlertCircle,
    CheckCircle,
    Loader2,
    Plug,
    Plus,
    Sparkles,
    Wifi,
    WifiOff,
    X,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import type {
    AgentCardDetailResponse,
    AgentCardDiscoverResponse,
} from '../../lib/a2aTypes';
import { CardIcon } from './AgentCardImportModal';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler } from '../../lib/asyncSafe';

interface Props {
    onClose: () => void;
    /** Called after a card is created. Parent typically reloads the
     *  list and opens the new card's detail view. */
    onCreated: (card: AgentCardDetailResponse) => void;
}

const AgentCardCreateDialog: React.FC<Props> = ({ onClose, onCreated }) => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    // ── Form fields ───────────────────────────────────────────────
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [url, setUrl] = useState('');

    // Did the operator manually edit Name / Description? If they did,
    // we stop auto-syncing from discovery results — their text wins.
    const [nameDirty, setNameDirty] = useState(false);
    const [descriptionDirty, setDescriptionDirty] = useState(false);

    // ── Connect state ─────────────────────────────────────────────
    const [connecting, setConnecting] = useState(false);
    const [discovery, setDiscovery] = useState<AgentCardDiscoverResponse | null>(null);
    const [connectError, setConnectError] = useState<string | null>(null);

    // ── Create state ──────────────────────────────────────────────
    const [creating, setCreating] = useState(false);

    // Re-running Connect after the operator edits the URL field is
    // valuable — the Create button's gate becomes "did the LAST connect
    // for THIS URL succeed?". `connectedUrl` is the URL we last
    // confirmed; if `url` drifts away from it, Create is gated again.
    const [connectedUrl, setConnectedUrl] = useState<string | null>(null);

    const trimmedUrl = url.trim();
    const trimmedName = name.trim();
    const canConnect = !!trimmedUrl && !connecting;
    const connectionStillValid =
        !!discovery && connectedUrl === trimmedUrl && !connectError;
    const canCreate =
        connectionStillValid && !creating && !!trimmedName;

    async function handleConnect() {
        if (!canConnect) return;
        setConnecting(true);
        setConnectError(null);
        setDiscovery(null);
        try {
            const resp = await gatewayClient.discoverA2AAgentCard({
                source_url: trimmedUrl,
            });
            setDiscovery(resp);
            setConnectedUrl(trimmedUrl);

            // Auto-fill Name + Description from the discovered card
            // unless the operator already typed something. The dirty
            // flags freeze the operator's choice — we never overwrite
            // typed text because that's a privacy/UX surprise.
            if (!nameDirty) {
                // If a same-named card already exists in this tenant,
                // the operator MUST pick a different name — leaving
                // the auto-fill in place would just produce a 409 on
                // Create. Empty out the field as a clear signal.
                setName(resp.name_conflict ? '' : (resp.name ?? ''));
            }
            if (!descriptionDirty) {
                setDescription(resp.description ?? '');
            }
        } catch (e) {
            setConnectError(errorMessage(e) ?? 'Connection failed');
        } finally {
            setConnecting(false);
        }
    }

    async function handleCreate() {
        if (!canCreate || !discovery) return;
        setCreating(true);
        try {
            const detail = await gatewayClient.importA2AAgentCard({
                source_url: trimmedUrl,
                name_override: trimmedName,
                description_override: description.trim() || null,
            });
            toast.success('Agent card created', detail.name);
            onCreated(detail);
        } catch (e) {
            toast.error('Create failed', errorMessage(e) ?? 'Could not create card');
        } finally {
            setCreating(false);
        }
    }

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            onClick={onClose}
        >
            <div
                className="w-full max-w-2xl bg-surface border border-default rounded-xl shadow-xl flex flex-col"
                style={{ maxHeight: '90vh' }}
                onClick={e => e.stopPropagation()}
            >
                {/* Header */}
                <div className="border-b border-default px-5 py-4 flex items-center justify-between">
                    <div className="flex items-center space-x-2">
                        <Plus className="w-5 h-5 text-primary-600" />
                        <h3 className="text-base font-semibold text-default">
                            Add Agent Card
                        </h3>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1 text-muted hover:text-default rounded transition-colors"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region px-5 py-4 space-y-4">
                    {/* Hint */}
                    <div className="rounded-lg bg-primary-50 border border-primary-200 p-3 text-sm text-primary-700">
                        <p className="font-medium mb-1 flex items-center gap-1.5">
                            <Sparkles className="w-4 h-4" />
                            URL-first registration
                        </p>
                        <p className="text-xs text-primary-600">
                            We connect to the agent's <code>/.well-known/agent-card.json</code>{' '}
                            (or legacy <code>/.well-known/agent.json</code>) and import the
                            card it returns. Manual JSON pasting moves to the Edit dialog
                            after the card exists.
                        </p>
                    </div>

                    {/* Name */}
                    <FormField
                        label="Name"
                        required
                        hint="Local label shown in this admin UI. Must be unique per tenant."
                    >
                        <input
                            value={name}
                            onChange={e => { setName(e.target.value); setNameDirty(true); }}
                            placeholder="e.g. salem-hotel-agent"
                            className="w-full px-3 py-2 rounded-lg border border-strong bg-surface text-default text-sm focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
                        />
                        {discovery?.name_conflict && (
                            <p className="text-xs text-amber-700 mt-1">
                                The remote card's name (<code>{discovery.name}</code>) is
                                already taken in this tenant. Pick a different name.
                            </p>
                        )}
                    </FormField>

                    {/* Description */}
                    <FormField
                        label="Description"
                        hint="Operator-friendly summary. Falls back to the remote card's description on Connect."
                    >
                        <textarea
                            value={description}
                            onChange={e => { setDescription(e.target.value); setDescriptionDirty(true); }}
                            rows={2}
                            placeholder="What this agent is used for in your org"
                            className="w-full px-3 py-2 rounded-lg border border-strong bg-surface text-default text-sm focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
                        />
                    </FormField>

                    {/* URL */}
                    <FormField
                        label="Agent URL"
                        required
                        hint="Base URL works (we try both well-known paths). You can also paste a full /.well-known/agent-card.json URL."
                    >
                        <input
                            value={url}
                            onChange={e => {
                                setUrl(e.target.value);
                                // Editing the URL invalidates the prior connection — the
                                // operator has to re-Connect against the new endpoint.
                                if (e.target.value.trim() !== connectedUrl) {
                                    setDiscovery(null);
                                    setConnectError(null);
                                }
                            }}
                            placeholder="https://example.com or https://example.com/.well-known/agent-card.json"
                            className="w-full px-3 py-2 rounded-lg border border-strong bg-surface text-default text-sm font-mono focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
                        />
                    </FormField>

                    {/* Connect button */}
                    <button
                        type="button"
                        onClick={asyncHandler(handleConnect, 'click')}
                        disabled={!canConnect}
                        className={
                            'w-full px-4 py-2 border rounded-md font-medium text-sm transition-colors ' +
                            (connecting
                                ? 'bg-gray-400 text-white cursor-wait border-gray-400'
                                : !trimmedUrl
                                    ? 'bg-surface-inset text-muted border-strong cursor-not-allowed'
                                    : connectionStillValid
                                        ? 'bg-green-50 text-green-700 border-green-300 hover:bg-green-100'
                                        : 'bg-primary-500 text-white border-primary-500 hover:bg-primary-600')
                        }
                    >
                        {connecting ? (
                            <span className="flex items-center justify-center space-x-2">
                                <Loader2 className="w-4 h-4 animate-spin" />
                                <span>Connecting…</span>
                            </span>
                        ) : connectionStillValid ? (
                            <span className="flex items-center justify-center space-x-2">
                                <Wifi className="w-4 h-4" />
                                <span>Connected — re-run</span>
                            </span>
                        ) : (
                            <span className="flex items-center justify-center space-x-2">
                                <WifiOff className="w-4 h-4" />
                                <span>Connect</span>
                            </span>
                        )}
                    </button>

                    {/* Connection result */}
                    {connectError && (
                        <div className="rounded-md border border-red-200 bg-red-50 p-3 flex items-start space-x-2">
                            <AlertCircle className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                            <div className="flex-1 min-w-0 min-h-0">
                                <p className="text-sm font-medium text-red-900">
                                    Connection failed
                                </p>
                                <p className="text-xs text-red-700 mt-0.5 break-words">
                                    {connectError}
                                </p>
                            </div>
                        </div>
                    )}

                    {connectionStillValid && discovery && (
                        <DiscoveryPreview discovery={discovery} />
                    )}
                </div>

                {/* Footer */}
                <div className="border-t border-default px-5 py-3 flex items-center justify-between">
                    <div className="text-xs text-muted">
                        {connectionStillValid
                            ? 'Click Create to import this card and its capabilities.'
                            : 'Connect to a working agent endpoint to enable Create.'}
                    </div>
                    <div className="flex items-center space-x-2">
                        <button
                            onClick={onClose}
                            className="px-4 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleCreate, 'click')}
                            disabled={!canCreate}
                            className={
                                'flex items-center space-x-1.5 px-4 py-1.5 text-sm font-medium rounded-lg transition-colors ' +
                                (canCreate
                                    ? 'text-white bg-primary-500 hover:bg-primary-600'
                                    : 'text-muted bg-surface-inset border border-strong cursor-not-allowed')
                            }
                            title={
                                !connectionStillValid
                                    ? 'Connect first'
                                    : !trimmedName
                                        ? 'Set a name'
                                        : ''
                            }
                        >
                            {creating ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                                <Plus className="w-4 h-4" />
                            )}
                            <span>Create</span>
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default AgentCardCreateDialog;

// ─── Sub-components ─────────────────────────────────────────────────────────

const FormField: React.FC<{
    label: string;
    required?: boolean;
    hint?: string;
    children: React.ReactNode;
}> = ({ label, required, hint, children }) => (
    <div>
        <label className="block text-xs font-medium text-secondary mb-1">
            {label}
            {required && <span className="text-red-500 ml-0.5">*</span>}
        </label>
        {children}
        {hint && <p className="text-[11px] text-muted mt-1">{hint}</p>}
    </div>
);

const DiscoveryPreview: React.FC<{
    discovery: AgentCardDiscoverResponse;
}> = ({ discovery }) => {
    const usedFallback =
        discovery.resolved_url.endsWith('/.well-known/agent.json');
    return (
        <div className="rounded-md border border-green-200 bg-green-50 p-4">
            <div className="flex items-start gap-3">
                <CheckCircle className="w-5 h-5 text-green-600 mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0 min-h-0">
                    <div className="flex items-center gap-3">
                        <CardIcon
                            src={discovery.icon_url}
                            alt={discovery.name ?? '?'}
                            size={40}
                        />
                        <div className="min-w-0">
                            <p className="text-sm font-semibold text-green-900 truncate">
                                {discovery.name || '(unnamed agent)'}
                                {discovery.version && (
                                    <span className="ml-1.5 text-xs font-normal text-green-700">
                                        v{discovery.version}
                                    </span>
                                )}
                            </p>
                            <p className="text-xs text-green-700 truncate">
                                {discovery.description ?? '(no description)'}
                            </p>
                        </div>
                    </div>

                    {/* Meta strip */}
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
                        {discovery.protocol_version && (
                            <span className="font-mono px-1.5 py-0.5 rounded bg-white text-green-800 border border-green-200">
                                A2A {discovery.protocol_version}
                            </span>
                        )}
                        {discovery.provider_name && (
                            <span className="text-green-800">
                                Provider: {discovery.provider_name}
                            </span>
                        )}
                        <span className="text-green-800">
                            {discovery.capability_count}{' '}
                            {discovery.capability_count === 1 ? 'skill' : 'skills'}
                        </span>
                        {usedFallback && (
                            <span className="text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5">
                                legacy 0.2.x path
                            </span>
                        )}
                    </div>

                    {/* Skill list */}
                    {discovery.capabilities.length > 0 && (
                        <div className="mt-3">
                            <p className="text-[11px] font-semibold uppercase text-green-800 tracking-wider mb-1">
                                Skills the agent advertises
                            </p>
                            <ul className="divide-y divide-green-200 border border-green-200 rounded bg-white">
                                {discovery.capabilities.map((c, idx) => (
                                    <li
                                        key={c.id || c.name || idx}
                                        className="px-2.5 py-1.5 flex items-start gap-2"
                                    >
                                        <Plug className="w-3.5 h-3.5 text-green-600 mt-0.5 flex-shrink-0" />
                                        <div className="min-w-0 flex-1 min-h-0">
                                            <p className="text-xs font-mono text-default truncate">
                                                {c.name}
                                            </p>
                                            {c.description && (
                                                <p className="text-[11px] text-secondary line-clamp-2">
                                                    {c.description}
                                                </p>
                                            )}
                                            {c.tags.length > 0 && (
                                                <p className="text-[10px] text-muted mt-0.5">
                                                    {c.tags.map(t => '#' + t).join(' ')}
                                                </p>
                                            )}
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}

                    <p className="mt-2 text-[11px] text-muted font-mono truncate">
                        {discovery.resolved_url}
                    </p>
                </div>
            </div>
        </div>
    );
};
