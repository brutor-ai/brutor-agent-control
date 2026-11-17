/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Endpoint reference for an Agent Card.
 *
 * Mirrors `EndpointModal.tsx` from the MCP server list — same visual
 * idiom (one URL block per endpoint, each with a copy button + a
 * dedicated `<code>` line). The difference is A2A is multi-endpoint
 * per card (the v1.0 spec splits send/stream/cancel/subscribe across
 * separate paths) so we render a small list rather than a single URL.
 *
 * The discovery URL is unauthenticated by design — anyone can fetch
 * it to inspect what the agent advertises. The `/a2a/...` invocation
 * URLs require the tenant's API key. The Notes panel calls that out
 * so an operator copying the URL doesn't paste it into a browser
 * expecting the chat UI MCP shows.
 */

import React, { useState } from 'react';
import { CheckCircle, Copy, X } from 'lucide-react';
import InfoBanner from '../common/InfoBanner';
import { asyncHandler } from '../../lib/asyncSafe';

interface Props {
    cardName: string;
    tenantId: string;
    gatewayBaseUrl: string;
    onClose: () => void;
}

interface EndpointSpec {
    /** Human label shown above the URL block */
    label: string;
    /** HTTP method — purely cosmetic; the chip in the corner of the URL */
    method: 'GET' | 'POST';
    /** Path appended to the gateway base URL */
    path: string;
    /** One-line hint shown beside the URL */
    hint: string;
}

const AgentCardEndpointModal: React.FC<Props> = ({
    cardName,
    tenantId,
    gatewayBaseUrl,
    onClose,
}) => {
    const base = gatewayBaseUrl.replace(/\/$/, '');
    const enc = (s: string) => encodeURIComponent(s);
    const cardPath = `${enc(tenantId)}/${enc(cardName)}`;

    const discoveryUrl = `${base}/.well-known/agent-card/${cardPath}.json`;

    const endpoints: EndpointSpec[] = [
        {
            label: 'message:send',
            method: 'POST',
            path: `/a2a/${cardPath}/message:send`,
            hint: 'Send one message; receive a Task envelope synchronously.',
        },
        {
            label: 'message:stream',
            method: 'POST',
            path: `/a2a/${cardPath}/message:stream`,
            hint: 'Streaming variant — Server-Sent Events with TaskStatusUpdateEvent frames.',
        },
        {
            label: 'tasks',
            method: 'GET',
            path: `/a2a/${cardPath}/tasks`,
            hint: 'List tasks created against this card.',
        },
        {
            label: 'tasks/{id}:subscribe',
            method: 'GET',
            path: `/a2a/${cardPath}/tasks/{task_id}:subscribe`,
            hint: 'SSE feed of state transitions for a single task.',
        },
        {
            label: 'extendedAgentCard',
            method: 'GET',
            path: `/a2a/${cardPath}/extendedAgentCard`,
            hint: 'Authenticated extended card (private skills + tenant-scoped data).',
        },
    ];

    return (
        <div
            className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
            onClick={onClose}
        >
            <div
                className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] flex flex-col"
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <h3 className="text-lg font-semibold text-default">
                        A2A Proxy Endpoints
                    </h3>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                    >
                        <X className="w-5 h-5 text-muted" />
                    </button>
                </div>

                <div className="px-6 py-4 overflow-y-auto">
                    <p className="text-sm text-secondary mb-4">
                        Card:{' '}
                        <span className="font-medium text-default">{cardName}</span> ·{' '}
                        Tenant:{' '}
                        <span className="font-mono text-xs text-default">{tenantId}</span>
                    </p>

                    {/* Discovery — unauthenticated */}
                    <UrlBlock
                        title="Discovery (public)"
                        method="GET"
                        url={discoveryUrl}
                        hint="Verbatim agent card JSON + signature. No auth required."
                    />

                    {/* Invocation endpoints */}
                    <h4 className="text-xs font-semibold uppercase text-muted tracking-wider mt-5 mb-2">
                        Invocation (authenticated)
                    </h4>
                    {endpoints.map(e => (
                        <UrlBlock
                            key={e.path}
                            title={e.label}
                            method={e.method}
                            url={`${base}${e.path}`}
                            hint={e.hint}
                        />
                    ))}

                    <InfoBanner title="Integration notes" className="mt-4 mb-0">
                        <ul className="space-y-1 text-xs">
                            <li>
                                • The discovery URL is open; the{' '}
                                <code>/a2a/…</code> URLs require an{' '}
                                <code>X-API-Key</code> header from the tenant.
                            </li>
                            <li>
                                • Replace <code>{'{task_id}'}</code> in the subscribe
                                URL with a task id returned by{' '}
                                <code>message:send</code>.
                            </li>
                            <li>
                                • Streaming endpoints use{' '}
                                <code>text/event-stream</code>; clients should
                                keep the connection open until the task reaches a
                                terminal state.
                            </li>
                        </ul>
                    </InfoBanner>
                </div>

                <div className="flex justify-end px-6 py-4 border-t border-default bg-surface-alt">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AgentCardEndpointModal;

// ─── Sub-components ─────────────────────────────────────────────────────────

const UrlBlock: React.FC<{
    title: string;
    method: 'GET' | 'POST';
    url: string;
    hint?: string;
}> = ({ title, method, url, hint }) => {
    const [copied, setCopied] = useState(false);

    async function copy() {
        try {
            await navigator.clipboard.writeText(url);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch {
            // ignore — older browsers don't have the clipboard API
        }
    }

    const methodColor =
        method === 'POST'
            ? 'bg-amber-100 text-amber-800 border-amber-200'
            : 'bg-emerald-100 text-emerald-800 border-emerald-200';

    return (
        <div className="bg-surface-alt border border-default rounded-lg p-3 mb-2">
            <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center space-x-2 min-w-0">
                    <span
                        className={`text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded border ${methodColor}`}
                    >
                        {method}
                    </span>
                    <span className="text-xs font-medium text-secondary truncate">
                        {title}
                    </span>
                </div>
                <button
                    onClick={asyncHandler(copy, 'click')}
                    className="px-2.5 py-1 text-xs bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-1.5 flex-shrink-0"
                >
                    {copied ? (
                        <>
                            <CheckCircle className="w-3.5 h-3.5" />
                            <span>Copied</span>
                        </>
                    ) : (
                        <>
                            <Copy className="w-3.5 h-3.5" />
                            <span>Copy</span>
                        </>
                    )}
                </button>
            </div>
            <code className="block text-xs break-all text-default font-mono bg-surface border border-default rounded p-2">
                {url}
            </code>
            {hint && (
                <p className="text-[11px] text-muted mt-1.5">{hint}</p>
            )}
        </div>
    );
};
