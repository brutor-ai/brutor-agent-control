/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
    X,
    Wrench,
    FolderOpen,
    MessageSquare,
    FileText,
    Settings2,
    ChevronRight,
    ChevronDown,
    Copy,
    Check,
    AlertTriangle,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { env } from '../../../../env.ts';
import { asyncHandler } from '../../../../lib/asyncSafe';
import { errorKind, isRecord } from '../../../../lib/narrow';
import {
    annotationBadges,
    declaredHints,
    isReadOnlyTool,
    UNANNOTATED_WARNING,
} from './toolAnnotations';
import type { HintTone } from './toolAnnotations';

// ── JSON Schema — the subset MCP `inputSchema` actually uses ──────────
export interface JsonSchemaProp {
    type?: string | string[];
    description?: string;
    items?: { type?: string };
    enum?: unknown[];
    default?: unknown;
    minimum?: number;
}
export interface JsonSchema {
    type?: string | string[];
    properties?: Record<string, JsonSchemaProp>;
    required?: string[];
}

export interface CapabilityItem {
    name: string;
    description?: string;
    // Tool-only MCP spec fields — captured at capability discovery and
    // stored verbatim on the server row by gateway-control's canonical
    // tool normaliser. Everything but `inputSchema` is JSON the upstream
    // chose, so it stays `unknown` and is narrowed where it is read.
    title?: string;
    inputSchema?: JsonSchema;
    outputSchema?: unknown;
    annotations?: unknown;
    _meta?: unknown;
}

// ── MCP tool annotations (behaviour hints) — see toolAnnotations.ts ──

const HINT_TONE_CLASS: Record<HintTone, string> = {
    safe: 'bg-green-100 text-green-700',
    risk: 'bg-red-100 text-red-700',
    neutral: 'bg-surface-inset text-secondary',
};

const HintBadges: React.FC<{ tool: CapabilityItem }> = ({ tool }) => {
    const badges = annotationBadges(tool.annotations);
    if (badges === null) {
        return (
            <span
                className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 font-medium"
                title="The tool declares no MCP behaviour hints. Brutor fails closed: it is gated as a write under restricted or approval-required autonomy."
            >
                <AlertTriangle className="w-3 h-3" />
                {UNANNOTATED_WARNING}
            </span>
        );
    }
    return (
        <>
            {badges.map(b => (
                <span
                    key={`${b.key}-${b.label}`}
                    className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${HINT_TONE_CLASS[b.tone]}`}
                >
                    {b.label}
                </span>
            ))}
        </>
    );
};

/** Collapsible pretty-printed JSON (outputSchema, _meta). */
const JsonDetails: React.FC<{ label: string; value: unknown }> = ({ label, value }) => (
    <details className="text-xs">
        <summary className="cursor-pointer font-semibold text-default select-none">{label}</summary>
        <pre className="mt-1.5 bg-surface-inset border border-default rounded-lg p-2.5 text-[11px] leading-relaxed text-default font-mono overflow-x-auto">
            {JSON.stringify(value, null, 2)}
        </pre>
    </details>
);

/** A value worth rendering: present and, for objects, non-empty. */
function hasContent(value: unknown): boolean {
    if (value === undefined || value === null) return false;
    return !isRecord(value) || Object.keys(value).length > 0;
}

export interface Capabilities {
    tools?: CapabilityItem[];
    resources?: CapabilityItem[];
    prompts?: CapabilityItem[];
    resource_templates?: CapabilityItem[];
}

interface Props {
    isOpen: boolean;
    onClose: () => void;
    serverName: string;
    /** Server id — used to build the runtime `tools/call` example URL. */
    serverId: string;
    capabilities: Capabilities | null;
}

type TabType = 'tools' | 'resources' | 'prompts' | 'resource_templates';

const TABS: { type: TabType; label: string; icon: LucideIcon; capKey: keyof Capabilities }[] = [
    { type: 'tools', label: 'Tools', icon: Wrench, capKey: 'tools' },
    { type: 'resources', label: 'Resources', icon: FolderOpen, capKey: 'resources' },
    { type: 'prompts', label: 'Prompts', icon: MessageSquare, capKey: 'prompts' },
    { type: 'resource_templates', label: 'Templates', icon: FileText, capKey: 'resource_templates' },
];

// ── Schema helpers ───────────────────────────────────────────────────

/** Human-readable type label for one schema property. */
function typeLabel(prop: JsonSchemaProp): string {
    const t = Array.isArray(prop.type) ? prop.type.join(' | ') : prop.type;
    if (t === 'array' && prop.items?.type) return `array<${prop.items.type}>`;
    return t || 'any';
}

/** A placeholder example value for one property, keyed by JSON type. */
function exampleValue(name: string, prop: JsonSchemaProp): unknown {
    // A schema-declared default is always the safest example.
    if (prop.default !== undefined) return prop.default;
    if (prop.enum && prop.enum.length > 0) return prop.enum[0];
    const t = Array.isArray(prop.type) ? prop.type[0] : prop.type;
    switch (t) {
        case 'integer':
        case 'number':
            // Honour a declared lower bound; otherwise fall back to 1.
            // `0` is a bad default — count/limit/page-style params
            // very commonly carry a `minimum: 1` constraint that an
            // operator's copy-pasted example would otherwise fail.
            return typeof prop.minimum === 'number' ? prop.minimum : 1;
        case 'boolean':
            return false;
        case 'array':
            return [];
        case 'object':
            return {};
        default:
            // string + anything unknown — use the param name as a hint.
            return `<${name}>`;
    }
}

/**
 * Build a copy-paste `tools/call` curl for this tool. Mirrors the
 * runtime path an operator would actually hit: the gateway-core
 * gateway (`BRUTOR_GATEWAY_PUBLIC_URL`) authenticated with a group-scoped API key.
 */
function buildToolCallCurl(serverId: string, tool: CapabilityItem): string {
    const proxy = env.BRUTOR_GATEWAY_PUBLIC_URL.replace(/\/$/, '');
    const args: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(tool.inputSchema?.properties ?? {})) {
        args[k] = exampleValue(k, v);
    }
    const body = JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: { name: tool.name, arguments: args },
    });
    return [
        `curl -X POST ${proxy}/v1/proxy/mcp/${serverId} \\`,
        `  -H "Content-Type: application/json" \\`,
        `  -H "Accept: application/json, text/event-stream" \\`,
        `  -H "Authorization: Bearer YOUR_API_KEY" \\`,
        `  -d '${body}'`,
    ].join('\n');
}

const CapabilityViewerDialog: React.FC<Props> = ({
    isOpen,
    onClose,
    serverName,
    serverId,
    capabilities,
}) => {
    const [activeTab, setActiveTab] = useState<TabType>('tools');
    const [expanded, setExpanded] = useState<string | null>(null);
    const [copied, setCopied] = useState<string | null>(null);

    if (!isOpen) return null;

    const currentItems = capabilities?.[TABS.find(t => t.type === activeTab)!.capKey] || [];

    const copyCurl = async (toolName: string, curl: string) => {
        try {
            await navigator.clipboard.writeText(curl);
            setCopied(toolName);
            setTimeout(() => setCopied(c => (c === toolName ? null : c)), 2000);
        } catch (err) {
            console.error('Failed to copy curl snippet:', errorKind(err));
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[80vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-5 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <div className="p-2 bg-primary-100 rounded-lg">
                            <Settings2 className="w-5 h-5 text-primary-600" />
                        </div>
                        <div>
                            <h2 className="text-lg font-semibold text-default">Discovered Capabilities</h2>
                            <p className="text-sm text-muted">{serverName}</p>
                        </div>
                    </div>
                    <button onClick={onClose} className="text-muted hover:text-secondary transition-colors">
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Tabs */}
                <div className="flex space-x-1 bg-surface-raised rounded-lg p-1 mx-5 mt-4">
                    {TABS.map(tab => {
                        const Icon = tab.icon;
                        const count = capabilities?.[tab.capKey]?.length || 0;
                        const isActive = activeTab === tab.type;

                        return (
                            <button
                                key={tab.type}
                                onClick={() => {
                                    setActiveTab(tab.type);
                                    setExpanded(null);
                                }}
                                className={`flex items-center space-x-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors flex-1 justify-center min-h-0 min-w-0 ${
                                    isActive
                                        ? 'bg-surface shadow-sm text-primary-600'
                                        : 'text-muted hover:text-secondary'
                                }`}
                            >
                                <Icon className="w-3.5 h-3.5" />
                                <span>{tab.label}</span>
                                <span className={`text-xs px-1.5 py-0.5 rounded-full ${
                                    isActive && count > 0 ? 'bg-primary-100 text-primary-700' : 'bg-surface-inset text-muted'
                                }`}>
                                    {count}
                                </span>
                            </button>
                        );
                    })}
                </div>

                {/* Content */}
                <div className="scroll-region p-5">
                    {currentItems.length === 0 ? (
                        <div className="text-center py-12 text-muted text-sm">
                            No {TABS.find(t => t.type === activeTab)?.label.toLowerCase()} discovered.
                        </div>
                    ) : activeTab === 'tools' ? (
                        <div className="space-y-2">
                            {currentItems.map((tool) => {
                                const isToolOpen = expanded === tool.name;
                                const propEntries = Object.entries(tool.inputSchema?.properties ?? {});
                                const required = new Set(tool.inputSchema?.required ?? []);
                                const curl = buildToolCallCurl(serverId, tool);

                                return (
                                    <div
                                        key={tool.name}
                                        className="rounded-lg border border-default bg-surface-alt overflow-hidden"
                                    >
                                        {/* Header — click to expand */}
                                        <button
                                            onClick={() => setExpanded(isToolOpen ? null : tool.name)}
                                            className="w-full flex items-start space-x-2 px-3 py-2.5 text-left hover:bg-surface-raised transition-colors"
                                        >
                                            {isToolOpen
                                                ? <ChevronDown className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                                : <ChevronRight className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />}
                                            <div className="flex-1 min-w-0 min-h-0">
                                                <span className="text-sm font-mono text-default">{tool.name}</span>
                                                {tool.title && (
                                                    <span className="text-xs text-secondary ml-2">{tool.title}</span>
                                                )}
                                                {tool.description && !isToolOpen && (
                                                    <p className="text-xs text-muted mt-0.5 line-clamp-1">
                                                        {tool.description}
                                                    </p>
                                                )}
                                                <div className="flex flex-wrap gap-1 mt-1">
                                                    <HintBadges tool={tool} />
                                                </div>
                                            </div>
                                            <span className="text-[10px] text-muted flex-shrink-0 mt-0.5 whitespace-nowrap">
                                                {propEntries.length} arg{propEntries.length === 1 ? '' : 's'}
                                            </span>
                                        </button>

                                        {isToolOpen && (
                                            <div className="px-3 pb-3 pt-2 space-y-3 border-t border-default">
                                                {/* tools/call curl snippet — first so the
                                                    operator sees the ready-to-run call
                                                    immediately, before the prose. */}
                                                <div>
                                                    <div className="flex items-center justify-between mb-1.5">
                                                        <h4 className="text-xs font-semibold text-default">
                                                            Call this tool via the API
                                                        </h4>
                                                        <button
                                                            onClick={asyncHandler(() => copyCurl(tool.name, curl), 'click')}
                                                            className="flex items-center space-x-1 px-2 py-1 rounded text-xs bg-primary-100 text-primary-700 hover:bg-primary-200 transition-colors"
                                                        >
                                                            {copied === tool.name
                                                                ? <Check className="w-3 h-3" />
                                                                : <Copy className="w-3 h-3" />}
                                                            <span>{copied === tool.name ? 'Copied!' : 'Copy'}</span>
                                                        </button>
                                                    </div>
                                                    <pre className="bg-surface-inset border border-default rounded-lg p-2.5 text-[11px] leading-relaxed text-default font-mono overflow-x-auto">{curl}</pre>
                                                    <p className="text-[10px] text-muted mt-1 leading-relaxed">
                                                        Replace <span className="font-mono">YOUR_API_KEY</span> with a
                                                        Brutor API key scoped to a resource group that has this server
                                                        assigned. Placeholder values (<span className="font-mono">&lt;name&gt;</span>)
                                                        are examples — substitute real arguments before sending.
                                                    </p>
                                                </div>

                                                {/* Arguments */}
                                                <div>
                                                    <h4 className="text-xs font-semibold text-default mb-1.5">
                                                        Arguments
                                                    </h4>
                                                    {propEntries.length === 0 ? (
                                                        <p className="text-xs text-muted">
                                                            This tool takes no arguments.
                                                        </p>
                                                    ) : (
                                                        <div className="space-y-2">
                                                            {propEntries.map(([pname, pschema]) => (
                                                                <div key={pname} className="text-xs">
                                                                    <div className="flex items-center flex-wrap gap-x-2 gap-y-0.5">
                                                                        <span className="font-mono text-default">
                                                                            {pname}
                                                                        </span>
                                                                        <span className="text-muted font-mono">
                                                                            {typeLabel(pschema)}
                                                                        </span>
                                                                        {required.has(pname) && (
                                                                            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 font-medium">
                                                                                required
                                                                            </span>
                                                                        )}
                                                                    </div>
                                                                    {pschema.description && (
                                                                        <p className="text-muted mt-0.5">
                                                                            {pschema.description}
                                                                        </p>
                                                                    )}
                                                                </div>
                                                            ))}
                                                        </div>
                                                    )}
                                                </div>

                                                {/* Behaviour hints — what the runtime gates on. */}
                                                <div>
                                                    <h4 className="text-xs font-semibold text-default mb-1.5">
                                                        Behaviour hints (MCP annotations)
                                                    </h4>
                                                    {declaredHints(tool.annotations).length === 0 ? (
                                                        <p className="text-xs text-amber-700">
                                                            {UNANNOTATED_WARNING}.
                                                        </p>
                                                    ) : (
                                                        <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                                                            {declaredHints(tool.annotations).map(([k, v]) => (
                                                                <span key={k} className="text-xs font-mono text-secondary">
                                                                    {k}: {String(v)}
                                                                </span>
                                                            ))}
                                                        </div>
                                                    )}
                                                    <p className="text-[10px] text-muted mt-1 leading-relaxed">
                                                        {isReadOnlyTool(tool.annotations)
                                                            ? 'Runtime classification: read-only — allowed under restricted autonomy.'
                                                            : 'Runtime classification: side-effecting — held for approval or denied under approval-required / restricted autonomy.'}
                                                    </p>
                                                </div>

                                                {hasContent(tool.outputSchema) && (
                                                    <JsonDetails label="Output schema" value={tool.outputSchema} />
                                                )}
                                                {hasContent(tool._meta) && (
                                                    <JsonDetails label="Metadata (_meta)" value={tool._meta} />
                                                )}

                                                {/* Full description — last; background
                                                    context once the call shape is clear. */}
                                                {tool.description && (
                                                    <div>
                                                        <h4 className="text-xs font-semibold text-default mb-1.5">
                                                            Description
                                                        </h4>
                                                        <p className="text-xs text-secondary whitespace-pre-wrap leading-relaxed">
                                                            {tool.description}
                                                        </p>
                                                    </div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    ) : (
                        <div className="space-y-1">
                            {currentItems.map((item) => (
                                <div
                                    key={item.name}
                                    className="px-3 py-2 rounded-lg bg-surface-alt"
                                >
                                    <span className="text-sm font-mono text-default">{item.name}</span>
                                    {item.description && (
                                        <p className="text-xs text-muted mt-0.5 whitespace-pre-wrap">
                                            {item.description}
                                        </p>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="flex justify-end p-5 border-t border-default">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default CapabilityViewerDialog;
