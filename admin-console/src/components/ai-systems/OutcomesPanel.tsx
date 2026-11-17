/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The declared outcomes editor — how a run of this system may
 * end, signed off with the contract.
 *
 * Each outcome says whether it counts toward the operating baseline (is it
 * the work, or "nothing to do"), may carry variants (domain states of the
 * same outcome, which the baselines are learned on), and may declare what a
 * run reporting it must look like (`expect`: cheap outcomes need evidence).
 *
 * Like the operating envelope, **saving is not signing**: the gateway judges
 * reported outcomes against the catalogue in the ACTIVE contract. The panel
 * shows that catalogue, and when the live one differs it says so and points
 * at minting a new version.
 */

import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Plus, RotateCcw, Save, Trash2 } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { useGateway } from '../../hooks/useGateway';
import { errorMessage } from '../../lib/narrow';
import {
    catalogueIdChanges,
    catalogueServerErrors,
    catalogueToDrafts,
    cataloguesDiffer,
    draftsToCatalogue,
    emptyOutcomeDraft,
    emptyVariantDraft,
    reportableIds,
    validateCatalogueDraft,
    MAX_OUTCOMES,
    MAX_VARIANTS,
    type CatalogueErrors,
    type ExpectDraft,
    type OutcomeDraft,
    type OutcomesResponse,
    type VariantDraft,
} from '../../lib/runOutcomes';

interface OutcomesPanelProps {
    aiSystemId: string;
    /** The catalogue is part of the closure the contract views render. */
    onChanged?: () => void;
}

const INPUT = 'w-full px-2 py-1.5 text-sm border border-strong rounded bg-surface';
const SMALL_LABEL = 'text-[11px] text-muted';

function FieldError({ message, testId }: { message?: string; testId?: string }) {
    if (!message) return null;
    return (
        <p className="mt-0.5 text-xs text-red-700" data-testid={testId}>
            {message}
        </p>
    );
}

export function OutcomesPanel({ aiSystemId, onChanged }: OutcomesPanelProps) {
    const { fetchAISystemOutcomes, putAISystemOutcomes } = useGateway();

    const [data, setData] = useState<OutcomesResponse | null>(null);
    const [drafts, setDrafts] = useState<OutcomeDraft[]>([]);
    const [dirty, setDirty] = useState(false);
    const [touched, setTouched] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [serverErrors, setServerErrors] = useState<CatalogueErrors>({});

    const accept = (resp: OutcomesResponse) => {
        setData(resp);
        setDrafts(catalogueToDrafts(resp.outcomes));
        setDirty(false);
        setTouched(false);
        setServerErrors({});
    };

    useEffect(() => {
        let cancelled = false;
        fetchAISystemOutcomes(aiSystemId).then(
            (resp) => {
                if (cancelled) return;
                setError('');
                setData(resp);
                setDrafts(catalogueToDrafts(resp.outcomes));
                setDirty(false);
                setTouched(false);
                setServerErrors({});
            },
            (err: unknown) => {
                if (cancelled) return;
                setError(errorMessage(err, 'Failed to load the declared outcomes'));
                setData(null);
            },
        );
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, fetchAISystemOutcomes]);

    const clientErrors = useMemo(() => validateCatalogueDraft(drafts), [drafts]);
    const errors: CatalogueErrors = touched ? { ...serverErrors, ...clientErrors } : serverErrors;
    const err = (path: string) => errors[path];

    const edit = (next: OutcomeDraft[]) => {
        setDrafts(next);
        setDirty(true);
        setNotice('');
        // Server errors are keyed by position; once the form changes they
        // may point at the wrong row, so they go until the next save.
        setServerErrors({});
    };
    const updateOutcome = (i: number, patch: Partial<OutcomeDraft>) =>
        edit(drafts.map((o, k) => (k === i ? { ...o, ...patch } : o)));
    const updateVariant = (i: number, j: number, patch: Partial<VariantDraft>) =>
        updateOutcome(i, { variants: drafts[i].variants.map((v, k) => (k === j ? { ...v, ...patch } : v)) });

    const save = async () => {
        setTouched(true);
        if (Object.keys(clientErrors).length > 0) {
            setError('Fix the highlighted fields before saving.');
            return;
        }
        try {
            setBusy(true);
            setError('');
            setNotice('');
            const resp = await putAISystemOutcomes(aiSystemId, drafts.length ? draftsToCatalogue(drafts) : null);
            accept(resp);
            setNotice(
                resp.config_drifted
                    ? 'Saved. Live configuration now differs from the active contract — mint, approve and promote a new version on the Active tab to have runs judged against these outcomes.'
                    : 'Saved.',
            );
            onChanged?.();
        } catch (e: unknown) {
            const fields = catalogueServerErrors(e);
            setServerErrors(fields);
            setError(
                Object.keys(fields).length
                    ? 'The catalogue was refused — see the highlighted fields.'
                    : errorMessage(e, 'Failed to save the declared outcomes'),
            );
        } finally {
            setBusy(false);
        }
    };

    if (!data && !error) return null;

    const active = data?.active_outcomes ?? null;
    const live = data?.outcomes ?? null;
    const differs = !!data && cataloguesDiffer(active, live);
    const changes = catalogueIdChanges(active, live);
    const typedIds = reportableIds(
        drafts
            .filter((o) => o.id)
            .map((o) => ({
                id: o.id,
                label: o.label,
                counts_toward_baseline: o.counts_toward_baseline,
                variants: o.variants.filter((v) => v.id).map((v) => ({ id: v.id, label: v.label })),
            })),
    );

    return (
        <div className="space-y-3" data-testid="outcomes-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Declared outcomes</h3>
                    <HelpTip
                        title="Declared outcomes"
                        text="How a run of this system may end. The agent names the outcome when it closes a run (<outcome> or <outcome>.<variant>); the gateway checks it against this list in the active contract. A completed run in a declared outcome that counts toward the baseline is a success; one that does not count (nothing to do) is no-work overhead — never a success, never averaged into cost or run length; an undeclared or missing outcome is a deviation. Variants are domain states of the same outcome; baselines and the outcome mix are learned per variant. Expectations are evidence a run must show: an outcome that costs nothing should prove it did nothing."
                    />
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        disabled={busy || !dirty}
                        onClick={() => data && accept(data)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        data-testid="outcomes-discard"
                    >
                        <RotateCcw className="w-4 h-4" />
                        Discard changes
                    </button>
                    <button
                        type="button"
                        disabled={busy || !dirty}
                        onClick={() => void save()}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                        data-testid="outcomes-save"
                    >
                        <Save className="w-4 h-4" />
                        Save outcomes
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded" data-testid="outcomes-error">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}
            {err('outcomes') && <FieldError message={err('outcomes')} testId="outcomes-error-outcomes" />}
            {notice && !error && (
                <p className="text-sm text-muted" data-testid="outcomes-notice">
                    {notice}
                </p>
            )}

            {data && (
                <>
                    <JudgedAgainst version={data.active_contract_version ?? null} ids={reportableIds(active)} />

                    {differs && (
                        <div
                            className="flex items-start gap-2 p-3 border border-amber-200 bg-amber-50 rounded"
                            data-testid="outcomes-config-drift"
                        >
                            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                            <div className="text-sm text-amber-900">
                                <p className="font-medium">The saved outcomes are not signed yet</p>
                                <p className="mt-1">
                                    The live catalogue differs from the one in{' '}
                                    {data.active_contract_version ? `contract v${data.active_contract_version}` : 'the active contract'}
                                    , and runs are still judged against that one. Mint, approve and promote a new
                                    version on the Active tab to change what the gateway checks.
                                </p>
                                {(changes.added.length > 0 || changes.removed.length > 0) && (
                                    <ul className="mt-1 text-xs space-y-0.5">
                                        {changes.added.length > 0 && (
                                            <li data-testid="outcomes-drift-added">Would add: {changes.added.join(', ')}</li>
                                        )}
                                        {changes.removed.length > 0 && (
                                            <li data-testid="outcomes-drift-removed">Would remove: {changes.removed.join(', ')}</li>
                                        )}
                                    </ul>
                                )}
                            </div>
                        </div>
                    )}

                    {drafts.length === 0 && (
                        <div className="p-4 text-center border border-dashed border-strong rounded-lg text-sm text-muted" data-testid="outcomes-empty">
                            No outcomes declared. Every outcome an agent reports will be judged undeclared, and no
                            completed run can count as a success.
                        </div>
                    )}

                    {drafts.map((o, i) => (
                        <OutcomeCard
                            key={i}
                            index={i}
                            outcome={o}
                            err={err}
                            onChange={(patch) => updateOutcome(i, patch)}
                            onRemove={() => edit(drafts.filter((_, k) => k !== i))}
                            onAddVariant={() => updateOutcome(i, { variants: [...o.variants, emptyVariantDraft()] })}
                            onChangeVariant={(j, patch) => updateVariant(i, j, patch)}
                            onRemoveVariant={(j) => updateOutcome(i, { variants: o.variants.filter((_, k) => k !== j) })}
                        />
                    ))}

                    <div className="flex items-center justify-between gap-3">
                        <button
                            type="button"
                            disabled={busy || drafts.length >= MAX_OUTCOMES}
                            onClick={() => edit([...drafts, emptyOutcomeDraft()])}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                            data-testid="outcomes-add"
                        >
                            <Plus className="w-4 h-4" />
                            Add outcome
                        </button>
                        {typedIds.length > 0 && (
                            <p className="text-[11px] text-muted text-right" data-testid="outcomes-reportable">
                                The agent may report: <span className="font-mono">{typedIds.join(' · ')}</span>
                            </p>
                        )}
                    </div>
                </>
            )}
        </div>
    );
}

/** What the gateway judges against right now: the active contract's
 *  catalogue, not the form. */
function JudgedAgainst({ version, ids }: { version: number | null; ids: string[] }) {
    return (
        <div className="p-3 border border-default rounded-lg bg-surface-alt text-xs" data-testid="outcomes-judged-against">
            {version && ids.length > 0 ? (
                <>
                    <p className="text-default">
                        <span className="font-medium">Judged against contract v{version}.</span> Reported outcomes are checked
                        against the catalogue signed in the active contract:
                    </p>
                    <p className="mt-1 font-mono text-secondary break-words">{ids.join(' · ')}</p>
                </>
            ) : (
                <p className="text-amber-800" data-testid="outcomes-none-active">
                    {version
                        ? `The active contract v${version} declares no outcomes`
                        : 'No contract is active'}{' '}
                    — every outcome an agent reports is judged undeclared until a contract carrying a catalogue is
                    promoted.
                </p>
            )}
        </div>
    );
}

function OutcomeCard({
    index,
    outcome,
    err,
    onChange,
    onRemove,
    onAddVariant,
    onChangeVariant,
    onRemoveVariant,
}: {
    index: number;
    outcome: OutcomeDraft;
    err: (path: string) => string | undefined;
    onChange: (patch: Partial<OutcomeDraft>) => void;
    onRemove: () => void;
    onAddVariant: () => void;
    onChangeVariant: (j: number, patch: Partial<VariantDraft>) => void;
    onRemoveVariant: (j: number) => void;
}) {
    const path = `outcomes.${index}`;
    const tid = `outcome-${index}`;
    return (
        <div className="bg-surface border border-default rounded-lg p-4 space-y-3" data-testid={tid}>
            <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto] gap-3 items-start">
                <div>
                    <label className={SMALL_LABEL} htmlFor={`${tid}-id`}>
                        Id
                    </label>
                    <input
                        id={`${tid}-id`}
                        className={`${INPUT} font-mono`}
                        value={outcome.id}
                        onChange={(e) => onChange({ id: e.target.value })}
                        placeholder="application_processed"
                        aria-invalid={Boolean(err(`${path}.id`))}
                        data-testid={`${tid}-id`}
                    />
                    <FieldError message={err(`${path}.id`)} testId={`${tid}-error-id`} />
                </div>
                <div>
                    <label className={SMALL_LABEL} htmlFor={`${tid}-label`}>
                        Label
                    </label>
                    <input
                        id={`${tid}-label`}
                        className={INPUT}
                        value={outcome.label}
                        onChange={(e) => onChange({ label: e.target.value })}
                        placeholder="Application processed"
                        aria-invalid={Boolean(err(`${path}.label`))}
                        data-testid={`${tid}-label`}
                    />
                    <FieldError message={err(`${path}.label`)} testId={`${tid}-error-label`} />
                </div>
                <button
                    type="button"
                    onClick={onRemove}
                    className="mt-4 p-1.5 text-muted hover:text-red-700"
                    title="Remove this outcome"
                    aria-label={`Remove outcome ${outcome.id || index + 1}`}
                    data-testid={`${tid}-remove`}
                >
                    <Trash2 className="w-4 h-4" />
                </button>
            </div>

            <div>
                <label className={SMALL_LABEL} htmlFor={`${tid}-description`}>
                    Description (optional)
                </label>
                <textarea
                    id={`${tid}-description`}
                    rows={1}
                    className={INPUT}
                    value={outcome.description}
                    onChange={(e) => onChange({ description: e.target.value })}
                    data-testid={`${tid}-description`}
                />
                <FieldError message={err(`${path}.description`)} />
            </div>

            <div>
                <label className="flex items-center gap-2 text-sm text-default">
                    <input
                        type="checkbox"
                        checked={outcome.counts_toward_baseline}
                        onChange={(e) => onChange({ counts_toward_baseline: e.target.checked })}
                        data-testid={`${tid}-counts`}
                    />
                    Counts toward the baseline — this outcome is the work
                </label>
                {!outcome.counts_toward_baseline && (
                    <p className="mt-1 ml-6 text-xs text-muted" data-testid={`${tid}-no-work-note`}>
                        A no-work outcome (for example nothing to do): its runs are never a success, never in the success
                        rate, and never averaged into cost or run length — they are reported as no-work overhead. Turning
                        an outcome off is a widening the approver is shown.
                    </p>
                )}
                <FieldError message={err(`${path}.counts_toward_baseline`)} />
            </div>

            <ExpectEditor
                expect={outcome.expect}
                path={`${path}.expect`}
                testId={`${tid}-expect`}
                err={err}
                onChange={(expect) => onChange({ expect })}
                title={outcome.variants.length ? 'Expected evidence (every variant)' : 'Expected evidence'}
            />

            <div className="border-t border-default pt-3 space-y-2">
                <div className="flex items-center gap-2">
                    <p className="text-xs font-medium text-default">Variants</p>
                    <HelpTip
                        title="Variants"
                        text="Domain states of the same outcome — approved, declined, referred. When an outcome declares variants the agent must report one of them (outcome.variant); a bare outcome id is then undeclared. Baselines and the outcome mix are learned per variant, so a change in the mix is never mistaken for a run behaving differently. A variant's expectations override the outcome's."
                    />
                </div>
                {outcome.variants.length === 0 && (
                    <p className="text-xs text-muted">None — the agent reports {outcome.id || 'this outcome'} bare.</p>
                )}
                <FieldError message={err(`${path}.variants`)} testId={`${tid}-error-variants`} />
                {outcome.variants.map((v, j) => (
                    <VariantRow
                        key={j}
                        variant={v}
                        path={`${path}.variants.${j}`}
                        testId={`${tid}-variant-${j}`}
                        outcomeId={outcome.id}
                        err={err}
                        onChange={(patch) => onChangeVariant(j, patch)}
                        onRemove={() => onRemoveVariant(j)}
                    />
                ))}
                <button
                    type="button"
                    disabled={outcome.variants.length >= MAX_VARIANTS}
                    onClick={onAddVariant}
                    className="flex items-center gap-1 px-2 py-1 text-xs border border-strong rounded hover:bg-surface-hover disabled:opacity-50"
                    data-testid={`${tid}-add-variant`}
                >
                    <Plus className="w-3.5 h-3.5" />
                    Add variant
                </button>
            </div>
        </div>
    );
}

function VariantRow({
    variant,
    path,
    testId,
    outcomeId,
    err,
    onChange,
    onRemove,
}: {
    variant: VariantDraft;
    path: string;
    testId: string;
    outcomeId: string;
    err: (path: string) => string | undefined;
    onChange: (patch: Partial<VariantDraft>) => void;
    onRemove: () => void;
}) {
    return (
        <div className="ml-4 pl-3 border-l-2 border-default space-y-2" data-testid={testId}>
            <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto] gap-3 items-start">
                <div>
                    <label className={SMALL_LABEL} htmlFor={`${testId}-id`}>
                        Variant id <span className="font-mono">{outcomeId ? `${outcomeId}.…` : ''}</span>
                    </label>
                    <input
                        id={`${testId}-id`}
                        className={`${INPUT} font-mono`}
                        value={variant.id}
                        onChange={(e) => onChange({ id: e.target.value })}
                        placeholder="loan_approved"
                        aria-invalid={Boolean(err(`${path}.id`))}
                        data-testid={`${testId}-id`}
                    />
                    <FieldError message={err(`${path}.id`)} testId={`${testId}-error-id`} />
                </div>
                <div>
                    <label className={SMALL_LABEL} htmlFor={`${testId}-label`}>
                        Label
                    </label>
                    <input
                        id={`${testId}-label`}
                        className={INPUT}
                        value={variant.label}
                        onChange={(e) => onChange({ label: e.target.value })}
                        aria-invalid={Boolean(err(`${path}.label`))}
                        data-testid={`${testId}-label`}
                    />
                    <FieldError message={err(`${path}.label`)} testId={`${testId}-error-label`} />
                </div>
                <button
                    type="button"
                    onClick={onRemove}
                    className="mt-4 p-1.5 text-muted hover:text-red-700"
                    title="Remove this variant"
                    aria-label={`Remove variant ${variant.id || ''}`}
                    data-testid={`${testId}-remove`}
                >
                    <Trash2 className="w-4 h-4" />
                </button>
            </div>
            <ExpectEditor
                expect={variant.expect}
                path={`${path}.expect`}
                testId={`${testId}-expect`}
                err={err}
                onChange={(expect) => onChange({ expect })}
                title="Expected evidence (overrides the outcome's)"
            />
        </div>
    );
}

const EXPECT_FIELDS: { key: keyof ExpectDraft; label: string; placeholder: string }[] = [
    { key: 'max_actions', label: 'Max actions', placeholder: 'no bound' },
    { key: 'max_model_calls', label: 'Max model calls', placeholder: 'no bound' },
    { key: 'max_cost_usd', label: 'Max cost (USD)', placeholder: 'no bound' },
    { key: 'requires_capabilities', label: 'Requires capabilities', placeholder: 'tool names, comma-separated' },
    { key: 'forbids_capabilities', label: 'Forbids capabilities', placeholder: 'tool names, comma-separated' },
];

/** The `expect` bounds: what a run reporting this outcome must look like.
 *  Empty fields are no bound. */
function ExpectEditor({
    expect,
    path,
    testId,
    err,
    onChange,
    title,
}: {
    expect: ExpectDraft;
    path: string;
    testId: string;
    err: (path: string) => string | undefined;
    onChange: (expect: ExpectDraft) => void;
    title: string;
}) {
    return (
        <div data-testid={testId}>
            <div className="flex items-center gap-1 mb-1">
                <p className="text-[11px] font-medium text-secondary">{title}</p>
                <HelpTip
                    title="Expected evidence"
                    text="Optional bounds a run reporting this outcome must meet, checked by the gateway from the run ledger: at most this many actions, model calls or dollars; capabilities (tool names) the run must have used, or must not have. A run that breaks them is a conformance finding. Raising or removing a bound is a widening the approver is shown."
                />
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {EXPECT_FIELDS.map((f) => (
                    <div key={f.key} className={f.key.endsWith('capabilities') ? 'sm:col-span-3 md:col-span-1' : ''}>
                        <label className={SMALL_LABEL} htmlFor={`${testId}-${f.key}`}>
                            {f.label}
                        </label>
                        <input
                            id={`${testId}-${f.key}`}
                            className={INPUT}
                            value={expect[f.key]}
                            placeholder={f.placeholder}
                            onChange={(e) => onChange({ ...expect, [f.key]: e.target.value })}
                            aria-invalid={Boolean(err(`${path}.${f.key}`))}
                            data-testid={`${testId}-${f.key}`}
                        />
                        <FieldError message={err(`${path}.${f.key}`)} testId={`${testId}-error-${f.key}`} />
                    </div>
                ))}
            </div>
            <FieldError message={err(path)} testId={`${testId}-error`} />
        </div>
    );
}

export default OutcomesPanel;
