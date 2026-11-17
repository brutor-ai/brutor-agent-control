/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance Checks → blind review queue.
 *
 * Each week a deterministic stratified sample of a judged check's verdicts
 * is re-graded by a person who does NOT see what the judge said. The grade
 * is sealed as an `adjudication` record, and agreement per (check,
 * evaluator fingerprint) over 30 days — observed agreement, κ, a Wilson
 * 95 % interval, n — is stated beside every judged row.
 *
 * Blindness is enforced twice: the CP withholds `judged_result` while a
 * review is pending, and this component never reads the judged fields of a
 * pending review at all (so a server that leaked them would still not put
 * them on screen). The judge's answer is shown only in the response to a
 * submitted grade, next to the sealed adjudication record id.
 */

import { useCallback, useState } from 'react';
import { CalendarPlus, Loader2, Scale } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { errorMessage } from '../../lib/narrow';
import { shortHash } from '../../lib/complianceEvidenceTypes';
import {
    HUMAN_RESULTS,
    type CheckAgreement,
    type CheckReview,
    type CheckReviewGrade,
    type HumanResult,
} from '../../lib/evidenceSurfaceTypes';
import { HelpTip } from '../ui/HelpTip';

interface Props {
    checkId: string;
}

/** What a pending row may show. Built field by field so the judged result
 *  can never ride along into the render. */
interface BlindRow {
    id: string;
    sample_key: string;
    week: string | null;
    assessment_record_id: string | null;
    evaluator_fingerprint: string;
}

function blindRow(r: CheckReview): BlindRow {
    return {
        id: r.id,
        sample_key: r.sample_key,
        week: r.week ?? null,
        assessment_record_id: r.assessment_record_id ?? null,
        evaluator_fingerprint: r.evaluator_fingerprint,
    };
}

const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? '—' : v.toFixed(digits));

export function AgreementSummary({ agreement }: { agreement: CheckAgreement }) {
    const [lo, hi] = agreement.ci95 ?? [];
    return (
        <div className="rounded-lg border border-default bg-surface p-2.5 text-xs space-y-1" data-testid="review-agreement">
            <div className="flex items-center gap-1.5 font-medium text-default">
                <Scale className="w-3.5 h-3.5 text-primary-600" /> Human agreement with the judge
                <HelpTip text="Blind human grades against the judge's verdicts for the current evaluator fingerprint over the window. Observed agreement with Cohen's κ and a Wilson 95 % interval. Fewer than 10 graded reviews is an insufficient sample; the counter resets when the judge (model, prompt or rubric) changes. Below the threshold the check raises judge_uncalibrated." />
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-secondary">
                <span data-testid="agreement-observed">
                    observed <strong className="text-default">{fmt(agreement.observed_agreement)}</strong>
                </span>
                <span data-testid="agreement-ci">
                    95 % CI [{fmt(lo)}–{fmt(hi)}]
                </span>
                <span data-testid="agreement-kappa">κ {fmt(agreement.kappa)}</span>
                {agreement.mae !== null && agreement.mae !== undefined && <span>MAE {fmt(agreement.mae, 3)}</span>}
                <span data-testid="agreement-n">
                    n = {agreement.n}
                    {agreement.blind ? ', blind' : ''}
                </span>
                <span>{agreement.window_days} days</span>
            </div>
            {agreement.insufficient_sample && (
                <p className="text-amber-800" data-testid="agreement-insufficient">
                    Insufficient sample — fewer than 10 graded reviews for this judge; agreement is not stated yet.
                </p>
            )}
            {!agreement.insufficient_sample && agreement.below_threshold && (
                <p className="text-red-700" data-testid="agreement-below">
                    Below the {agreement.threshold} threshold — the judge is treated as uncalibrated.
                </p>
            )}
            {agreement.evaluator_fingerprint && (
                <p className="text-muted font-mono" title={agreement.evaluator_fingerprint}>
                    judge {shortHash(agreement.evaluator_fingerprint, 16)}
                </p>
            )}
        </div>
    );
}

interface Draft {
    result: HumanResult | '';
    score: string;
}

export default function BlindReviewPanel({ checkId }: Props) {
    const { gatewayClient } = useGateway();
    const [drafts, setDrafts] = useState<Record<string, Draft>>({});
    const [submitting, setSubmitting] = useState<string | null>(null);
    const [graded, setGraded] = useState<CheckReviewGrade[]>([]);
    const [rowError, setRowError] = useState<Record<string, string>>({});
    const [week, setWeek] = useState('');
    const [sampling, setSampling] = useState(false);
    const [sampleNote, setSampleNote] = useState('');

    const loader = useCallback(async () => {
        const r = await gatewayClient.listCheckReviews(checkId, { status: 'pending' });
        return {
            // Only the blind projection is kept in state.
            pending: r.items.filter((i) => i.status === 'pending').map(blindRow),
            counts: r.counts,
            agreement: r.agreement,
        };
    }, [gatewayClient, checkId]);
    const { data, error, reload, setData } = useKeyedLoad(`reviews:${checkId}`, loader, 'Failed to load the review queue');

    const draftFor = (id: string): Draft => drafts[id] ?? { result: '', score: '' };

    const submit = async (row: BlindRow) => {
        const d = draftFor(row.id);
        if (!d.result) return;
        const score = d.score.trim() === '' ? null : Number(d.score);
        if (score !== null && (Number.isNaN(score) || score < 0 || score > 1)) {
            setRowError((p) => ({ ...p, [row.id]: 'Score must be between 0 and 1.' }));
            return;
        }
        setSubmitting(row.id);
        setRowError((p) => ({ ...p, [row.id]: '' }));
        try {
            const res = await gatewayClient.gradeCheckReview(checkId, row.id, { human_result: d.result, human_score: score });
            setGraded((g) => [res, ...g]);
            setData((prev) =>
                prev
                    ? {
                          ...prev,
                          pending: prev.pending.filter((p) => p.id !== row.id),
                          agreement: res.agreement,
                          counts: { ...prev.counts, pending: Math.max(0, (prev.counts.pending ?? 1) - 1), graded: (prev.counts.graded ?? 0) + 1 },
                      }
                    : prev,
            );
        } catch (e: unknown) {
            setRowError((p) => ({ ...p, [row.id]: errorMessage(e, 'Grade could not be saved') }));
        } finally {
            setSubmitting(null);
        }
    };

    const drawSample = async () => {
        setSampling(true);
        setSampleNote('');
        try {
            const r = await gatewayClient.drawCheckReviewSample(checkId, week || undefined);
            setSampleNote(
                r.created > 0
                    ? `${r.created} verdict${r.created === 1 ? '' : 's'} sampled for the week of ${r.week}.`
                    : `The week of ${r.week} is already sampled (or had no judged verdicts).`,
            );
            reload();
        } catch (e: unknown) {
            setSampleNote(`Sampling failed: ${errorMessage(e)}`);
        } finally {
            setSampling(false);
        }
    };

    return (
        <div className="space-y-2" data-testid="blind-review-panel">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <p className="text-xs font-medium text-secondary">
                    Blind review queue
                    {data && (
                        <span className="font-normal text-muted" data-testid="review-counts">
                            {' '}
                            · {data.counts.pending ?? 0} pending · {data.counts.graded ?? 0} graded
                        </span>
                    )}
                </p>
                <div className="flex items-center gap-1.5">
                    <input
                        type="date"
                        value={week}
                        onChange={(e) => setWeek(e.target.value)}
                        aria-label="Week to sample (any day; default last week)"
                        className="px-1.5 py-0.5 text-xs rounded border border-default bg-surface"
                        data-testid="review-sample-week"
                    />
                    <button
                        type="button"
                        onClick={() => void drawSample()}
                        disabled={sampling}
                        className="inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded-lg border border-default hover:bg-surface disabled:opacity-50"
                        data-testid="review-draw-sample"
                    >
                        {sampling ? <Loader2 className="w-3 h-3 animate-spin" /> : <CalendarPlus className="w-3 h-3" />}
                        Draw sample
                    </button>
                </div>
            </div>
            {sampleNote && (
                <p className="text-xs text-secondary" data-testid="review-sample-note">
                    {sampleNote}
                </p>
            )}
            {error && (
                <p className="text-xs text-red-700" data-testid="review-error">
                    {error}
                </p>
            )}
            {data && <AgreementSummary agreement={data.agreement} />}

            {data && data.pending.length === 0 && (
                <p className="text-xs text-muted" data-testid="review-empty">
                    Nothing awaiting a blind grade. Samples are drawn weekly; draw one now for a past week if needed.
                </p>
            )}

            {data && data.pending.length > 0 && (
                <ul className="divide-y divide-default border border-default rounded-lg bg-surface">
                    {data.pending.map((row) => {
                        const d = draftFor(row.id);
                        return (
                            <li key={row.id} className="p-2 text-xs space-y-1.5" data-testid={`review-row-${row.id}`}>
                                <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-secondary">
                                    <span className="font-mono text-default" title={row.sample_key}>
                                        {shortHash(row.sample_key, 24)}
                                    </span>
                                    {row.week && <span>week of {row.week}</span>}
                                    {row.assessment_record_id && (
                                        <span className="font-mono" title={row.assessment_record_id}>
                                            assessment {shortHash(row.assessment_record_id, 12)}
                                        </span>
                                    )}
                                    <span className="text-muted">judge's verdict withheld until you grade</span>
                                </div>
                                <div className="flex flex-wrap items-center gap-2">
                                    {HUMAN_RESULTS.map((h) => (
                                        <label key={h.value} className="inline-flex items-center gap-1">
                                            <input
                                                type="radio"
                                                name={`grade-${row.id}`}
                                                value={h.value}
                                                checked={d.result === h.value}
                                                onChange={() => setDrafts((p) => ({ ...p, [row.id]: { ...d, result: h.value } }))}
                                                data-testid={`review-grade-${row.id}-${h.value}`}
                                            />
                                            {h.label}
                                        </label>
                                    ))}
                                    <input
                                        type="number"
                                        min={0}
                                        max={1}
                                        step={0.01}
                                        placeholder="score 0–1 (optional)"
                                        value={d.score}
                                        onChange={(e) => setDrafts((p) => ({ ...p, [row.id]: { ...d, score: e.target.value } }))}
                                        className="w-36 px-1.5 py-0.5 rounded border border-default bg-surface"
                                        aria-label="Human score"
                                        data-testid={`review-score-${row.id}`}
                                    />
                                    <button
                                        type="button"
                                        disabled={!d.result || submitting !== null}
                                        onClick={() => void submit(row)}
                                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
                                        data-testid={`review-submit-${row.id}`}
                                    >
                                        {submitting === row.id && <Loader2 className="w-3 h-3 animate-spin" />}
                                        Submit grade
                                    </button>
                                </div>
                                {rowError[row.id] && (
                                    <p className="text-red-700" data-testid={`review-row-error-${row.id}`}>
                                        {rowError[row.id]}
                                    </p>
                                )}
                            </li>
                        );
                    })}
                </ul>
            )}

            {graded.length > 0 && (
                <div className="space-y-1" data-testid="review-graded">
                    <p className="text-xs font-medium text-secondary">Graded just now</p>
                    {graded.map((g) => (
                        <div key={g.review.id} className="rounded border border-default bg-surface p-2 text-xs space-y-0.5" data-testid={`review-graded-${g.review.id}`}>
                            <div className="text-secondary">
                                <span className="font-mono">{shortHash(g.review.sample_key, 24)}</span> — you:{' '}
                                <strong className="text-default">{g.review.human_result ?? '—'}</strong> · judge:{' '}
                                <strong className="text-default">{g.review.judged_result ?? '—'}</strong> ·{' '}
                                {g.agrees_with_judge ? 'agrees' : 'disagrees'}
                            </div>
                            {g.seal.sealed && g.seal.record_id ? (
                                <div className="text-green-800" data-testid={`review-sealed-${g.review.id}`}>
                                    Sealed as adjudication record <span className="font-mono">{g.seal.record_id}</span>
                                </div>
                            ) : (
                                <div className="text-amber-800" data-testid={`review-unsealed-${g.review.id}`}>
                                    Grade saved, not sealed{g.seal.seal_error ? ` (${g.seal.seal_error})` : ''} — it counts toward agreement,
                                    but no adjudication record exists for it.
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
