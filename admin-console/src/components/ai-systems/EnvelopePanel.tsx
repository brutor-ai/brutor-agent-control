/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The operating envelope editor — the "what it should achieve"
 * half of the AI System Contract.
 *
 * Two things this panel is deliberate about:
 *
 * **A declared term is a promise, so the vocabulary is closed.** The term
 * list comes from the server; there is no free-text row, because a term
 * nobody evaluates is a promise nobody keeps. The one term users keep
 * asking for — "% correct decisions" — is absent on purpose: a proxy has
 * no ground truth, and the panel says so rather than leaving a gap.
 *
 * **Saving is not signing.** Changing the envelope drifts live config from
 * the active contract, and the panel says so and points at minting — the
 * promise counts once an approver has signed the version that carries it.
 *
 * **Some terms name an outcome**: the share of runs ending in one
 * declared outcome, and the average cost of one. Their keys carry the
 * outcome (`max_outcome_share:no_applications_available`), so the panel lists
 * one row per declared bound and adds rows by outcome id.
 *
 * **Some enforced terms are not graded.** The Control Plane marks them
 * (`graded: false`): the gateway enforces them and records no worst case a
 * report could grade, so the Assurance Report states each as not graded.
 * The panel says so beside the field.
 *
 * **The limit on new runs is two terms.** `max_new_runs_per_window` bounds
 * how many runs one credential starts, and `new_runs_window_seconds` is the
 * window it counts in. The Control Plane refuses a window declared without
 * the maximum, so the window is offered only while the maximum has a value
 * and is dropped when the maximum is cleared.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Plus, Save, Trash2 } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';

import { useGateway } from '../../hooks/useGateway';
import { EnvelopeResponse, EnvelopeTermSpec } from '../../lib/aiSystemTypes';
import { outcomeTermKey, reportableIds, splitEnvelopeTerm } from '../../lib/runOutcomes';
import { errorKind } from '../../lib/narrow';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface EnvelopePanelProps {
    aiSystemId: string;
    /** The gate/contract views re-query after a change — the envelope is
     *  part of the closure they render. */
    onChanged?: () => void;
}

/** The maximum of new runs one credential may start, and the window it counts in. */
const TERM_MAX_NEW_RUNS = 'max_new_runs_per_window';
const TERM_NEW_RUNS_WINDOW = 'new_runs_window_seconds';
const NEW_RUNS_TERMS: ReadonlySet<string> = new Set([TERM_MAX_NEW_RUNS, TERM_NEW_RUNS_WINDOW]);
/** The ceiling on what one conversation spends: every run of one chat, not one run. */
const TERM_MAX_COST_PER_SESSION = 'max_cost_per_session_usd';
const CONVERSATION_TERMS: ReadonlySet<string> = new Set([TERM_MAX_COST_PER_SESSION]);

/** The kinds of term, and why an operator should care which is which.
 *  Enforced ceilings stop traffic; graded bounds only appear in the report.
 *  Mixing them in one undifferentiated grid lets someone set a number
 *  believing it will protect them when it only describes them. The limit
 *  on new runs is enforced too, and is not a ceiling of one run: it bounds
 *  how many runs there are, so it has a section of its own. So has the
 *  ceiling of a conversation, which is every run of one chat. */
const GROUPS = [
    {
        key: 'enforced',
        holds: (term: string, spec: EnvelopeTermSpec) =>
            Boolean(spec.enforced) && !NEW_RUNS_TERMS.has(term) && !CONVERSATION_TERMS.has(term),
        badge: 'enforced',
        chip: 'bg-primary-50 text-primary-700 border border-primary-200',
        title: 'Ceilings for one run',
        blurb:
            'Checked at the gateway before every call. When a run reaches one of these, its next call is refused — the runaway stops at its own expense instead of consuming the whole resource group\u2019s daily budget.',
        help:
            'These block traffic, and are scoped to a single run, which is what makes them different from the group budgets and rate limits elsewhere in Brutor: a rate limit slows a loop down, and a group budget stops it only after it has spent everyone else\u2019s allowance. Set the token ceiling first — it is the one bound a vendor price change cannot loosen underneath you.',
    },
    {
        key: 'conversation',
        holds: (term: string, spec: EnvelopeTermSpec) => Boolean(spec.enforced) && CONVERSATION_TERMS.has(term),
        badge: 'enforced',
        chip: 'bg-primary-50 text-primary-700 border border-primary-200',
        title: 'Ceiling for one conversation',
        blurb:
            'Checked at the gateway before every call of a conversation. A conversation is every message of one chat: when what it has spent reaches the ceiling, its next call is refused.',
        help:
            'A run is one message; a conversation is the chat it belongs to and can last months. Many cheap runs stay under every ceiling for one run, so this is the bound that sees what the whole conversation spends. It is enforced and not graded. Raising it takes effect on the next call; ending the conversation starts its count again.',
    },
    {
        key: 'new-runs',
        holds: (term: string, spec: EnvelopeTermSpec) => Boolean(spec.enforced) && NEW_RUNS_TERMS.has(term),
        badge: 'enforced',
        chip: 'bg-primary-50 text-primary-700 border border-primary-200',
        title: 'Limit on new runs',
        blurb:
            'Checked at the gateway when a credential starts a run in this system. It bounds how many runs there are, so a caller cannot stay under every ceiling for one run by putting each step in a run of its own.',
        help:
            'The maximum is the most runs one credential (an agent identity, otherwise an API key) may start in a window; the window is its length in seconds. The window is declared only together with the maximum. Both are enforced and not graded.',
    },
    {
        key: 'graded',
        holds: (_term: string, spec: EnvelopeTermSpec) => !spec.enforced,
        badge: 'graded',
        chip: 'bg-surface-alt text-muted border border-default',
        title: 'Bounds measured over the window',
        blurb:
            'Evaluated against the run ledger and reported as met, breached or insufficient evidence in the Assurance Report. These never refuse a call.',
        help:
            'Graded terms describe the system\u2019s behaviour over the whole reporting period, so a single bad run is averaged away by design. That is the right shape for a budgeting or reliability claim and the wrong shape for a safety limit — which is exactly why the ceilings above exist separately. Rates report insufficient evidence until the system has finished at least 20 tasks.',
    },
] as const;

function stepFor(unit: string): string {
    if (unit === 'rate') return '0.01';
    if (unit === 'usd') return '0.0001';
    return '1';
}

function placeholderFor(spec: { unit: string; direction: string }, term?: string): string {
    // The window is a length, not a bound in a direction.
    if (term === TERM_NEW_RUNS_WINDOW) return 'seconds';
    if (spec.unit === 'rate') return `${spec.direction} 0\u20131`;
    if (spec.unit === 'usd') return `${spec.direction} USD`;
    if (spec.unit === 'seconds') return `${spec.direction} seconds`;
    return `${spec.direction} count`;
}

export function EnvelopePanel({ aiSystemId, onChanged }: EnvelopePanelProps) {
    const { fetchAISystemEnvelope, putAISystemEnvelope, fetchAISystemOutcomes } = useGateway();

    const [data, setData] = useState<EnvelopeResponse | null>(null);
    const [values, setValues] = useState<Record<string, string>>({});
    const [dirty, setDirty] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');

    const load = useCallback(async () => {
        try {
            setError('');
            const resp = await fetchAISystemEnvelope(aiSystemId);
            setData(resp);
            const initial: Record<string, string> = {};
            for (const [term, bound] of Object.entries(
                resp.operating_envelope ?? {}
            )) {
                initial[term] = String(bound);
            }
            setValues(initial);
            setDirty(false);
        } catch (err: unknown) {
            console.error('Failed to load envelope:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load envelope');
            setData(null);
        }
    }, [aiSystemId, fetchAISystemEnvelope]);

    useEffect(() => {
        void load();
    }, [load]);

    // The outcome ids the per-outcome terms can name — offered as
    // suggestions; best-effort (a typed id is still accepted).
    const [outcomeIds, setOutcomeIds] = useState<string[]>([]);
    const [readFailure, reads] = useReadFailures<'outcomes'>();
    const [outcomesAsked, setOutcomesAsked] = useState(0);
    useEffect(() => {
        let cancelled = false;
        Promise.resolve()
            .then(() => fetchAISystemOutcomes(aiSystemId))
            .then(
                (res) => {
                    if (cancelled) return;
                    const ids = reportableIds(res?.outcomes);
                    // An outcome id alone covers all of its variants.
                    const outcomes = (res?.outcomes ?? []).filter((o) => (o.variants ?? []).length > 0).map((o) => o.id);
                    setOutcomeIds([...new Set([...outcomes, ...ids])].sort());
                    reads.loaded('outcomes');
                },
                (err: unknown) => {
                    if (cancelled) return;
                    setOutcomeIds([]);
                    reads.lost('outcomes', 'Failed to load the declared outcomes; an outcome id can still be typed', err);
                },
            );
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, fetchAISystemOutcomes, outcomesAsked, reads]);

    const save = async () => {
        try {
            setBusy(true);
            setError('');
            setNotice('');
            const envelope: Record<string, number> = {};
            for (const [term, raw] of Object.entries(values)) {
                if (raw.trim() === '') continue;
                const num = Number(raw);
                if (Number.isNaN(num)) {
                    setError(`${term}: not a number`);
                    setBusy(false);
                    return;
                }
                envelope[term] = num;
            }
            const resp = await putAISystemEnvelope(
                aiSystemId,
                Object.keys(envelope).length ? envelope : null
            );
            setData(resp);
            setDirty(false);
            if (resp.config_drifted) {
                setNotice(
                    'Saved. Live configuration now differs from the active ' +
                        'contract — mint and approve a new version to bring ' +
                        'this promise under signature.'
                );
            } else {
                setNotice('Saved.');
            }
            onChanged?.();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to save envelope');
        } finally {
            setBusy(false);
        }
    };

    if (!data && !error) return null;

    return (
        <div className="space-y-3" data-testid="envelope-panel">
            <ReadFailureNotice failure={readFailure('outcomes')} onRetry={() => setOutcomesAsked((n) => n + 1)} />
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">
                        Operating envelope
                    </h3>
                    <HelpTip
                        title="Operating envelope"
                        text="What this system is supposed to achieve — the other half of its contract, alongside the authorization closure that says what it may touch. Two kinds of term live here. ENFORCED ceilings apply to one run and are checked at the gateway before each call, so a runaway agent is stopped at its own expense instead of draining the whole group's daily budget. GRADED bounds are measured over the reporting window and appear in the Assurance Report as met, breached or insufficient evidence — they never block traffic. There is deliberately no correctness term: a proxy has no ground truth about whether an answer was right."
                    />
                </div>
                <button
                    type="button"
                    disabled={busy || !dirty}
                    onClick={() => void save()}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                    data-testid="envelope-save"
                >
                    <Save className="w-4 h-4" />
                    Save envelope
                </button>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}
            {notice && !error && (
                <p className="text-sm text-muted" data-testid="envelope-notice">
                    {notice}
                </p>
            )}

            {data && (
                <div className="space-y-3">
                    {GROUPS.map((group) => {
                        const entries = Object.entries(data.known_terms).filter(
                            ([term, spec]) =>
                                group.holds(term, spec) &&
                                // Per-outcome terms have their own section.
                                !spec.parameterized,
                        );
                        // The window is the window of the maximum: offered while the maximum has a value.
                        const maximumOfWindow = data.known_terms[TERM_MAX_NEW_RUNS] as EnvelopeTermSpec | undefined;
                        const windowNeedsMaximum = (term: string): boolean =>
                            term === TERM_NEW_RUNS_WINDOW && (values[TERM_MAX_NEW_RUNS] ?? '').trim() === '';
                        if (entries.length === 0) return null;
                        return (
                            <div
                                key={group.key}
                                className="bg-surface border border-default rounded-lg p-4"
                                data-testid={`envelope-group-${group.key}`}
                            >
                                <div className="flex items-start gap-2 mb-1">
                                    <span
                                        className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider ${group.chip}`}
                                    >
                                        {group.badge}
                                    </span>
                                    <p className="text-sm font-medium text-default">
                                        {group.title}
                                    </p>
                                    <HelpTip title={group.title} text={group.help} />
                                </div>
                                <p className="text-[11px] text-muted mb-3">
                                    {group.blurb}
                                </p>
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    {entries.map(([term, spec]) => (
                                        <div
                                            key={term}
                                            className="text-xs text-muted flex flex-col gap-1"
                                        >
                                            <span className="flex items-center gap-1">
                                                <label htmlFor={`envelope-${term}`}>
                                                    {spec.label}
                                                </label>
                                                {spec.help && (
                                                    <HelpTip
                                                        title={spec.label}
                                                        text={spec.help}
                                                        ariaLabel={`Help: ${spec.label}`}
                                                    />
                                                )}
                                            </span>
                                            <span className="flex items-center gap-1">
                                                {spec.unit === 'usd' && (
                                                    <span className="text-muted">$</span>
                                                )}
                                                <input
                                                    id={`envelope-${term}`}
                                                    type="number"
                                                    step={stepFor(spec.unit)}
                                                    min="0"
                                                    max={spec.unit === 'rate' ? '1' : undefined}
                                                    value={values[term] ?? ''}
                                                    placeholder={placeholderFor(spec, term)}
                                                    disabled={windowNeedsMaximum(term)}
                                                    onChange={(e) => {
                                                        const entered = e.target.value;
                                                        setValues((v) => {
                                                            const next = { ...v, [term]: entered };
                                                            // Without its maximum the window is not declared.
                                                            if (term === TERM_MAX_NEW_RUNS && entered.trim() === '') delete next[TERM_NEW_RUNS_WINDOW];
                                                            return next;
                                                        });
                                                        setDirty(true);
                                                    }}
                                                    className="px-2 py-1.5 text-sm border border-strong rounded w-full disabled:opacity-50"
                                                    data-testid={`envelope-${term}`}
                                                />
                                            </span>
                                            {windowNeedsMaximum(term) && (
                                                <span className="text-[11px] text-muted" data-testid="envelope-window-needs-maximum">
                                                    Declared only together with {maximumOfWindow?.label ?? TERM_MAX_NEW_RUNS}.
                                                </span>
                                            )}
                                            {spec.enforced && spec.graded === false && (
                                                <span className="text-[11px] text-muted" data-testid={`envelope-not-graded-${term}`}>
                                                    Enforced, not graded: the Assurance Report states this term and shows it as not graded.
                                                </span>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            </div>
                        );
                    })}
                    <OutcomeTerms
                        terms={Object.entries(data.known_terms).filter(([, spec]) => spec.parameterized)}
                        values={values}
                        outcomeIds={outcomeIds}
                        onChange={(next) => {
                            setValues(next);
                            setDirty(true);
                        }}
                    />
                    <p className="text-[11px] text-muted">
                        Empty terms are undeclared — a blank field is not a
                        ceiling of zero. Rates are 0–1 fractions of finished
                        runs; counts and seconds are whole numbers. Loosening a
                        signed bound is a contract widening the approver will
                        be shown.
                    </p>
                </div>
            )}
        </div>
    );
}

/**
 * The per-outcome terms: one row per declared `<term>:<outcome>` bound, and
 * an "add" row per term that names the outcome. Graded, never enforced.
 */
function OutcomeTerms({
    terms,
    values,
    outcomeIds,
    onChange,
}: {
    terms: [string, EnvelopeTermSpec][];
    values: Record<string, string>;
    outcomeIds: string[];
    onChange: (values: Record<string, string>) => void;
}) {
    const [adding, setAdding] = useState<Record<string, string>>({});
    const [addError, setAddError] = useState<Record<string, string>>({});
    if (terms.length === 0) return null;
    const listId = 'envelope-outcome-ids';
    return (
        <div className="bg-surface border border-default rounded-lg p-4" data-testid="envelope-group-outcomes">
            <div className="flex items-start gap-2 mb-1">
                <span className="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider bg-surface-alt text-muted border border-default">
                    graded
                </span>
                <p className="text-sm font-medium text-default">Bounds per declared outcome</p>
                <HelpTip
                    title="Bounds per declared outcome"
                    text="Measured over the window on the runs that ended in one declared outcome, never blended with the others. An outcome id without a variant covers all of its variants. A jump in the share of a 'nothing to do' outcome is how an upstream feed that silently stopped delivering shows up."
                />
            </div>
            <p className="text-[11px] text-muted mb-3">
                Evaluated against the run ledger and reported in the Assurance Report. These never refuse a call.
            </p>
            <datalist id={listId}>
                {outcomeIds.map((id) => (
                    <option key={id} value={id} />
                ))}
            </datalist>
            <div className="space-y-4">
                {terms.map(([base, spec]) => {
                    const rows = Object.keys(values)
                        .filter((k) => splitEnvelopeTerm(k).base === base && splitEnvelopeTerm(k).outcome)
                        .sort();
                    const add = () => {
                        const key = outcomeTermKey(base, adding[base] ?? '');
                        if (!key) {
                            setAddError((e) => ({ ...e, [base]: 'Enter an outcome id: <outcome> or <outcome>.<variant>.' }));
                            return;
                        }
                        setAddError((e) => ({ ...e, [base]: '' }));
                        setAdding((a) => ({ ...a, [base]: '' }));
                        if (!(key in values)) onChange({ ...values, [key]: '' });
                    };
                    return (
                        <div key={base} data-testid={`envelope-outcome-term-${base}`}>
                            <p className="flex items-center gap-1 text-xs text-muted">
                                {spec.label}
                                {spec.help && <HelpTip title={spec.label} text={spec.help} />}
                            </p>
                            <div className="mt-1 space-y-1.5">
                                {rows.map((key) => (
                                    <div key={key} className="flex items-center gap-2 text-xs">
                                        <code className="font-mono text-default w-64 truncate" title={key}>
                                            {splitEnvelopeTerm(key).outcome}
                                        </code>
                                        {spec.unit === 'usd' && <span className="text-muted">$</span>}
                                        <input
                                            type="number"
                                            step={stepFor(spec.unit)}
                                            min="0"
                                            max={spec.unit === 'rate' ? '1' : undefined}
                                            value={values[key] ?? ''}
                                            placeholder={placeholderFor(spec)}
                                            aria-label={`${spec.label}: ${splitEnvelopeTerm(key).outcome ?? ''}`}
                                            onChange={(e) => onChange({ ...values, [key]: e.target.value })}
                                            className="px-2 py-1 text-sm border border-strong rounded w-32"
                                            data-testid={`envelope-${key}`}
                                        />
                                        <button
                                            type="button"
                                            onClick={() => {
                                                const next = { ...values };
                                                delete next[key];
                                                onChange(next);
                                            }}
                                            className="p-1 text-muted hover:text-red-700"
                                            aria-label={`Remove ${key}`}
                                            data-testid={`envelope-remove-${key}`}
                                        >
                                            <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                    </div>
                                ))}
                                <div className="flex items-center gap-2">
                                    <input
                                        type="text"
                                        list={listId}
                                        value={adding[base] ?? ''}
                                        onChange={(e) => setAdding((a) => ({ ...a, [base]: e.target.value }))}
                                        placeholder="outcome or outcome.variant"
                                        aria-label={`Outcome for ${spec.label}`}
                                        className="px-2 py-1 text-sm border border-strong rounded w-64 font-mono"
                                        data-testid={`envelope-add-outcome-${base}`}
                                    />
                                    <button
                                        type="button"
                                        onClick={add}
                                        className="flex items-center gap-1 px-2 py-1 text-xs border border-strong rounded hover:bg-surface-hover"
                                        data-testid={`envelope-add-${base}`}
                                    >
                                        <Plus className="w-3.5 h-3.5" />
                                        Add bound
                                    </button>
                                </div>
                                {addError[base] && (
                                    <p className="text-xs text-red-700" data-testid={`envelope-add-error-${base}`}>
                                        {addError[base]}
                                    </p>
                                )}
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

export default EnvelopePanel;
