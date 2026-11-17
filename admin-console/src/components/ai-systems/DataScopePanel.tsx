/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Data scope and record reach.
 *
 * **Data scope** says which tool argument names which kind of record, so the
 * gateway can tell whether a run's tool call stays inside the records the
 * run declared it works on. `audit` records a call outside the scope and lets
 * it through; `enforce` refuses it. The 7-day activity table sits under the
 * config on purpose: it shows what `enforce` WOULD refuse, so the switch is
 * made from data rather than hope.
 *
 * **Reach rules** limit how many distinct records one requester (or key,
 * agent, the whole system, or a scope value) reaches across runs in a window.
 * The preview compares a draft limit with the last 30 days before it is
 * saved, so a limit is set from the observed p95/p99 rather than guessed.
 */

import { useCallback, useMemo, useState } from 'react';
import { AlertTriangle, Eye, Plus, RefreshCw, Trash2 } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_THEAD, TABLE_TH_TYPE } from '../ui/tableStyles';
import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import type {
    DataScopeActivity,
    DataScopeConfig,
    DataScopeConfigBody,
    DataScopeKeySource,
    DataScopeMode,
    DataScopeOnUnbound,
    ReachPreview,
    ReachRule,
    ReachRuleBody,
    ReachRuleSeverity,
} from '../../lib/aiSystemTypes';
import { errorMessage } from '../../lib/narrow';

interface DataScopePanelProps {
    aiSystemId: string;
}

const MODES: { id: DataScopeMode; label: string }[] = [
    { id: 'off', label: 'Off' },
    { id: 'audit', label: 'Audit (record, let through)' },
    { id: 'enforce', label: 'Enforce (refuse out-of-scope calls)' },
];

const ON_UNBOUND: { id: DataScopeOnUnbound; label: string }[] = [
    { id: 'deny', label: 'Treat as a violation' },
    { id: 'allow', label: 'Allow' },
];

const SOURCES: { id: DataScopeKeySource; label: string }[] = [
    { id: 'declared', label: 'Declared by the run' },
    { id: 'subject', label: 'The person the agent acts for' },
];

/** Column order of the activity table, and how loudly each verdict reads. */
const VERDICTS: { id: string; label: string; tone: 'ok' | 'warn' | 'bad' | 'muted' }[] = [
    { id: 'in_scope', label: 'In scope', tone: 'ok' },
    { id: 'out_of_scope', label: 'Out of scope', tone: 'bad' },
    { id: 'conflict', label: 'Conflict', tone: 'bad' },
    { id: 'unbound', label: 'No scope declared', tone: 'warn' },
    { id: 'missing_argument', label: 'Argument missing', tone: 'warn' },
    { id: 'unscoped_tool', label: 'Unscoped tool', tone: 'muted' },
];

const TONE_CLASS: Record<string, string> = {
    ok: 'text-default',
    warn: 'text-amber-800 bg-amber-50 font-medium',
    bad: 'text-red-800 bg-red-50 font-semibold',
    muted: 'text-muted',
};

const FIXED_GROUP_BY: { id: string; label: string }[] = [
    { id: 'subject', label: 'Per person (subject)' },
    { id: 'api_key', label: 'Per API key' },
    { id: 'agent', label: 'Per agent' },
    { id: 'system', label: 'Whole system' },
];

const SEVERITIES: ReachRuleSeverity[] = ['low', 'medium', 'high', 'critical'];

const EMPTY_CONFIG: DataScopeConfigBody = {
    mode: 'off',
    on_unbound: 'deny',
    keys: [],
    rules: [],
    unscoped_tools: [],
};

const INPUT = 'px-2 py-1 text-xs border border-strong rounded bg-surface';
const BUTTON = 'px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50';

interface Loaded {
    config: DataScopeConfig;
    activity: DataScopeActivity;
    rules: ReachRule[];
}

function bodyOf(c: DataScopeConfig): DataScopeConfigBody {
    return {
        mode: c.mode,
        on_unbound: c.on_unbound,
        keys: c.keys,
        rules: c.rules,
        unscoped_tools: c.unscoped_tools,
    };
}

function groupByLabel(groupBy: string): string {
    const fixed = FIXED_GROUP_BY.find((g) => g.id === groupBy);
    if (fixed) return fixed.label;
    return groupBy.startsWith('scope:') ? `Per ${groupBy.slice('scope:'.length)}` : groupBy;
}

export function DataScopePanel({ aiSystemId }: DataScopePanelProps) {
    const {
        fetchDataScope,
        updateDataScope,
        fetchDataScopeActivity,
        fetchReachRules,
        createReachRule,
        updateReachRule,
        deleteReachRule,
        previewReachRule,
    } = useGateway();

    const loader = useCallback(async (): Promise<Loaded> => {
        const [config, activity, reach] = await Promise.all([
            fetchDataScope(aiSystemId),
            fetchDataScopeActivity(aiSystemId, 7),
            fetchReachRules(aiSystemId),
        ]);
        return { config, activity, rules: reach.rules };
    }, [aiSystemId, fetchDataScope, fetchDataScopeActivity, fetchReachRules]);

    // The loaded config lives in the hook; unsaved edits are an overlay on
    // it, keyed by system so switching systems never carries a draft across.
    const { data, error, loading, reload, setData } = useKeyedLoad(aiSystemId, loader, 'Failed to load the data scope');
    const [edit, setEdit] = useState<{ key: string; draft: DataScopeConfigBody; unscopedText: string } | null>(null);
    const [saveError, setSaveError] = useState('');
    const [warnings, setWarnings] = useState<string[]>([]);
    const [saved, setSaved] = useState(false);
    const [busy, setBusy] = useState('');

    const stored = data?.config;
    const editing = edit?.key === aiSystemId ? edit : null;
    const draft: DataScopeConfigBody = editing?.draft ?? (stored ? bodyOf(stored) : EMPTY_CONFIG);
    const unscopedText = editing?.unscopedText ?? (stored?.unscoped_tools ?? []).join(', ');
    const configured = stored?.configured ?? false;
    const version = stored?.version ?? 0;
    const activity = data?.activity ?? null;
    const rules = data?.rules ?? [];

    const save = async () => {
        try {
            setBusy('save');
            setSaveError('');
            setSaved(false);
            const unscoped_tools = unscopedText
                .split(',')
                .map((t) => t.trim())
                .filter(Boolean);
            const result = await updateDataScope(aiSystemId, { ...draft, unscoped_tools });
            setData((prev) => (prev ? { ...prev, config: result } : prev));
            setEdit(null);
            setWarnings(result.warnings ?? []);
            setSaved(true);
        } catch (err: unknown) {
            setWarnings([]);
            setSaveError(errorMessage(err, 'Could not save the data scope'));
        } finally {
            setBusy('');
        }
    };

    const patch = (p: Partial<DataScopeConfigBody>, text = unscopedText) => {
        setSaved(false);
        setEdit({ key: aiSystemId, draft: { ...draft, ...p }, unscopedText: text });
    };

    const keyNames = draft.keys.map((k) => k.key).filter(Boolean);

    return (
        <div className="space-y-4" data-testid="data-scope-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Data scope</h3>
                    <HelpTip text="Which tool argument names which kind of record, so a run's tool calls can be checked against the records it declared it works on. Audit records an out-of-scope call; enforce refuses it. The core applies a change within 30 seconds." />
                </div>
                <button
                    type="button"
                    onClick={reload}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded" data-testid="data-scope-error">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {/* --- config ---------------------------------------------------- */}
            <div className="bg-surface border border-default rounded-lg p-4 space-y-3" data-testid="data-scope-config">
                <p className="text-xs text-muted" data-testid="data-scope-status">
                    {configured ? `Version ${version}` : 'Not configured — no tool call is checked against a scope.'}
                </p>

                <div className="flex flex-wrap gap-3">
                    <label className="text-xs text-muted">
                        Mode
                        <select
                            value={draft.mode}
                            onChange={(e) => patch({ mode: e.target.value as DataScopeMode })}
                            aria-label="Data scope mode"
                            className={`ml-2 ${INPUT}`}
                            data-testid="data-scope-mode"
                        >
                            {MODES.map((m) => (
                                <option key={m.id} value={m.id}>
                                    {m.label}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="text-xs text-muted">
                        A record named while the run declared no scope
                        <select
                            value={draft.on_unbound}
                            onChange={(e) => patch({ on_unbound: e.target.value as DataScopeOnUnbound })}
                            aria-label="On unbound"
                            className={`ml-2 ${INPUT}`}
                            data-testid="data-scope-on-unbound"
                        >
                            {ON_UNBOUND.map((m) => (
                                <option key={m.id} value={m.id}>
                                    {m.label}
                                </option>
                            ))}
                        </select>
                    </label>
                </div>

                {/* Keys: the kinds of record a run can declare. */}
                <div className="space-y-1.5">
                    <p className="text-xs font-medium text-default">Record kinds (keys)</p>
                    {draft.keys.map((k, i) => (
                        <div key={i} className="flex flex-wrap items-center gap-2" data-testid="data-scope-key-row">
                            <input
                                type="text"
                                value={k.key}
                                placeholder="e.g. customer_id"
                                aria-label={`Key ${i + 1}`}
                                onChange={(e) =>
                                    patch({ keys: draft.keys.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)) })
                                }
                                className={`${INPUT} font-mono`}
                                data-testid="data-scope-key-name"
                            />
                            <select
                                value={k.source}
                                aria-label={`Key ${i + 1} source`}
                                onChange={(e) =>
                                    patch({
                                        keys: draft.keys.map((x, j) =>
                                            j === i ? { ...x, source: e.target.value as DataScopeKeySource } : x,
                                        ),
                                    })
                                }
                                className={INPUT}
                                data-testid="data-scope-key-source"
                            >
                                {SOURCES.map((s) => (
                                    <option key={s.id} value={s.id}>
                                        {s.label}
                                    </option>
                                ))}
                            </select>
                            <button
                                type="button"
                                aria-label={`Remove key ${i + 1}`}
                                onClick={() => patch({ keys: draft.keys.filter((_, j) => j !== i) })}
                                className="p-1 border border-strong rounded hover:bg-surface-hover"
                            >
                                <Trash2 className="w-3.5 h-3.5" />
                            </button>
                        </div>
                    ))}
                    <button
                        type="button"
                        onClick={() => patch({ keys: [...draft.keys, { key: '', source: 'declared' }] })}
                        className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline"
                        data-testid="data-scope-add-key"
                    >
                        <Plus className="w-3 h-3" /> Add a key
                    </button>
                </div>

                {/* Rules: which tool argument names which kind of record. */}
                <div className="space-y-1.5">
                    <p className="text-xs font-medium text-default">Tool arguments that name records</p>
                    <p className="text-xs text-muted" data-testid="data-scope-tool-names">
                        Tool: an MCP tool name, <code className="font-mono">skill:&lt;skill name&gt;</code>,{' '}
                        <code className="font-mono">a2a:&lt;agent card name&gt;</code>, or{' '}
                        <code className="font-mono">model:&lt;function name&gt;</code> for a tool call the model
                        asks for, which the agent may run outside Brutor.
                    </p>
                    {draft.rules.length > 0 && (
                        <div className={TABLE_CONTAINER}>
                            <table className="w-full text-xs">
                                <thead className={TABLE_THEAD}>
                                    <tr>
                                        <th className={`${TABLE_TH_TYPE} text-left px-2 py-1.5`}>Tool</th>
                                        <th className={`${TABLE_TH_TYPE} text-left px-2 py-1.5`}>Argument</th>
                                        <th className={`${TABLE_TH_TYPE} text-left px-2 py-1.5`}>Key</th>
                                        <th className="px-2 py-1.5" />
                                    </tr>
                                </thead>
                                <tbody className={TABLE_TBODY}>
                                    {draft.rules.map((r, i) => {
                                        const set = (p: Partial<typeof r>) =>
                                            patch({ rules: draft.rules.map((x, j) => (j === i ? { ...x, ...p } : x)) });
                                        return (
                                            <tr key={i} data-testid="data-scope-rule-row">
                                                <td className="px-2 py-1">
                                                    <input
                                                        type="text"
                                                        value={r.tool}
                                                        aria-label={`Rule ${i + 1} tool`}
                                                        onChange={(e) => set({ tool: e.target.value })}
                                                        className={`${INPUT} w-full font-mono`}
                                                    />
                                                </td>
                                                <td className="px-2 py-1">
                                                    <input
                                                        type="text"
                                                        value={r.argument}
                                                        aria-label={`Rule ${i + 1} argument`}
                                                        onChange={(e) => set({ argument: e.target.value })}
                                                        className={`${INPUT} w-full font-mono`}
                                                    />
                                                </td>
                                                <td className="px-2 py-1">
                                                    <select
                                                        value={r.key}
                                                        aria-label={`Rule ${i + 1} key`}
                                                        onChange={(e) => set({ key: e.target.value })}
                                                        className={`${INPUT} w-full`}
                                                    >
                                                        {!keyNames.includes(r.key) && <option value={r.key}>{r.key || '—'}</option>}
                                                        {keyNames.map((k) => (
                                                            <option key={k} value={k}>
                                                                {k}
                                                            </option>
                                                        ))}
                                                    </select>
                                                </td>
                                                <td className="px-2 py-1 text-right">
                                                    <button
                                                        type="button"
                                                        aria-label={`Remove rule ${i + 1}`}
                                                        onClick={() => patch({ rules: draft.rules.filter((_, j) => j !== i) })}
                                                        className="p-1 border border-strong rounded hover:bg-surface-hover"
                                                    >
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    </button>
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    )}
                    <button
                        type="button"
                        onClick={() => patch({ rules: [...draft.rules, { tool: '', argument: '', key: keyNames[0] ?? '' }] })}
                        className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline"
                        data-testid="data-scope-add-rule"
                    >
                        <Plus className="w-3 h-3" /> Add a rule
                    </button>
                </div>

                <label className="block text-xs text-muted">
                    Tools that name no records (comma-separated)
                    <input
                        type="text"
                        value={unscopedText}
                        onChange={(e) => patch({}, e.target.value)}
                        placeholder="e.g. search_docs, get_weather"
                        aria-label="Unscoped tools"
                        className={`mt-1 w-full ${INPUT} font-mono`}
                        data-testid="data-scope-unscoped"
                    />
                </label>

                {saveError && (
                    <p className="flex items-start gap-1.5 text-xs text-red-700" data-testid="data-scope-save-error">
                        <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                        {saveError}
                    </p>
                )}
                {warnings.map((w, i) => (
                    <p key={i} className="flex items-start gap-1.5 text-xs text-amber-800" data-testid="data-scope-warning">
                        <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                        {w}
                    </p>
                ))}

                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        disabled={busy === 'save'}
                        onClick={() => void save()}
                        className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                        data-testid="data-scope-save"
                    >
                        Save data scope
                    </button>
                    {saved && (
                        <span className="text-xs text-green-700" data-testid="data-scope-saved">
                            Saved — the gateway applies it within 30 seconds.
                        </span>
                    )}
                </div>
            </div>

            {activity && <ActivityTable activity={activity} />}

            <ReachRules
                aiSystemId={aiSystemId}
                rules={rules}
                keyNames={keyNames}
                busy={busy}
                setBusy={setBusy}
                reload={reload}
                api={{ createReachRule, updateReachRule, deleteReachRule, previewReachRule }}
            />
        </div>
    );
}

/** Tool × verdict counts; anything enforce would refuse is highlighted. */
function ActivityTable({ activity }: { activity: DataScopeActivity }) {
    const { tools, counts } = useMemo(() => {
        const c = new Map<string, Map<string, number>>();
        for (const r of activity.rows) {
            const row = c.get(r.tool) ?? new Map<string, number>();
            row.set(r.verdict, (row.get(r.verdict) ?? 0) + r.count);
            c.set(r.tool, row);
        }
        return { tools: [...c.keys()].sort(), counts: c };
    }, [activity]);
    // A verdict the UI does not know yet still gets a column.
    const extra = [...new Set(activity.rows.map((r) => r.verdict))].filter((v) => !VERDICTS.some((x) => x.id === v));
    const columns = [...VERDICTS, ...extra.map((id) => ({ id, label: id, tone: 'muted' as const }))];

    return (
        <div className="space-y-1.5" data-testid="data-scope-activity">
            <p className="text-xs font-medium text-default">Last {activity.days} days of tool calls</p>
            {tools.length === 0 ? (
                <p className="text-xs text-muted" data-testid="data-scope-activity-empty">
                    No tool call named a record in this window.
                </p>
            ) : (
                <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                    <table className="w-full text-xs">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1.5`}>Tool</th>
                                {columns.map((v) => (
                                    <th key={v.id} className={`${TABLE_TH_TYPE} text-right px-2 py-1.5`}>
                                        {v.label}
                                    </th>
                                ))}
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {tools.map((t) => (
                                <tr key={t} data-testid={`data-scope-activity-${t}`}>
                                    <td className="px-2 py-1 font-mono text-default">{t}</td>
                                    {columns.map((v) => {
                                        const n = counts.get(t)?.get(v.id) ?? 0;
                                        return (
                                            <td
                                                key={v.id}
                                                className={`px-2 py-1 text-right tabular-nums ${n > 0 ? TONE_CLASS[v.tone] : 'text-muted'}`}
                                                data-testid={n > 0 && v.tone === 'bad' ? 'data-scope-violation' : undefined}
                                            >
                                                {n > 0 ? n.toLocaleString() : '·'}
                                            </td>
                                        );
                                    })}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}

interface ReachApi {
    createReachRule: (aiSystemId: string, body: ReachRuleBody) => Promise<ReachRule>;
    updateReachRule: (aiSystemId: string, ruleId: string, body: Partial<ReachRuleBody>) => Promise<ReachRule>;
    deleteReachRule: (aiSystemId: string, ruleId: string) => Promise<unknown>;
    previewReachRule: (aiSystemId: string, body: ReachRuleBody) => Promise<ReachPreview>;
}

function ReachRules({
    aiSystemId,
    rules,
    keyNames,
    busy,
    setBusy,
    reload,
    api,
}: {
    aiSystemId: string;
    rules: ReachRule[];
    keyNames: string[];
    busy: string;
    setBusy: (b: string) => void;
    reload: () => void;
    api: ReachApi;
}) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState<ReachRuleBody>({
        scope_key: '',
        group_by: 'subject',
        window_hours: 24,
        max_distinct: 50,
        severity: 'high',
        enabled: true,
    });
    const [preview, setPreview] = useState<ReachPreview | null>(null);
    const [formError, setFormError] = useState('');
    const [rowError, setRowError] = useState('');

    const scopeKey = form.scope_key || keyNames[0] || '';
    const body: ReachRuleBody = { ...form, scope_key: scopeKey };
    const groupByOptions = [
        ...FIXED_GROUP_BY,
        ...keyNames.map((k) => ({ id: `scope:${k}`, label: `Per ${k} (scope value)` })),
    ];

    const update = (p: Partial<ReachRuleBody>) => {
        setPreview(null);
        setForm((f) => ({ ...f, ...p }));
    };

    const run = async (tag: string, fn: () => Promise<void>, onError: (m: string) => void, fallback: string) => {
        try {
            setBusy(tag);
            onError('');
            await fn();
        } catch (err: unknown) {
            onError(errorMessage(err, fallback));
        } finally {
            setBusy('');
        }
    };

    const doPreview = () =>
        run('preview', async () => setPreview(await api.previewReachRule(aiSystemId, body)), setFormError, 'Preview failed');

    const doCreate = () =>
        run(
            'create',
            async () => {
                await api.createReachRule(aiSystemId, body);
                setAdding(false);
                setPreview(null);
                reload();
            },
            setFormError,
            'Could not add the reach rule',
        );

    return (
        <div className="space-y-2" data-testid="reach-rules">
            <div className="flex items-start gap-2">
                <p className="text-xs font-medium text-default">Record reach limits</p>
                <HelpTip text="How many distinct records one requester may reach across runs in a window. A breach is recorded as a drift finding the response policies can act on." />
            </div>

            {rowError && (
                <p className="text-xs text-red-700" data-testid="reach-row-error">
                    {rowError}
                </p>
            )}

            {rules.length === 0 && !adding && (
                <p className="text-xs text-muted" data-testid="reach-empty">
                    No reach limit — records reached across runs are recorded but never limited.
                </p>
            )}

            {rules.map((r) => (
                <div
                    key={r.id}
                    className="flex items-start justify-between gap-3 bg-surface border border-default rounded-lg p-3 text-xs"
                    data-testid={`reach-rule-${r.id}`}
                >
                    <div className="min-w-0">
                        <p className={r.enabled ? 'text-default' : 'text-muted line-through'}>
                            At most <strong>{r.max_distinct}</strong> distinct <code className="font-mono">{r.scope_key}</code>{' '}
                            records in {r.window_hours}h · {groupByLabel(r.group_by)}
                        </p>
                        <p className="text-muted mt-0.5">severity {r.severity}</p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        <label className="flex items-center gap-1 text-muted">
                            <input
                                type="checkbox"
                                checked={r.enabled}
                                disabled={busy === r.id}
                                onChange={(e) =>
                                    void run(
                                        r.id,
                                        async () => {
                                            await api.updateReachRule(aiSystemId, r.id, { enabled: e.target.checked });
                                            reload();
                                        },
                                        setRowError,
                                        'Could not change the reach rule',
                                    )
                                }
                                data-testid="reach-rule-enabled"
                            />
                            enabled
                        </label>
                        <button
                            type="button"
                            aria-label="Delete reach rule"
                            disabled={busy === r.id}
                            onClick={() =>
                                void run(
                                    r.id,
                                    async () => {
                                        await api.deleteReachRule(aiSystemId, r.id);
                                        reload();
                                    },
                                    setRowError,
                                    'Could not delete the reach rule',
                                )
                            }
                            className="p-1.5 border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-40"
                            data-testid="reach-rule-delete"
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                        </button>
                    </div>
                </div>
            ))}

            {!adding ? (
                <button
                    type="button"
                    disabled={keyNames.length === 0}
                    title={keyNames.length === 0 ? 'Save a data scope with at least one key first' : undefined}
                    onClick={() => setAdding(true)}
                    className={BUTTON}
                    data-testid="reach-add"
                >
                    Add a reach limit
                </button>
            ) : (
                <div className="bg-surface border border-default rounded-lg p-3 space-y-3" data-testid="reach-form">
                    <div className="flex flex-wrap gap-3">
                        <label className="text-xs text-muted">
                            Records of kind
                            <select
                                value={scopeKey}
                                onChange={(e) => update({ scope_key: e.target.value })}
                                aria-label="Reach scope key"
                                className={`ml-2 ${INPUT}`}
                                data-testid="reach-scope-key"
                            >
                                {keyNames.map((k) => (
                                    <option key={k} value={k}>
                                        {k}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label className="text-xs text-muted">
                            Counted
                            <select
                                value={form.group_by}
                                onChange={(e) => update({ group_by: e.target.value })}
                                aria-label="Reach group by"
                                className={`ml-2 ${INPUT}`}
                                data-testid="reach-group-by"
                            >
                                {groupByOptions.map((g) => (
                                    <option key={g.id} value={g.id}>
                                        {g.label}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label className="text-xs text-muted">
                            Window (hours)
                            <input
                                type="number"
                                min={1}
                                max={720}
                                value={form.window_hours}
                                onChange={(e) => update({ window_hours: Number(e.target.value) })}
                                aria-label="Reach window hours"
                                className={`ml-2 w-20 ${INPUT}`}
                                data-testid="reach-window"
                            />
                        </label>
                        <label className="text-xs text-muted">
                            At most
                            <input
                                type="number"
                                min={1}
                                value={form.max_distinct}
                                onChange={(e) => update({ max_distinct: Number(e.target.value) })}
                                aria-label="Reach max distinct"
                                className={`ml-2 w-24 ${INPUT}`}
                                data-testid="reach-max"
                            />
                        </label>
                        <label className="text-xs text-muted">
                            Severity
                            <select
                                value={form.severity}
                                onChange={(e) => update({ severity: e.target.value as ReachRuleSeverity })}
                                aria-label="Reach severity"
                                className={`ml-2 ${INPUT}`}
                                data-testid="reach-severity"
                            >
                                {SEVERITIES.map((s) => (
                                    <option key={s} value={s}>
                                        {s}
                                    </option>
                                ))}
                            </select>
                        </label>
                    </div>

                    {formError && (
                        <p className="text-xs text-red-700" data-testid="reach-form-error">
                            {formError}
                        </p>
                    )}

                    {preview && <ReachPreviewBlock preview={preview} limit={form.max_distinct} />}

                    <div className="flex gap-2">
                        <button
                            type="button"
                            disabled={busy === 'preview' || !scopeKey}
                            onClick={() => void doPreview()}
                            className={`inline-flex items-center gap-1 ${BUTTON}`}
                            data-testid="reach-preview"
                        >
                            <Eye className="w-3.5 h-3.5" /> Preview
                        </button>
                        <button
                            type="button"
                            disabled={busy === 'create' || !scopeKey}
                            onClick={() => void doCreate()}
                            className={BUTTON}
                            data-testid="reach-create"
                        >
                            Add limit
                        </button>
                        <button
                            type="button"
                            onClick={() => {
                                setAdding(false);
                                setPreview(null);
                                setFormError('');
                            }}
                            className={BUTTON}
                        >
                            Cancel
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

function ReachPreviewBlock({ preview, limit }: { preview: ReachPreview; limit: number }) {
    const fmt = (n: number | null | undefined) => (n === null || n === undefined ? '—' : n.toLocaleString());
    return (
        <div className="border border-default rounded-lg p-3 space-y-2 bg-surface-subtle" data-testid="reach-preview-result">
            <p className="text-xs text-default">
                Over the last {preview.days} days,{' '}
                <strong data-testid="reach-over-limit">{preview.groups_over_limit}</strong> of {preview.groups_seen} group
                {preview.groups_seen === 1 ? '' : 's'} would have gone over a limit of <strong>{limit}</strong>.
            </p>
            <p className="text-xs text-muted" data-testid="reach-percentiles">
                p95 <strong className="text-default">{fmt(preview.p95)}</strong> · p99{' '}
                <strong className="text-default">{fmt(preview.p99)}</strong> · limit{' '}
                <strong className="text-default">{limit}</strong>
            </p>
            {preview.partial && (
                <p className="flex items-start gap-1.5 text-xs text-amber-800" data-testid="reach-partial">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    Partial: the preview read only the most recent accesses.
                </p>
            )}
            {preview.top.length > 0 && (
                <ul className="space-y-0.5" data-testid="reach-top">
                    {preview.top.map((g) => (
                        <li
                            key={g.group}
                            className={`flex justify-between gap-2 text-xs ${g.max_distinct_records > limit ? 'text-red-800 font-medium' : 'text-default'}`}
                        >
                            <span className="font-mono truncate">{g.group}</span>
                            <span className="tabular-nums">{g.max_distinct_records.toLocaleString()}</span>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
}

export default DataScopePanel;
