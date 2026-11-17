/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Automatic response to drift.
 *
 * This is the panel that arms a control which can restrict production
 * systems without a human in the loop, so almost every decision here is
 * about making the consequences visible *before* the click.
 *
 * **The blast radius is shown before arming, not after.** Autonomy resolves
 * min-wins down the resource-group tree: a policy attached to a team-level
 * group restricts every inheriting system beneath it. An operator who finds
 * that out during an incident will never trust the feature again, so the
 * preview is fetched as the form changes and the arm button is not offered
 * until it has been seen.
 *
 * **Refusals are shown as prominently as actions.** Silence after a
 * critical finding is indistinguishable from a broken feature. "No policy
 * matched" and "the loop is down" look identical unless the UI says which.
 *
 * **Suspend is a separate, deliberate switch.** It denies every call
 * including read-only ones, and it is off by default in the API for the
 * same reason it is a second click here.
 */

import { useCallback, useEffect, useState } from 'react';
import {
    AlertTriangle,
    Ban,
    Bell,
    Check, Layers,
    RefreshCw,
    RotateCcw,
    Trash2,
    X,
} from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';

import { useGateway } from '../../hooks/useGateway';
import {
    AUTONOMY_LABELS,
    AutonomyLevel,
    DRIFT_CLASS_LABELS,
    DriftClass,
    PolicyPreview,
    REFUSAL_LABELS,
    ResponseHistory,
    ResponsePolicy,
    ResponsePolicyListResponse,
    ResponseSeverity,
} from '../../lib/aiSystemTypes';
import { runBackground } from '../../lib/asyncSafe';
import { errorKind } from '../../lib/narrow';

interface ResponsePolicyPanelProps {
    aiSystemId: string;
}

const SEVERITIES: ResponseSeverity[] = ['low', 'medium', 'high', 'critical'];
const TIGHTENING_LEVELS: AutonomyLevel[] = [
    'approval_required',
    'restricted',
    'suspended',
];

export function ResponsePolicyPanel({ aiSystemId }: ResponsePolicyPanelProps) {
    const {
        fetchResponsePolicies,
        createResponsePolicy,
        previewResponsePolicy,
        deleteResponsePolicy,
        fetchResponseHistory,
        restoreAutonomy,
    } = useGateway();

    const [policies, setPolicies] = useState<ResponsePolicyListResponse | null>(null);
    const [history, setHistory] = useState<ResponseHistory | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState('');

    // --- the form ---------------------------------------------------------
    const [adding, setAdding] = useState(false);
    const [name, setName] = useState('');
    const [severity, setSeverity] = useState<ResponseSeverity>('high');
    const [driftClass, setDriftClass] = useState<DriftClass | ''>('');
    const [level, setLevel] = useState<AutonomyLevel>('restricted');
    const [allowSuspend, setAllowSuspend] = useState(false);
    const [preview, setPreview] = useState<PolicyPreview | null>(null);
    const [previewError, setPreviewError] = useState('');

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const [p, h] = await Promise.all([
                fetchResponsePolicies(aiSystemId),
                fetchResponseHistory(aiSystemId),
            ]);
            setPolicies(p);
            setHistory(h);
        } catch (err: unknown) {
            console.error('Failed to load response policies:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load');
            setPolicies(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchResponsePolicies, fetchResponseHistory]);

    useEffect(() => {
        void load();
    }, [load]);

    const draft = useCallback(
        () => ({
            signal_kind: 'drift',
            drift_class: driftClass || null,
            severity_at_least: severity,
            actions: [{ kind: 'set_autonomy', level }],
            allow_suspend: allowSuspend,
            require_human_to_restore: true,
        }),
        [driftClass, severity, level, allowSuspend]
    );

    // Refetched whenever the draft changes: the operator must be looking at
    // the radius for the policy they are about to arm, not a stale one.
    useEffect(() => {
        if (!adding) return;
        let cancelled = false;
        runBackground((async () => {
            try {
                setPreviewError('');
                const p = await previewResponsePolicy(aiSystemId, draft());
                if (!cancelled) setPreview(p);
            } catch (err: unknown) {
                if (!cancelled) {
                    setPreview(null);
                    setPreviewError(
                        err instanceof Error ? err.message : 'Preview failed'
                    );
                }
            }
        })(), 'async work');
        return () => {
            cancelled = true;
        };
    }, [adding, aiSystemId, draft, previewResponsePolicy]);

    const arm = async () => {
        try {
            setBusy('arm');
            setError('');
            await createResponsePolicy(aiSystemId, { name, ...draft() });
            setAdding(false);
            setName('');
            setPreview(null);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Could not arm the policy');
        } finally {
            setBusy('');
        }
    };

    const disarm = async (id: string) => {
        try {
            setBusy(id);
            await deleteResponsePolicy(id);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Could not disarm');
        } finally {
            setBusy('');
        }
    };

    const doRestore = async () => {
        try {
            setBusy('restore');
            await restoreAutonomy(aiSystemId, { to_level: null });
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Could not restore');
        } finally {
            setBusy('');
        }
    };

    return (
        <div className="space-y-4" data-testid="response-policy-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">
                        Automatic response
                    </h3>
                    <HelpTip text="What the platform does on its own when this system drifts. Every automatic action is reversible, rate-limited, and recorded in the audit trail." />
                </div>
                <button
                    type="button"
                    onClick={() => void load()}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {/* A pending backlog that stays high is how an operator notices
                the loop has stopped running at all. */}
            {history && history.pending_count > 0 && (
                <p className="text-xs text-muted" data-testid="pending-count">
                    {history.pending_count} finding(s) not yet considered by the
                    response loop.
                </p>
            )}

            {policies && !error && policies.total === 0 && !adding && (
                <div className="p-6 text-center border border-dashed border-strong rounded-lg">
                    <p className="text-sm text-default font-medium">
                        No automatic response
                    </p>
                    <p className="text-sm text-muted mt-1">
                        Drift on this system is detected and recorded, but nothing
                        happens without a human.
                    </p>
                </div>
            )}

            {policies?.policies.map((p) => (
                <PolicyRow
                    key={p.id}
                    policy={p}
                    isInherited={p.ai_system_id !== aiSystemId}
                    busy={busy === p.id}
                    onDisarm={() => void disarm(p.id)}
                />
            ))}

            {!adding ? (
                <button
                    type="button"
                    onClick={() => setAdding(true)}
                    className="px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg"
                    data-testid="add-policy"
                >
                    Add a response policy
                </button>
            ) : (
                <div
                    className="bg-surface border border-default rounded-lg p-4 space-y-3"
                    data-testid="policy-form"
                >
                    <input
                        type="text"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="Policy name"
                        aria-label="Policy name"
                        className="w-full px-2 py-1.5 text-sm border border-strong rounded"
                        data-testid="policy-name"
                    />

                    <div className="flex flex-wrap gap-3">
                        <label className="text-xs text-muted">
                            When severity is at least
                            <select
                                value={severity}
                                onChange={(e) =>
                                    setSeverity(e.target.value as ResponseSeverity)
                                }
                                aria-label="Severity floor"
                                className="ml-2 px-2 py-1 text-xs border border-strong rounded"
                                data-testid="severity-select"
                            >
                                {SEVERITIES.map((s) => (
                                    <option key={s} value={s}>
                                        {s}
                                    </option>
                                ))}
                            </select>
                        </label>

                        <label className="text-xs text-muted">
                            Drift class
                            <select
                                value={driftClass}
                                onChange={(e) =>
                                    setDriftClass(e.target.value as DriftClass | '')
                                }
                                aria-label="Drift class"
                                className="ml-2 px-2 py-1 text-xs border border-strong rounded"
                                data-testid="class-select"
                            >
                                <option value="">any</option>
                                {Object.entries(DRIFT_CLASS_LABELS).map(([k, v]) => (
                                    <option key={k} value={k}>
                                        {v}
                                    </option>
                                ))}
                            </select>
                        </label>

                        <label className="text-xs text-muted">
                            Set autonomy to
                            <select
                                value={level}
                                onChange={(e) =>
                                    setLevel(e.target.value as AutonomyLevel)
                                }
                                aria-label="Target autonomy"
                                className="ml-2 px-2 py-1 text-xs border border-strong rounded"
                                data-testid="level-select"
                            >
                                {TIGHTENING_LEVELS.map((l) => (
                                    <option key={l} value={l}>
                                        {AUTONOMY_LABELS[l]}
                                    </option>
                                ))}
                            </select>
                        </label>
                    </div>

                    {/* Suspend is a second, deliberate switch: it denies every
                        call including read-only ones. */}
                    {level === 'suspended' && (
                        <label
                            className="flex items-start gap-2 text-xs text-red-800"
                            data-testid="suspend-optin"
                        >
                            <input
                                type="checkbox"
                                checked={allowSuspend}
                                onChange={(e) => setAllowSuspend(e.target.checked)}
                                className="mt-0.5"
                                data-testid="suspend-checkbox"
                            />
                            <span>
                                I understand this denies <strong>every</strong> call from
                                every actor in the affected systems, including read-only
                                ones, without a human in the loop.
                            </span>
                        </label>
                    )}

                    {previewError && (
                        <p className="text-xs text-red-700" data-testid="preview-error">
                            {previewError}
                        </p>
                    )}

                    {preview && <PreviewBlock preview={preview} />}

                    <div className="flex gap-2">
                        <button
                            type="button"
                            disabled={
                                busy === 'arm' ||
                                !name.trim() ||
                                !preview ||
                                (level === 'suspended' && !allowSuspend)
                            }
                            onClick={() => void arm()}
                            className="px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                            data-testid="arm-button"
                        >
                            Arm this policy
                        </button>
                        <button
                            type="button"
                            onClick={() => {
                                setAdding(false);
                                setPreview(null);
                            }}
                            className="px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                        >
                            Cancel
                        </button>
                    </div>
                </div>
            )}

            {history && history.responses.length > 0 && (
                <HistoryBlock history={history} busy={busy} onRestore={() => void doRestore()} />
            )}
        </div>
    );
}

function PreviewBlock({ preview }: { preview: PolicyPreview }) {
    return (
        <div
            className="border border-default rounded-lg p-3 space-y-2 bg-surface-subtle"
            data-testid="policy-preview"
        >
            <p className="text-xs font-medium text-default">
                If this fires right now
            </p>

            {preview.blast_radius && (
                <p
                    className={`flex items-start gap-1.5 text-xs ${
                        preview.blast_radius.is_wide ? 'text-amber-800' : 'text-muted'
                    }`}
                    data-testid="blast-radius"
                >
                    <Layers className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    <span>
                        Affects <strong>{preview.blast_radius.total}</strong> system
                        {preview.blast_radius.total === 1 ? '' : 's'}
                        {preview.blast_radius.is_wide && (
                            <>
                                {' — '}
                                {preview.blast_radius.inherited_by.join(', ')} inherit
                                from this one
                            </>
                        )}
                    </span>
                </p>
            )}

            {preview.would_do.length > 0 ? (
                <ul className="space-y-0.5" data-testid="would-do">
                    {preview.would_do.map((a, i) => (
                        <li
                            key={`${a.kind}-${i}`}
                            className="flex items-start gap-1.5 text-xs text-default"
                        >
                            <Check className="w-3 h-3 text-green-600 shrink-0 mt-0.5" />
                            {a.kind === 'set_autonomy'
                                ? `Autonomy ${preview.current_autonomy ?? 'unset'} → ${String(
                                      a.params.level
                                  )}`
                                : a.kind}
                        </li>
                    ))}
                </ul>
            ) : (
                <p className="text-xs text-muted" data-testid="would-do-nothing">
                    Nothing — see below.
                </p>
            )}

            {preview.would_refuse.length > 0 && (
                <ul className="space-y-0.5" data-testid="would-refuse">
                    {preview.would_refuse.map((r, i) => (
                        <li
                            key={`${r.code}-${i}`}
                            className="flex items-start gap-1.5 text-xs text-muted"
                        >
                            <X className="w-3 h-3 shrink-0 mt-0.5" />
                            {REFUSAL_LABELS[r.code] ?? r.code}
                            {r.detail ? ` — ${r.detail}` : ''}
                        </li>
                    ))}
                </ul>
            )}

            {preview.warnings.map((w, i) => (
                <p
                    key={i}
                    className="flex items-start gap-1.5 text-xs text-amber-800"
                    data-testid="preview-warning"
                >
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    {w}
                </p>
            ))}
        </div>
    );
}

function PolicyRow({
    policy,
    isInherited,
    busy,
    onDisarm,
}: {
    policy: ResponsePolicy;
    isInherited: boolean;
    busy: boolean;
    onDisarm: () => void;
}) {
    return (
        <div
            className="bg-surface border border-default rounded-lg p-4 space-y-2"
            data-testid={`policy-${policy.id}`}
        >
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <p className="text-sm font-medium text-default">
                        {policy.name}
                        {isInherited && (
                            <span
                                className="ml-2 inline-flex px-2 py-0.5 rounded text-xs bg-slate-100 text-slate-700"
                                data-testid="inherited-badge"
                            >
                                inherited from {policy.ai_system_id}
                            </span>
                        )}
                    </p>
                    <p className="text-xs text-muted mt-1">
                        On {policy.drift_class ? DRIFT_CLASS_LABELS[policy.drift_class] : 'any'} drift
                        at {policy.severity_at_least} or above
                        {policy.target_autonomy
                            ? ` → ${AUTONOMY_LABELS[policy.target_autonomy]}`
                            : ''}
                    </p>
                </div>
                <button
                    type="button"
                    disabled={busy || isInherited}
                    onClick={onDisarm}
                    title={
                        isInherited
                            ? 'Disarm it on the system it is attached to'
                            : 'Disarm this policy'
                    }
                    className="shrink-0 p-1.5 border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-40"
                    data-testid="disarm-button"
                >
                    <Trash2 className="w-3.5 h-3.5" />
                </button>
            </div>

            <div className="flex flex-wrap gap-2 text-xs">
                {policy.allow_suspend && (
                    <span
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-red-100 text-red-800"
                        data-testid="suspend-badge"
                    >
                        <Ban className="w-3 h-3" />
                        may suspend
                    </span>
                )}
                {policy.require_human_to_restore && (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-100 text-slate-700">
                        <RotateCcw className="w-3 h-3" />
                        human restore
                    </span>
                )}
                <span className="text-muted">
                    cooldown {Math.round(policy.response_cooldown_seconds / 60)}m
                </span>
                {policy.fire_count > 0 && (
                    <span className="text-muted" data-testid="fire-count">
                        fired {policy.fire_count}×
                    </span>
                )}
            </div>
        </div>
    );
}

function HistoryBlock({
    history,
    busy,
    onRestore,
}: {
    history: ResponseHistory;
    busy: string;
    onRestore: () => void;
}) {
    return (
        <div className="space-y-2" data-testid="response-history">
            <div className="flex items-center justify-between">
                <p className="text-xs font-medium text-default">Recent responses</p>
                {history.acted_count > 0 && (
                    <button
                        type="button"
                        disabled={busy === 'restore'}
                        onClick={onRestore}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        data-testid="restore-button"
                        title="Clear the automatic override and let this system inherit again"
                    >
                        <RotateCcw className="w-3.5 h-3.5" />
                        Restore autonomy
                    </button>
                )}
            </div>

            {history.responses.map((r) => (
                <div
                    key={r.drift_event_id}
                    className="bg-surface border border-default rounded-lg p-3 text-xs"
                    data-testid={`response-${r.drift_event_id}`}
                >
                    <div className="flex items-start gap-1.5">
                        {r.acted ? (
                            <Check className="w-3.5 h-3.5 text-green-600 shrink-0 mt-0.5" />
                        ) : (
                            <Bell className="w-3.5 h-3.5 text-muted shrink-0 mt-0.5" />
                        )}
                        <span className="text-default">
                            {r.acted
                                ? `${r.policy_name ?? 'A policy'} acted on ${r.severity} ${
                                      DRIFT_CLASS_LABELS[r.drift_class] ?? r.drift_class
                                  } drift`
                                : `${r.severity} ${
                                      DRIFT_CLASS_LABELS[r.drift_class] ?? r.drift_class
                                  } drift — no action`}
                        </span>
                    </div>

                    {/* A refusal is as important as an action: without it,
                        "nothing matched" and "the loop is down" look the same. */}
                    {r.refusals.map((refusal, i) => (
                        <p
                            key={i}
                            className="text-muted mt-1 ml-5"
                            data-testid="response-refusal"
                        >
                            {REFUSAL_LABELS[refusal.code] ?? refusal.code}
                        </p>
                    ))}

                    {/* What the action that WAS taken does not cover — e.g. an
                        approval hold does not stop plain API keys or people. */}
                    {(r.limitations ?? []).map((lim, i) => (
                        <p
                            key={`${lim.code}-${i}`}
                            className="flex items-start gap-1.5 mt-1 ml-5 text-amber-800"
                            data-testid="response-limitation"
                        >
                            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                            <span>{lim.detail}</span>
                        </p>
                    ))}
                </div>
            ))}
        </div>
    );
}

export default ResponsePolicyPanel;
