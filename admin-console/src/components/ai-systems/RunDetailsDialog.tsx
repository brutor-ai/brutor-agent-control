/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Everything the ledger holds about one run, in one window.
 *
 * The runs table is a one-line summary per run and deliberately hides most
 * columns. This dialog is the other end of that trade: every field the
 * rollup carries — identity, the contract that governed it, timing, shape,
 * cost, governance counters, provenance — grouped the way an operator asks
 * about them, with the raw JSON one click away for anything the grouping
 * did not anticipate. Fields are rendered from the record itself, so a new
 * ledger column shows up here without a UI change.
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, Copy, Square, Workflow, X } from 'lucide-react';

import type { OutcomeConformance, RunDetail, RunSummary } from '../../lib/aiSystemTypes';
import { humanDurationMs } from '../../lib/aiSystemTypes';
import AbortRunDialog from './AbortRunDialog';
import RecordDialog from '../compliance/RecordDialog';
import RunEvidenceSection from './RunEvidenceSection';
import { AgentReleaseChip } from '../agent/AgentReleaseBadges';
import { runRelease } from '../../lib/agentRelease';
import { ErrorCauseText, RunClassChip, RunOutcomeCell } from './RunOutcomeBadges';
import { RUN_CLASS_LABELS, errorCauseLabel, isRunClass } from '../../lib/runOutcomes';

interface RunDetailsDialogProps {
    run: RunSummary;
    /** Loads the full rollup; the summary row is shown until it arrives. */
    fetchRun?: (id: string) => Promise<RunDetail>;
    /** Stop this run. Omitted when the caller has no
     *  abort permission, which is why it is optional rather than disabled:
     *  a control you cannot use should not be on screen at all. */
    onAbort?: (rootTaskId: string, reason: string) => Promise<void>;
    /** Open the Composition tab's Simple graph with this run applied (the
     *  run overlay reads the same action projection as this dialog). */
    onShowOnGraph?: () => void;
    onClose: () => void;
}

/** Field groups in the order an operator reads them. Anything on the record
 *  that no group names lands in "Other", so nothing is silently dropped. */
const GROUPS: Array<{ title: string; keys: string[] }> = [
    {
        title: 'Identity',
        keys: ['id', 'root_task_id', 'ai_system_id', 'ai_system_name', 'system_kind', 'via_system_ids', 'client_label', 'is_replay'],
    },
    {
        title: 'Who',
        keys: ['actor_type', 'actor_id', 'agent_id', 'subject_kind', 'subject_id', 'trace_root_kind'],
    },
    // WHAT software ran it and WHICH release (never who: that is
    // the credential above), with the trust the release came in on.
    {
        title: 'Implementation',
        keys: ['agent_impl_name', 'agent_impl_version', 'agent_impl_build', 'agent_impl_trust', 'implementation_fingerprint'],
    },
    {
        title: 'Contract that governed it',
        keys: ['contract_id', 'contract_hash'],
    },
    {
        title: 'When',
        keys: ['started_at', 'ended_at', 'duration_ms', 'active_duration_ms', 'awaiting_human_ms', 'created_at', 'terminal_state', 'finalized_by', 'reopened_count'],
    },
    // The three questions: how the execution ended (and why it
    // failed), whether the reported outcome is declared, and whether it
    // counts as work; the run class combines them.
    {
        title: 'Outcome',
        keys: [
            'run_class', 'outcome_id', 'outcome_conformance', 'outcome_counts_toward_baseline',
            'outcome_contract_version', 'error_cause',
        ],
    },
    {
        title: 'Shape',
        keys: [
            'action_count', 'step_count', 'turn_count', 'turn_attribution', 'llm_call_count', 'tool_call_count',
            'skill_call_count', 'a2a_call_count', 'max_delegation_depth', 'distinct_tools', 'tool_signature',
            'tool_set_signature', 'tools_used', 'models_used',
        ],
    },
    // Whether the run declared which records it works on, how many
    // distinct records its tool calls named, and the calls that went outside
    // that scope (or named records with no scope declared). Null = the run
    // was finalized before data scope existed.
    {
        title: 'Data scope',
        keys: ['scope_declared', 'records_touched', 'scope_violation_count', 'scope_unbound_count'],
    },
    {
        title: 'Cost and tokens',
        keys: [
            'total_cost_usd', 'total_tokens', 'prompt_tokens', 'completion_tokens', 'first_prompt_tokens',
            'peak_context_tokens', 'peak_context_utilization', 'context_growth',
            'cache_hit_count', 'tokens_saved', 'input_bytes', 'output_bytes',
        ],
    },
    {
        title: 'Governance',
        keys: [
            'error_count', 'tool_error_count', 'tool_error_types', 'retry_count', 'fallback_count',
            'guardrail_trip_count', 'guardrail_block_count', 'policy_denial_count', 'approval_request_count',
            'is_successful', 'is_baseline_eligible', 'is_fragmented',
        ],
    },
    {
        title: 'Evidence and provenance',
        keys: ['chain_integrity', 'source', 'prompt_fingerprint', 'rag_used', 'rag_chunks', 'rag_collection_ids', 'feedback_score'],
    },
    // The sealed action record this run's terminal verdict
    // produced. Rendered with a "View record" button when present.
    {
        title: 'Evidence record',
        keys: ['record_id'],
    },
];

const HIDDEN = new Set(['actions']);

const DURATION_KEYS = new Set(['duration_ms', 'active_duration_ms', 'awaiting_human_ms']);

function fmt(key: string, v: unknown): string {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (key === 'total_cost_usd' && typeof v === 'number') return `$${v.toFixed(6)}`;
    if (key === 'error_cause' && typeof v === 'string') return `${errorCauseLabel(v)} (${v})`;
    if (key === 'run_class' && isRunClass(v)) return `${RUN_CLASS_LABELS[v]} (${v})`;
    if (key === 'outcome_contract_version' && typeof v === 'number') return `v${v}`;
    // Elapsed and active side by side, each with a readable span: an elapsed
    // figure alone makes a slow approval look like a slow system.
    if (DURATION_KEYS.has(key) && typeof v === 'number') {
        return v >= 1000 ? `${humanDurationMs(v)} (${v.toLocaleString()} ms)` : `${v.toLocaleString()} ms`;
    }
    if (/_at$/.test(key) && typeof v === 'string') {
        const d = new Date(v);
        return Number.isNaN(d.getTime()) ? v : `${d.toLocaleString()} (${v})`;
    }
    if (Array.isArray(v)) return v.length === 0 ? '—' : v.map((x) => (typeof x === 'object' ? JSON.stringify(x) : String(x))).join(', ');
    if (typeof v === 'object') return JSON.stringify(v);
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'bigint') return v.toString();
    return typeof v;
}

/** The release as every other surface names it (name@version, build,
 *  trust), and the channel it came through: the normalised client label. */
function RunReleaseLine({ record }: { record: Record<string, unknown> }) {
    const release = runRelease(record);
    const client = typeof record.client_label === 'string' && record.client_label ? record.client_label : null;
    return (
        <p className="mb-1 flex flex-wrap items-center gap-1.5 text-xs" data-testid="run-details-release">
            {release ? (
                <AgentReleaseChip release={release} showApproval={false} />
            ) : (
                <span className="italic text-muted">No release declared on this run.</span>
            )}
            {client && (
                <span className="text-muted" data-testid="run-details-client">
                    via client <code className="font-mono text-default">{client}</code>
                </span>
            )}
        </p>
    );
}

/** The outcome at a glance: run class, the reported outcome with its
 *  conformance and baseline marker, and the error cause when it failed. */
function RunOutcomeLine({ record }: { record: Record<string, unknown> }) {
    const str = (k: string) => (typeof record[k] === 'string' ? record[k] : null);
    const conformance = str('outcome_conformance') as OutcomeConformance | null;
    const counts = typeof record.outcome_counts_toward_baseline === 'boolean' ? record.outcome_counts_toward_baseline : null;
    return (
        <p className="mb-1 flex flex-wrap items-center gap-2 text-xs" data-testid="run-details-outcome">
            <RunClassChip cls={str('run_class')} testId="run-details-class" />
            <RunOutcomeCell
                run={{
                    id: typeof record.id === 'string' ? record.id : 'run',
                    outcome_id: str('outcome_id'),
                    outcome_conformance: conformance,
                    outcome_counts_toward_baseline: counts,
                }}
            />
            {str('error_cause') && <ErrorCauseText cause={str('error_cause')} testId="run-details-error-cause" />}
        </p>
    );
}

export default function RunDetailsDialog({ run, fetchRun, onAbort, onShowOnGraph, onClose }: RunDetailsDialogProps) {
    const [detail, setDetail] = useState<RunDetail | null>(null);
    const [error, setError] = useState('');
    const [raw, setRaw] = useState(false);
    const [aborting, setAborting] = useState(false);
    const [copied, setCopied] = useState(false);
    const [recordOpen, setRecordOpen] = useState(false);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onClose]);

    useEffect(() => {
        let cancelled = false;
        if (!fetchRun) return;
        fetchRun(run.id)
            .then((d) => {
                if (!cancelled) setDetail(d);
            })
            .catch((err: unknown) => {
                if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load run');
            });
        return () => {
            cancelled = true;
        };
    }, [fetchRun, run.id]);

    const record: Record<string, unknown> = { ...run, ...(detail ?? {}) };
    const named = new Set(GROUPS.flatMap((g) => g.keys));
    const other = Object.keys(record)
        .filter((k) => !named.has(k) && !HIDDEN.has(k))
        .sort();
    const groups = [...GROUPS, ...(other.length ? [{ title: 'Other', keys: other }] : [])];
    const printable = Object.fromEntries(Object.entries(record).filter(([k]) => !HIDDEN.has(k)));
    const recordId = typeof record.record_id === 'string' && record.record_id ? record.record_id : null;
    const evidenceRoot = run.root_task_id || null;

    const copyJson = () => {
        void (async () => {
            try {
                await navigator.clipboard.writeText(JSON.stringify(printable, null, 2));
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
            } catch {
                // Clipboard denied; the JSON view is on screen either way.
            }
        })();
    };

    return createPortal(
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            role="dialog"
            aria-modal="true"
            aria-label={`Run ${run.id} details`}
            data-testid="run-details-dialog"
            onMouseDown={(e) => {
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <div className="bg-surface border border-default rounded-xl shadow-xl w-full max-w-3xl max-h-[85vh] flex flex-col">
                <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-default">
                    <div className="min-w-0">
                        <h2 className="text-sm font-semibold text-default">Run details</h2>
                        <p className="font-mono text-xs text-secondary truncate" title={run.id}>
                            {run.id}
                        </p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        {onAbort && !run.terminal_state && (
                            <button
                                type="button"
                                onClick={() => setAborting(true)}
                                className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-red-700 border border-red-300 rounded-lg hover:bg-red-50 dark:border-red-800 dark:hover:bg-red-950/40"
                                data-testid="run-details-abort"
                            >
                                <Square className="w-3.5 h-3.5" />
                                Stop run
                            </button>
                        )}
                        {onShowOnGraph && (
                            <button
                                type="button"
                                onClick={() => {
                                    onShowOnGraph();
                                    onClose();
                                }}
                                className="inline-flex items-center gap-1 px-2.5 py-1 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                                data-testid="run-details-show-on-graph"
                            >
                                <Workflow className="w-3.5 h-3.5" />
                                Show on graph
                            </button>
                        )}
                        <button
                            type="button"
                            onClick={() => setRaw((r) => !r)}
                            className="px-2.5 py-1 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                            data-testid="run-details-raw"
                        >
                            {raw ? 'Grouped view' : 'Raw JSON'}
                        </button>
                        <button
                            type="button"
                            onClick={copyJson}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                            data-testid="run-details-copy"
                        >
                            {copied ? <Check className="w-3.5 h-3.5 text-green-600" /> : <Copy className="w-3.5 h-3.5" />}
                            Copy JSON
                        </button>
                        <button
                            type="button"
                            onClick={onClose}
                            aria-label="Close run details"
                            className="p-1 rounded hover:bg-surface-raised text-muted"
                            data-testid="run-details-close"
                        >
                            <X className="w-5 h-5" />
                        </button>
                    </div>
                </div>

                {recordOpen && recordId && (
                    <RecordDialog recordId={recordId} onClose={() => setRecordOpen(false)} />
                )}

                {aborting && onAbort && (
                    <AbortRunDialog
                        rootTaskId={run.root_task_id ?? run.id}
                        aiSystemName={run.ai_system_name}
                        onConfirm={async (reason) => {
                            await onAbort(run.root_task_id ?? run.id, reason);
                            setAborting(false);
                        }}
                        onCancel={() => setAborting(false)}
                    />
                )}

                <div className="overflow-y-auto px-5 py-4 text-sm">
                    {error && (
                        <p className="mb-3 text-xs text-red-700" data-testid="run-details-error">
                            {error} — showing the summary row only.
                        </p>
                    )}
                    {!error && fetchRun && !detail && (
                        <p className="mb-3 text-xs text-muted">Loading the full rollup…</p>
                    )}
                    {raw ? (
                        <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-surface-alt border border-default rounded p-3" data-testid="run-details-json">
                            {JSON.stringify(printable, null, 2)}
                        </pre>
                    ) : (
                        <div className="space-y-4">
                            {groups.map((g) => {
                                const rows = g.keys.filter((k) => k in record);
                                if (rows.length === 0) return null;
                                return (
                                    <section key={g.title} data-testid={`run-details-group-${g.title.toLowerCase().replace(/[^a-z]+/g, '-')}`}>
                                        <h3 className="text-xs font-semibold text-default mb-1">{g.title}</h3>
                                        {g.title === 'Implementation' && <RunReleaseLine record={record} />}
                                        {g.title === 'Outcome' && <RunOutcomeLine record={record} />}
                                        <dl className="grid grid-cols-[minmax(160px,1fr)_2fr] gap-x-4 gap-y-1">
                                            {rows.map((k) => (
                                                <div key={k} className="contents">
                                                    <dt className="text-xs text-muted font-mono">{k}</dt>
                                                    <dd className="text-xs text-default break-all" data-testid={`run-field-${k}`}>
                                                        {fmt(k, record[k])}
                                                        {k === 'record_id' && recordId && (
                                                            <button
                                                                type="button"
                                                                onClick={() => setRecordOpen(true)}
                                                                className="ml-2 px-2 py-0.5 text-[11px] border border-strong rounded hover:bg-surface-hover"
                                                                data-testid="run-details-view-record"
                                                            >
                                                                View record
                                                            </button>
                                                        )}
                                                    </dd>
                                                </div>
                                            ))}
                                        </dl>
                                    </section>
                                );
                            })}
                            {/* Each step's sealed record, chain links, bundle / verify. */}
                            {evidenceRoot && <RunEvidenceSection rootTaskId={evidenceRoot} />}
                        </div>
                    )}
                </div>
            </div>
        </div>,
        document.body,
    );
}
