/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ProxyLogDetailsModal — full-data audit drill-down for one
 * proxy_log row.
 *
 * The inline row expansion in ProxyLogsViewer is intentionally
 * compact — body previews are clamped to ~max-h-32, headers are
 * hidden, MCP / OAuth / custom-metadata blocks are omitted. That
 * shape is right for triage ("scan 50 rows, find the failing
 * one"), wrong for forensics ("a guardrail fired at 09:14 — show
 * me literally everything we recorded about that request").
 *
 * This modal is the forensics view. It iterates the entire
 * UnifiedProxyLogEntry shape, groups fields into operator-meaningful
 * sections, pretty-prints JSON bodies + headers, and clamps nothing.
 * Bodies live in their own scrollable panel so a multi-KB request
 * payload doesn't push the rest of the metadata off-screen.
 */

import { GatewayRefusalNote } from './GatewayRefusalNote';
import React from 'react';
import { X, Copy, Check } from 'lucide-react';
import { UnifiedProxyLogEntry } from '../../lib/types';
import { refusedCredentialKind, refusedCredentialOf, refusedCredentialSentence } from '../../lib/refusedCredential';

interface Props {
    log: UnifiedProxyLogEntry;
    onClose: () => void;
}

/**
 * If `text` parses as JSON, return it pretty-printed with 2-space
 * indent. Otherwise return as-is. Body previews from the proxy
 * are usually JSON-RPC envelopes or OpenAI-shaped chat completions
 * — both are noticeably easier to scan when indented.
 */
function prettyJsonOrText(text?: string | null): string {
    if (!text) return '';
    const trimmed = text.trim();
    if (!trimmed) return text;
    if (trimmed[0] !== '{' && trimmed[0] !== '[') return text;
    try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
        return text;
    }
}

/** Same idea for header blobs the server stringifies as JSON. */
function parsedHeaders(raw?: string | null): Array<[string, string]> | null {
    if (!raw) return null;
    try {
        const obj = JSON.parse(raw) as Record<string, unknown>;
        return Object.entries(obj).map(([k, v]) => [k, String(v)]);
    } catch {
        return null;
    }
}

function fmtTimestamp(ts: string | undefined | null): string {
    if (!ts) return '—';
    try {
        return new Date(ts).toLocaleString();
    } catch {
        return ts;
    }
}

function fmtBytes(n?: number | null): string {
    if (n == null) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1_048_576) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1_048_576).toFixed(2)} MB`;
}

function fmtDuration(ms?: number | null): string {
    if (ms == null) return '—';
    if (ms < 1000) return `${ms.toFixed(1)} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
}

// Small button that copies a value to the clipboard and flips its
// icon for 1.5 s as feedback. Common need on this modal — every
// ID-shaped field is something operators paste into a search box.
const CopyButton: React.FC<{ value: string }> = ({ value }) => {
    const [copied, setCopied] = React.useState(false);
    return (
        <button
            type="button"
            onClick={() => {
                void navigator.clipboard.writeText(value);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
            }}
            className="inline-flex items-center text-muted hover:text-default p-0.5 rounded transition-colors"
            title="Copy"
        >
            {copied ? <Check className="w-3 h-3 text-green-600" /> : <Copy className="w-3 h-3" />}
        </button>
    );
};

const Section: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
    <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
            {title}
        </h3>
        <div className="bg-surface-alt rounded-md border border-default p-3">
            {children}
        </div>
    </section>
);

/**
 * Two-column key / value row. `value` may be a node so callers can
 * render badges, copy buttons, etc. inline.
 */
const Field: React.FC<{
    label: string;
    value?: React.ReactNode;
    mono?: boolean;
    copy?: string;
    full?: boolean;
}> = ({ label, value, mono, copy, full }) => {
    const isEmpty = value == null || value === '' || value === '—';
    return (
        <div className={`grid ${full ? 'grid-cols-1' : 'grid-cols-[10rem,1fr]'} gap-x-3 gap-y-0.5 items-baseline py-0.5`}>
            <div className="text-xs text-muted">{label}</div>
            <div className={`text-xs ${mono ? 'font-mono' : ''} ${isEmpty ? 'text-muted italic' : 'text-default'} break-all flex items-center gap-1.5`}>
                <span className="flex-1 min-h-0 min-w-0">{isEmpty ? '—' : value}</span>
                {copy && !isEmpty && <CopyButton value={copy} />}
            </div>
        </div>
    );
};

const Pre: React.FC<{ children: string; maxHeight?: string }> = ({ children, maxHeight = '24rem' }) => (
    <pre
        className="text-xs font-mono whitespace-pre-wrap break-words bg-surface border border-default rounded p-3 overflow-y-auto"
        style={{ maxHeight }}
    >
        {children || <span className="text-muted italic">No body recorded</span>}
    </pre>
);

const ProxyLogDetailsModal: React.FC<Props> = ({ log, onClose }) => {
    const reqHeaders = parsedHeaders(log.request?.headers);
    const respHeaders = parsedHeaders(log.response?.headers);
    const refused = refusedCredentialOf(log);

    return (
        <div
            className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
            onMouseDown={(e) => {
                // Backdrop click closes; clicks inside the inner card
                // stop propagation via the inner onMouseDown below.
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <div
                className="bg-surface rounded-lg shadow-xl w-full max-w-5xl h-[90vh] flex flex-col"
                onMouseDown={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="flex items-center justify-between px-5 py-3 border-b border-default flex-shrink-0">
                    <div>
                        <h2 className="text-base font-semibold text-default">Proxy Log Details</h2>
                        <p className="text-xs text-muted mt-0.5">
                            {fmtTimestamp(log.timestamp)} · <span className="font-mono">{log.id}</span>
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 text-muted hover:text-default hover:bg-surface-alt rounded-md transition-colors"
                        aria-label="Close details"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-5 space-y-5">

                    {/* ── Identification ──────────────────────────── */}
                    <Section title="Identification">
                        <Field label="Log ID" value={log.id} mono copy={log.id} />
                        <Field label="Timestamp" value={fmtTimestamp(log.timestamp)} />
                        <Field label="Tenant ID" value={log.tenant_id} mono copy={log.tenant_id} />
                        <Field label="Proxy Type" value={
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase bg-primary-50 text-primary-700">
                                {log.proxy_type}
                            </span>
                        } />
                        {log.proxy_subtype && <Field label="Proxy Subtype" value={log.proxy_subtype} />}
                        <Field label="Success" value={
                            <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                log.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
                            }`}>
                                {log.success ? 'YES' : 'NO'}
                            </span>
                        } />
                    </Section>

                    {/* ── Refused credential ──────────────────────── */}
                    {refused && (
                        <div data-testid="refused-credential-details">
                            <Section title="Refused credential">
                                <p className="text-xs text-default mb-2">{refusedCredentialSentence(refused)}</p>
                                <Field label="Credential" value={refusedCredentialKind(refused.credential)} />
                                <Field label="Route" value={refused.route} mono />
                                <Field label="Refusals" value={refused.refusals === null ? null : String(refused.refusals)} />
                            </Section>
                        </div>
                    )}

                    {/* ── Correlation / Tracing ───────────────────── */}
                    <Section title="Correlation & Tracing">
                        <Field label="Correlation ID" value={log.correlation_id} mono copy={log.correlation_id || undefined} />
                        <Field label="Parent Request ID" value={log.parent_request_id} mono copy={log.parent_request_id || undefined} />
                        <Field label="Request ID" value={log.request_id} mono copy={log.request_id || undefined} />
                        {/* The caller's own W3C trace context, so this row can be
                            found from their tracing tool. Asserted by the client. */}
                        {log.trace_id && (
                            <>
                                <Field label="Trace ID (W3C)" value={log.trace_id} mono copy={log.trace_id} />
                                <Field label="Span ID (W3C)" value={log.span_id} mono copy={log.span_id || undefined} />
                            </>
                        )}
                        {/* Multi-step call chain. Only the
                            chain-stamped subset of fields is surfaced here; the
                            "View Trace" button in the row's expanded toolbar
                            opens the full timeline rooted at root_task_id. */}
                        {(log.root_task_id || log.current_task_id || log.delegation_depth != null || log.trace_root_kind) && (
                            <>
                                <Field label="Trace Root" value={log.root_task_id} mono copy={log.root_task_id || undefined} />
                                <Field label="Current Task" value={log.current_task_id} mono copy={log.current_task_id || undefined} />
                                <Field label="Parent Task" value={log.parent_task_id} mono copy={log.parent_task_id || undefined} />
                                <Field label="Depth" value={log.delegation_depth != null ? String(log.delegation_depth) : null} />
                                <Field label="Root Kind" value={log.trace_root_kind} mono />
                                <Field label="Continuity" value={log.trace_continuity} mono />
                            </>
                        )}
                    </Section>

                    {/* ── Content integrity & tool provenance ── */}
                    {(log.request_content_hash || log.response_content_hash || log.tool_definition_hash
                        || log.server_version || log.deciding_model) && (
                        <Section title="Content Integrity & Tool Provenance">
                            {/* Digests are sealed over the content as captured, before any
                                retention mode touched the preview, and sit inside the row
                                hash — so they can be checked against an original held
                                elsewhere even after the body here is gone. */}
                            <Field label="Request Digest (SHA-256)" value={log.request_content_hash} mono copy={log.request_content_hash || undefined} />
                            <Field label="Response Digest (SHA-256)" value={log.response_content_hash} mono copy={log.response_content_hash || undefined} />
                            <Field label="Tool Definition (SHA-256)" value={log.tool_definition_hash} mono copy={log.tool_definition_hash || undefined} />
                            <Field label="Server Version" value={log.server_version} mono />
                            <Field label="Decided By Model" value={log.deciding_model} mono />
                        </Section>
                    )}

                    {/* ── Compliance & Residency ──────────────────── */}
                    {((log.compliance_tags !== undefined && log.compliance_tags !== null) || log.residency_class || log.region_routed_to) && (
                        <Section title="Compliance & Residency">
                            {log.compliance_tags !== undefined && log.compliance_tags !== null && (
                                <Field
                                    full
                                    label="Compliance Tags"
                                    value={
                                        log.compliance_tags.length === 0 ? (
                                            <span className="text-[11px] text-muted italic">
                                                Profile declared — no tags fired on this call
                                            </span>
                                        ) : (
                                            <div className="flex flex-wrap gap-1">
                                                {log.compliance_tags.map((t) => (
                                                    <span
                                                        key={t}
                                                        className="inline-flex items-center px-1.5 py-0.5 rounded border border-default bg-surface text-[11px] font-mono"
                                                    >
                                                        {t}
                                                    </span>
                                                ))}
                                            </div>
                                        )
                                    }
                                />
                            )}
                            <Field label="Region Routed" value={log.region_routed_to} mono />
                            <Field label="Residency Class" value={log.residency_class} mono />
                        </Section>
                    )}

                    {/* ── User / Session / Network ────────────────── */}
                    <Section title="User · Session · Network">
                        <Field label="Principal" value={
                            log.gateway_user_username || log.gateway_user_id
                                ? `${log.gateway_user_username || ''} (${log.gateway_user_id || ''})`.replace(/^ \(/, '(')
                                : null
                        } />
                        <Field label="IdP User"
                            value={
                                log.idp_user_username || log.idp_user_id || log.idp_user_email
                                    ? `${log.idp_user_username || ''} ${log.idp_user_email ? `<${log.idp_user_email}>` : ''} ${log.idp_user_id ? `(${log.idp_user_id})` : ''}`.trim()
                                    : null
                            }
                        />
                        <Field label="Session ID" value={log.session_id} mono copy={log.session_id} />
                        <Field label="Client IP" value={log.client_ip} mono />
                        <Field label="User Agent" value={log.user_agent} />
                    </Section>

                    {/* ── Target ──────────────────────────────────── */}
                    <Section title="Target">
                        <Field label="Server ID" value={log.server_id} mono copy={log.server_id || undefined} />
                        <Field label="IdP Server ID" value={log.idp_server_id} mono />
                        <Field label="Target URL" value={log.request?.target_url || log.target_url} mono full />
                        <Field label="Endpoint" value={log.request?.endpoint || log.endpoint} mono />
                    </Section>

                    {/* ── Request ─────────────────────────────────── */}
                    <Section title="Request">
                        <Field label="HTTP Method" value={log.request?.method || log.http_method} mono />
                        <Field label="Size" value={fmtBytes(log.request?.size_bytes ?? log.request_size_bytes)} />
                        {reqHeaders ? (
                            <Field label="Headers" full value={
                                <div className="space-y-0.5 mt-0.5">
                                    {reqHeaders.map(([k, v]) => (
                                        <div key={k} className="font-mono text-xs">
                                            <span className="text-muted">{k}:</span> <span className="text-default">{v}</span>
                                        </div>
                                    ))}
                                </div>
                            } />
                        ) : (
                            <Field label="Headers" value={log.request?.headers} mono />
                        )}
                        <div className="mt-3">
                            <div className="text-xs text-muted mb-1">Body</div>
                            <Pre>{prettyJsonOrText(log.request?.body_preview)}</Pre>
                        </div>
                    </Section>

                    {/* ── Response ────────────────────────────────── */}
                    <Section title="Response">
                        <Field label="Status Code" value={
                            log.response?.status_code != null || log.http_status_code != null
                                ? <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                    (log.response?.status_code || log.http_status_code || 0) < 400 ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
                                }`}>
                                    {log.response?.status_code ?? log.http_status_code}
                                </span>
                                : null
                        } />
                        <Field label="Duration" value={fmtDuration(log.response?.duration_ms ?? log.duration_ms)} />
                        <Field label="Size" value={fmtBytes(log.response?.size_bytes ?? log.response_size_bytes)} />
                        <Field label="Cache Hit" value={log.cache_hit == null ? null : (log.cache_hit ? 'Yes' : 'No')} />
                        {respHeaders ? (
                            <Field label="Headers" full value={
                                <div className="space-y-0.5 mt-0.5">
                                    {respHeaders.map(([k, v]) => (
                                        <div key={k} className="font-mono text-xs">
                                            <span className="text-muted">{k}:</span> <span className="text-default">{v}</span>
                                        </div>
                                    ))}
                                </div>
                            } />
                        ) : (
                            <Field label="Headers" value={log.response?.headers} mono />
                        )}
                        <div className="mt-3">
                            <div className="text-xs text-muted mb-1">Body</div>
                            <Pre>{prettyJsonOrText(log.response?.body_preview)}</Pre>
                        </div>
                        {log.error_message && (
                            <div className="mt-3">
                                <div className="text-xs font-semibold text-red-700 mb-1">Error</div>
                                <div className="bg-red-50 border border-red-200 rounded p-2 text-xs text-red-800">
                                    <GatewayRefusalNote text={log.error_message} />
                                    {log.error_message}
                                    {log.error_type && (
                                        <span className="ml-2 text-red-600 font-mono">({log.error_type})</span>
                                    )}
                                </div>
                            </div>
                        )}
                    </Section>

                    {/* ── Protocol-specific blocks ────────────────── */}
                    {log.mcp && (
                        <Section title="MCP">
                            <Field label="Method" value={log.mcp.method} mono />
                            <Field label="Request ID" value={log.mcp.request_id} mono />
                            <Field label="Capability" value={log.mcp.capability_name} mono />
                            <Field label="Protocol Version" value={log.mcp.protocol_version} mono />
                            <Field label="Streaming" value={log.mcp.streaming == null ? null : (log.mcp.streaming ? 'Yes' : 'No')} />
                        </Section>
                    )}

                    {log.llm && (
                        <Section title="LLM">
                            <Field label="Provider" value={log.llm.provider} />
                            <Field label="Model Requested" value={log.llm.model_requested} mono />
                            <Field label="Model Used" value={log.llm.model_used} mono />
                            <Field label="Prompt Tokens" value={log.llm.prompt_tokens?.toLocaleString()} />
                            <Field label="Completion Tokens" value={log.llm.completion_tokens?.toLocaleString()} />
                            <Field label="Total Tokens" value={log.llm.total_tokens?.toLocaleString()} />
                            <Field label="Estimated Cost" value={
                                log.llm.estimated_cost_usd != null ? `$${log.llm.estimated_cost_usd.toFixed(6)}` : null
                            } />
                            <Field label="Streaming" value={log.llm.streaming == null ? null : (log.llm.streaming ? 'Yes' : 'No')} />
                            <Field label="Temperature" value={log.llm.temperature ?? null} />
                            <Field label="Max Tokens" value={log.llm.max_tokens?.toLocaleString()} />
                            <Field label="Function Calling" value={log.llm.function_calling == null ? null : (log.llm.function_calling ? 'Yes' : 'No')} />
                            <Field label="Used Fallback" value={log.llm.used_fallback == null ? null : (log.llm.used_fallback ? 'Yes' : 'No')} />
                            {log.llm.fallback_reason && <Field label="Fallback Reason" value={log.llm.fallback_reason} />}
                            <Field label="Router Rule" value={log.llm.router_rule_id} mono />
                            {log.llm.cache_hit && (
                                <>
                                    <Field label="Cache Hit" value="Yes" />
                                    <Field label="Cache Layer" value={log.llm.cache_layer} />
                                    <Field label="Tokens Saved" value={log.llm.tokens_saved?.toLocaleString()} />
                                    <Field label="Similarity Score" value={
                                        log.llm.cache_similarity_score != null
                                            ? `${(log.llm.cache_similarity_score * 100).toFixed(2)}%`
                                            : null
                                    } />
                                </>
                            )}
                            {/* Semantic-cache self-call embeddings (the request-side
                                query embed and response-side key embed) carry a
                                lookup/store cache_layer but are NOT cache hits, so the
                                cache_hit block above skips them. Surface the operation
                                here so the detail panel isn't blank for these rows. */}
                            {!log.llm.cache_hit && (log.llm.cache_layer === 'lookup' || log.llm.cache_layer === 'store') && (
                                <Field label="Cache Operation" value={`cache:${log.llm.cache_layer}`} mono />
                            )}
                        </Section>
                    )}

                    {log.oauth && (
                        <Section title="OAuth">
                            <Field label="IdP ID" value={log.oauth.idp_id} mono />
                            <Field label="Flow Type" value={log.oauth.flow_type} />
                            <Field label="Grant Type" value={log.oauth.grant_type} />
                            <Field label="Scope" value={log.oauth.scope} mono />
                            <Field label="Code Exchange" value={log.oauth.code_exchange == null ? null : (log.oauth.code_exchange ? 'Yes' : 'No')} />
                            <Field label="Token Refresh" value={log.oauth.token_refresh == null ? null : (log.oauth.token_refresh ? 'Yes' : 'No')} />
                        </Section>
                    )}

                    {/* ── Metadata + tags ─────────────────────────── */}
                    {(log.custom_metadata || (log.tags && log.tags.length > 0)) && (
                        <Section title="Metadata">
                            {log.tags && log.tags.length > 0 && (
                                <Field label="Tags" full value={
                                    <div className="flex flex-wrap gap-1 mt-0.5">
                                        {log.tags.map((t) => (
                                            <span key={t} className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-surface text-default border border-default">
                                                {t}
                                            </span>
                                        ))}
                                    </div>
                                } />
                            )}
                            {log.custom_metadata && Object.keys(log.custom_metadata).length > 0 && (
                                <div className="mt-3">
                                    <div className="text-xs text-muted mb-1">Custom metadata</div>
                                    <Pre maxHeight="16rem">{JSON.stringify(log.custom_metadata, null, 2)}</Pre>
                                </div>
                            )}
                        </Section>
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between px-5 py-3 border-t border-default flex-shrink-0 bg-surface-alt">
                    <button
                        type="button"
                        onClick={() => {
                            void navigator.clipboard.writeText(JSON.stringify(log, null, 2));
                        }}
                        className="text-xs text-secondary hover:text-default px-3 py-1.5 rounded border border-default hover:bg-surface transition-colors inline-flex items-center gap-1.5"
                        title="Copy the full log entry as JSON"
                    >
                        <Copy className="w-3.5 h-3.5" /> Copy as JSON
                    </button>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-xs font-medium px-4 py-1.5 rounded-md bg-primary-600 text-white hover:bg-primary-700 transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default ProxyLogDetailsModal;
