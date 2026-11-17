/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Declared outcomes — the vocabularies, labels and pure helpers
 * every surface reads: the run list, run details, the Signals sub-tabs, the
 * Contract tab's Outcomes editor and the dependency dialog.
 *
 * A finished run is judged on three separate questions, and every label here
 * says which one it answers:
 *
 *   1. did the run complete without errors? — the terminal state, and when
 *      it did not, the ERROR CAUSE (the gateway's word);
 *   2. was the outcome one the contract declares? — the CONFORMANCE of the
 *      agent's reported outcome id (the gateway checking the agent's word);
 *   3. does that outcome count toward the operating baseline? — the
 *      contract's word, per declared outcome.
 *
 * The control plane combines the three ONCE into the RUN CLASS; nothing here
 * re-derives it. `no_work` (an empty poll that ended as designed) is never
 * success and is never painted green.
 *
 * The catalogue editor's validation mirrors the control plane's
 * `validate_catalogue` so the form can say what is wrong before the server
 * does; the server's 422 (`{loc, msg}` per field) is still read back onto the
 * same field paths.
 */

import type { OutcomeConformance, RunClass } from './aiSystemTypes';
import type { components } from './generated/control-plane-api';
import { getArray, getRecord, getString, isRecord } from './narrow';

export type OutcomeDecl = components['schemas']['OutcomeDecl'];
export type OutcomeVariantDecl = components['schemas']['OutcomeVariantDecl'];
export type OutcomeExpect = components['schemas']['OutcomeExpect'];
export type OutcomesResponse = components['schemas']['OutcomesResponse'];

// ---------------------------------------------------------------------------
// Outcome ids — `<outcome>[.<variant>]`
// ---------------------------------------------------------------------------

/** One part of an outcome id: an outcome or a variant. */
export const OUTCOME_PART_RE = /^[a-z0-9_-]{1,48}$/;
/** A full outcome id, as sent in the outcome header. */
export const OUTCOME_ID_RE = /^[a-z0-9_-]{1,48}(\.[a-z0-9_-]{1,48})?$/;

export interface ParsedOutcomeId {
    outcome: string;
    variant: string | null;
}

/** `application_processed.loan_approved` → outcome + variant; null when the
 *  value is not a well-formed id (a malformed report is still shown verbatim
 *  elsewhere — it is a finding, not something to hide). */
export function parseOutcomeId(id: string | null | undefined): ParsedOutcomeId | null {
    if (!id || !OUTCOME_ID_RE.test(id)) return null;
    const dot = id.indexOf('.');
    return dot < 0 ? { outcome: id, variant: null } : { outcome: id.slice(0, dot), variant: id.slice(dot + 1) };
}

/** The declared outcome (and variant) an id names, mirroring the gateway's
 *  rule: an outcome with variants must be reported with one, an outcome
 *  without variants must be reported bare. Null when not declared. */
export function findDeclared(
    catalogue: OutcomeDecl[] | null | undefined,
    id: string | null | undefined,
): { outcome: OutcomeDecl; variant: OutcomeVariantDecl | null } | null {
    const parsed = parseOutcomeId(id);
    if (!parsed || !catalogue) return null;
    const outcome = catalogue.find((o) => o.id === parsed.outcome);
    if (!outcome) return null;
    const variants = outcome.variants ?? [];
    if (variants.length > 0) {
        const variant = parsed.variant ? variants.find((v) => v.id === parsed.variant) : undefined;
        return variant ? { outcome, variant } : null;
    }
    return parsed.variant ? null : { outcome, variant: null };
}

/** Every id the catalogue lets an agent report: `<outcome>` for an outcome
 *  without variants, `<outcome>.<variant>` for each variant. Sorted. */
export function reportableIds(catalogue: OutcomeDecl[] | null | undefined): string[] {
    const out: string[] = [];
    for (const o of catalogue ?? []) {
        const variants = o.variants ?? [];
        if (variants.length > 0) out.push(...variants.map((v) => `${o.id}.${v.id}`));
        else out.push(o.id);
    }
    return out.sort();
}

/** "Application processed · Recommended: approve" when the catalogue
 *  declares the id, else the id itself. */
export function outcomeLabel(id: string | null | undefined, catalogue?: OutcomeDecl[] | null): string {
    if (!id) return 'No outcome reported';
    const found = findDeclared(catalogue, id);
    if (!found) return id;
    return found.variant ? `${found.outcome.label} · ${found.variant.label}` : found.outcome.label;
}

/** What separates two catalogues, as reportable ids: what the live
 *  catalogue adds to the one the gateway judges against, and what it drops. */
export function catalogueIdChanges(
    active: OutcomeDecl[] | null | undefined,
    live: OutcomeDecl[] | null | undefined,
): { added: string[]; removed: string[] } {
    const a = new Set(reportableIds(active));
    const l = new Set(reportableIds(live));
    return {
        added: [...l].filter((id) => !a.has(id)).sort(),
        removed: [...a].filter((id) => !l.has(id)).sort(),
    };
}

/** True when two normalised catalogues differ in anything the contract
 *  carries (ids, labels, baseline flags, expectations). */
export function cataloguesDiffer(a: OutcomeDecl[] | null | undefined, b: OutcomeDecl[] | null | undefined): boolean {
    return JSON.stringify(a ?? null) !== JSON.stringify(b ?? null);
}

function readExpect(raw: unknown): OutcomeExpect | null {
    if (!isRecord(raw)) return null;
    const out: OutcomeExpect = {};
    for (const key of ['max_actions', 'max_model_calls', 'max_cost_usd'] as const) {
        const v = raw[key];
        if (typeof v === 'number' && Number.isFinite(v)) out[key] = v;
    }
    for (const key of ['requires_capabilities', 'forbids_capabilities'] as const) {
        const v = getArray(raw, key)?.filter((x): x is string => typeof x === 'string');
        if (v?.length) out[key] = v;
    }
    return Object.keys(out).length ? out : null;
}

/** A catalogue read from somewhere the type system cannot vouch for (a
 *  contract's sealed closure): entries without an id or label, or without a
 *  boolean baseline flag, declare nothing — the gateway's own rule. */
export function readCatalogue(raw: unknown): OutcomeDecl[] {
    if (!Array.isArray(raw)) return [];
    const out: OutcomeDecl[] = [];
    for (const item of raw as unknown[]) {
        const id = getString(item, 'id');
        const label = getString(item, 'label');
        const counts = isRecord(item) ? item.counts_toward_baseline : undefined;
        if (!id || !label || typeof counts !== 'boolean') continue;
        const variants = (getArray(item, 'variants') ?? []).flatMap((v): OutcomeVariantDecl[] => {
            const vid = getString(v, 'id');
            const vlabel = getString(v, 'label');
            if (!vid || !vlabel) return [];
            return [{ id: vid, label: vlabel, description: getString(v, 'description') ?? null, expect: readExpect(getRecord(v, 'expect')) }];
        });
        out.push({
            id,
            label,
            counts_toward_baseline: counts,
            description: getString(item, 'description') ?? null,
            expect: readExpect(getRecord(item, 'expect')),
            variants: variants.length ? variants : null,
        });
    }
    return out;
}

/** "≤ 2 actions · no model calls · ≤ $0.0000 · requires a, b · forbids c";
 *  empty when nothing is expected. */
export function expectSummary(expect: OutcomeExpect | null | undefined): string {
    if (!expect) return '';
    const parts: string[] = [];
    const bound = (n: number, one: string, many: string) => (n === 0 ? `no ${many}` : `≤ ${n} ${n === 1 ? one : many}`);
    if (typeof expect.max_actions === 'number') parts.push(bound(expect.max_actions, 'action', 'actions'));
    if (typeof expect.max_model_calls === 'number') parts.push(bound(expect.max_model_calls, 'model call', 'model calls'));
    if (typeof expect.max_cost_usd === 'number') parts.push(expect.max_cost_usd === 0 ? 'costs nothing' : `≤ $${expect.max_cost_usd}`);
    if (expect.requires_capabilities?.length) parts.push(`requires ${expect.requires_capabilities.join(', ')}`);
    if (expect.forbids_capabilities?.length) parts.push(`forbids ${expect.forbids_capabilities.join(', ')}`);
    return parts.join(' · ');
}

// ---------------------------------------------------------------------------
// Question 2 — conformance
// ---------------------------------------------------------------------------

export const CONFORMANCE_LABELS: Record<OutcomeConformance, string> = {
    declared: 'Declared',
    undeclared: 'Undeclared',
    unreported: 'Not reported',
};

export const CONFORMANCE_HELP: Record<OutcomeConformance, string> = {
    declared: 'The outcome the agent reported is one the active contract declares.',
    undeclared:
        'The agent reported an outcome the active contract does not declare (or a malformed one). A completed run ending there is a deviation, not a success.',
    unreported: 'The run finished without reporting an outcome. A completed run with no outcome is a deviation, not a success.',
};

export const CONFORMANCE_CLASSES: Record<OutcomeConformance, string> = {
    declared: 'bg-surface-alt text-default border border-default',
    undeclared: 'bg-red-100 text-red-800',
    unreported: 'bg-amber-100 text-amber-800',
};

// ---------------------------------------------------------------------------
// The run class — THE classification, read never re-derived
// ---------------------------------------------------------------------------

export const RUN_CLASSES: RunClass[] = ['success', 'no_work', 'deviation', 'failed', 'not_decisive'];

export const RUN_CLASS_LABELS: Record<RunClass, string> = {
    success: 'Success',
    no_work: 'No work',
    deviation: 'Deviation',
    failed: 'Failed',
    not_decisive: 'Not decisive',
};

export const RUN_CLASS_HELP: Record<RunClass, string> = {
    success:
        'Completed without errors and ended in a declared outcome that counts toward the baseline: the work the system exists to do.',
    no_work:
        'Completed and ended in a declared outcome that does not count toward the baseline — for example a poll that found nothing to do. ' +
        'Not a success and not a failure: left out of the success rate, the cost of the work and run length, and reported as no-work overhead.',
    deviation:
        'Completed, but the outcome was undeclared or not reported. Counts against the success rate: the run ended somewhere the contract never approved.',
    failed:
        'Errored or exhausted (a policy block included), whatever it was doing. Counts against the success rate; its error cause says why.',
    not_decisive: 'Abandoned or cancelled. Neither a success nor a failure, so it is left out of the success rate.',
};

/** Chip classes. No-work is a neutral grey — never green: an empty poll is
 *  the system checking, not the system working. */
export const RUN_CLASS_CLASSES: Record<RunClass, string> = {
    success: 'bg-emerald-100 text-emerald-800',
    no_work: 'bg-slate-100 text-slate-700',
    deviation: 'bg-amber-100 text-amber-800',
    failed: 'bg-red-100 text-red-800',
    not_decisive: 'bg-surface-alt text-muted border border-default',
};

/** Chart colours per class (the trend bars). */
export const RUN_CLASS_COLORS: Record<RunClass, string> = {
    success: '#10b981',
    no_work: '#94a3b8',
    deviation: '#f59e0b',
    failed: '#ef4444',
    not_decisive: '#cbd5e1',
};

/** What the success rate is, in the operator's words — shared with the
 *  Overview's reliability help and the Outcomes tile. */
export const SUCCESS_RATE_HELP =
    'Success = runs that completed without errors AND ended in a declared outcome that counts toward the baseline. ' +
    'The rate is success ÷ (success + deviations + every failed run). Deviations are completed runs whose outcome was undeclared or not reported; ' +
    'failed runs are every errored or exhausted run, policy blocks included. Runs whose declared outcome does not count (an empty poll) are in neither, ' +
    'so they cannot inflate the rate; abandoned and cancelled runs are not decisive.';

/** The no-work overhead card's "?". */
export const NO_WORK_HELP =
    'Runs that completed in a declared outcome that does not count toward the baseline — for example a poll that found nothing to do. ' +
    'They are the system checking, not the system working: never averaged into cost per processed outcome or run length, never counted as successes. ' +
    'This is what they cost in this range, reported on its own so it is neither hidden nor mistaken for the work.';

export function isRunClass(v: unknown): v is RunClass {
    return typeof v === 'string' && (RUN_CLASSES as string[]).includes(v);
}

// ---------------------------------------------------------------------------
// Question 1 — why an execution failed
// ---------------------------------------------------------------------------

/** In the gateway's order of precedence. */
export const ERROR_CAUSES = [
    'blocked_by_policy',
    'rate_limited',
    'model_unavailable',
    'model_rejected',
    'tool_failed',
    'skill_failed',
    'agent_unreachable',
    'delegate_deviated',
    'iteration_limit',
    'client_reported',
] as const;

export type ErrorCause = (typeof ERROR_CAUSES)[number];

export const ERROR_CAUSE_LABELS: Record<ErrorCause, string> = {
    blocked_by_policy: 'Blocked by policy',
    rate_limited: 'Rate limited',
    model_unavailable: 'Model unavailable',
    model_rejected: 'Model rejected the request',
    tool_failed: 'Tool failed',
    skill_failed: 'Skill failed',
    agent_unreachable: 'Delegate agent unreachable',
    delegate_deviated: 'Delegate answered outside its contract',
    iteration_limit: 'Iteration limit reached',
    client_reported: 'Client reported an error',
};

export const ERROR_CAUSE_HELP: Record<ErrorCause, string> = {
    blocked_by_policy:
        'The gateway refused a call — a guardrail, an argument policy, an access rule, or an approval denied or expired. The system could not do its job, so it counts against the success rate; look at governance configuration rather than at an outage.',
    rate_limited: 'A call was refused with 429 (too many requests), by a model provider, a tool or a gateway limit.',
    model_unavailable: 'A model call failed with a server error, a connection error or a timeout — usually a provider outage.',
    model_rejected: 'A model call was refused with a client error (4xx other than 429) — a bad request, an unknown model or missing access.',
    tool_failed: 'A tool (MCP) call failed — it returned an error or the connection to the server failed.',
    skill_failed: 'A skill run failed.',
    agent_unreachable: 'A call to another agent failed or timed out.',
    delegate_deviated: 'Another agent answered with an outcome its own contract does not declare, or with none.',
    iteration_limit: 'The client closed the run as exhausted: it hit its own iteration limit without an answer.',
    client_reported: 'The client closed the run as errored, and no failing call explains it.',
};

/** Keys the health verdict uses beside the error causes for deviations. */
const EXTRA_CAUSE_LABELS: Record<string, string> = {
    unknown: 'Cause not recorded',
    outcome_undeclared: 'Ended in an undeclared outcome',
    outcome_unreported: 'Reported no outcome',
};

export function isErrorCause(v: unknown): v is ErrorCause {
    return typeof v === 'string' && (ERROR_CAUSES as readonly string[]).includes(v);
}

/** A human label for any cause key the API sends — the ten error causes,
 *  `unknown`, and the two deviation keys; anything else is humanised. */
export function errorCauseLabel(cause: string | null | undefined): string {
    if (!cause) return '—';
    if (isErrorCause(cause)) return ERROR_CAUSE_LABELS[cause];
    const extra = EXTRA_CAUSE_LABELS[cause];
    if (extra) return extra;
    const words = cause.replace(/[_-]+/g, ' ').trim();
    return words ? words.charAt(0).toUpperCase() + words.slice(1) : cause;
}

// ---------------------------------------------------------------------------
// The outcome mix — rows grouped by outcome, variants underneath
// ---------------------------------------------------------------------------

export type OutcomeRow = components['schemas']['OutcomeRow'];

export interface OutcomeMixGroup {
    /** The outcome part of the id; null for runs that reported none. */
    outcome: string | null;
    runs: number;
    share: number;
    cost_usd: number;
    rows: OutcomeRow[];
}

/** The outcome signal's rows grouped by outcome (variants under their
 *  outcome), largest first; runs with no outcome last — listed, never
 *  dropped. A malformed id is its own group under its verbatim value. */
export function outcomeMixGroups(rows: OutcomeRow[] | null | undefined): OutcomeMixGroup[] {
    const groups = new Map<string, OutcomeMixGroup>();
    for (const row of rows ?? []) {
        const outcome = row.outcome_id ? (parseOutcomeId(row.outcome_id)?.outcome ?? row.outcome_id) : null;
        const key = outcome ?? '';
        let g = groups.get(key);
        if (!g) {
            g = { outcome, runs: 0, share: 0, cost_usd: 0, rows: [] };
            groups.set(key, g);
        }
        g.runs += row.runs ?? 0;
        g.share += row.share ?? 0;
        g.cost_usd += row.cost_usd ?? 0;
        g.rows.push(row);
    }
    for (const g of groups.values()) {
        g.rows.sort((a, b) => (b.runs ?? 0) - (a.runs ?? 0) || (a.outcome_id ?? '').localeCompare(b.outcome_id ?? ''));
    }
    return [...groups.values()].sort((a, b) => {
        if ((a.outcome === null) !== (b.outcome === null)) return a.outcome === null ? 1 : -1;
        return (b.runs ?? 0) - (a.runs ?? 0) || (a.outcome ?? '').localeCompare(b.outcome ?? '');
    });
}

/** The class counts in display order, with each class's share of all runs. */
export function runClassBreakdown(
    byClass: Record<string, number> | null | undefined,
): { cls: RunClass; runs: number; share: number }[] {
    const counts = byClass ?? {};
    const total = RUN_CLASSES.reduce((n, c) => n + (counts[c] ?? 0), 0);
    return RUN_CLASSES.map((cls) => {
        const runs = counts[cls] ?? 0;
        return { cls, runs, share: total ? runs / total : 0 };
    });
}

// ---------------------------------------------------------------------------
// The catalogue editor — drafts, validation, payload
// ---------------------------------------------------------------------------

export const MAX_OUTCOMES = 32;
export const MAX_VARIANTS = 32;
export const LABEL_MAX = 128;
export const DESCRIPTION_MAX = 2000;

/** The form keeps raw text; numbers and lists are parsed on save. */
export interface ExpectDraft {
    max_actions: string;
    max_model_calls: string;
    max_cost_usd: string;
    /** Comma- or newline-separated capability names. */
    requires_capabilities: string;
    forbids_capabilities: string;
}

export interface VariantDraft {
    id: string;
    label: string;
    description: string;
    expect: ExpectDraft;
}

export interface OutcomeDraft {
    id: string;
    label: string;
    description: string;
    counts_toward_baseline: boolean;
    expect: ExpectDraft;
    variants: VariantDraft[];
}

/** Field path → message, keyed like the server's `loc`
 *  (`outcomes.0.variants.1.expect.max_actions`). */
export type CatalogueErrors = Record<string, string>;

export function emptyExpectDraft(): ExpectDraft {
    return { max_actions: '', max_model_calls: '', max_cost_usd: '', requires_capabilities: '', forbids_capabilities: '' };
}

export function emptyVariantDraft(): VariantDraft {
    return { id: '', label: '', description: '', expect: emptyExpectDraft() };
}

export function emptyOutcomeDraft(): OutcomeDraft {
    return { id: '', label: '', description: '', counts_toward_baseline: true, expect: emptyExpectDraft(), variants: [] };
}

function expectToDraft(e: OutcomeExpect | null | undefined): ExpectDraft {
    const n = (v: number | null | undefined) => (v === null || v === undefined ? '' : String(v));
    return {
        max_actions: n(e?.max_actions),
        max_model_calls: n(e?.max_model_calls),
        max_cost_usd: n(e?.max_cost_usd),
        requires_capabilities: (e?.requires_capabilities ?? []).join(', '),
        forbids_capabilities: (e?.forbids_capabilities ?? []).join(', '),
    };
}

export function catalogueToDrafts(catalogue: OutcomeDecl[] | null | undefined): OutcomeDraft[] {
    return (catalogue ?? []).map((o) => ({
        id: o.id,
        label: o.label,
        description: o.description ?? '',
        counts_toward_baseline: o.counts_toward_baseline,
        expect: expectToDraft(o.expect),
        variants: (o.variants ?? []).map((v) => ({
            id: v.id,
            label: v.label,
            description: v.description ?? '',
            expect: expectToDraft(v.expect),
        })),
    }));
}

/** "a, b\nc" → ["a", "b", "c"] — trimmed, empties dropped, de-duplicated. */
export function capabilityList(raw: string): string[] {
    return [...new Set(raw.split(/[,\n]/).map((s) => s.trim()).filter(Boolean))];
}

const WHOLE = /^\d+$/;
const DECIMAL = /^\d+(\.\d+)?$/;

function validateExpect(e: ExpectDraft, path: string, errors: CatalogueErrors): void {
    for (const key of ['max_actions', 'max_model_calls'] as const) {
        const v = e[key].trim();
        if (v && !WHOLE.test(v)) errors[`${path}.${key}`] = `${key} must be a whole number ≥ 0`;
    }
    const cost = e.max_cost_usd.trim();
    if (cost && !DECIMAL.test(cost)) errors[`${path}.max_cost_usd`] = 'max_cost_usd must be a number ≥ 0';
    const required = new Set(capabilityList(e.requires_capabilities));
    const both = capabilityList(e.forbids_capabilities).filter((c) => required.has(c)).sort();
    if (both.length) errors[path] = `capabilities both required and forbidden: ${both.join(', ')}`;
}

function validateText(
    d: { id: string; label: string; description: string },
    path: string,
    errors: CatalogueErrors,
): void {
    if (!OUTCOME_PART_RE.test(d.id)) errors[`${path}.id`] = 'id must match [a-z0-9_-]{1,48}';
    const label = d.label.trim();
    if (!label) errors[`${path}.label`] = 'label is required (1–128 characters)';
    else if (label.length > LABEL_MAX) errors[`${path}.label`] = `label must be at most ${LABEL_MAX} characters`;
    if (d.description.trim().length > DESCRIPTION_MAX) {
        errors[`${path}.description`] = `description must be at most ${DESCRIPTION_MAX} characters`;
    }
}

/** The control plane's catalogue rules, on the form: ids `[a-z0-9_-]{1,48}`
 *  and unique (per catalogue, per outcome's variants), labels 1–128
 *  characters, at most 32 outcomes and 32 variants each, `expect` maxima
 *  whole numbers (cost: a number) ≥ 0, and no capability both required and
 *  forbidden. Empty = valid. */
export function validateCatalogueDraft(drafts: OutcomeDraft[]): CatalogueErrors {
    const errors: CatalogueErrors = {};
    if (drafts.length > MAX_OUTCOMES) errors.outcomes = `at most ${MAX_OUTCOMES} outcomes`;
    const seen = new Set<string>();
    drafts.forEach((o, i) => {
        const path = `outcomes.${i}`;
        validateText(o, path, errors);
        if (OUTCOME_PART_RE.test(o.id)) {
            if (seen.has(o.id)) errors[`${path}.id`] = `duplicate outcome id '${o.id}'`;
            seen.add(o.id);
        }
        validateExpect(o.expect, `${path}.expect`, errors);
        if (o.variants.length > MAX_VARIANTS) errors[`${path}.variants`] = `at most ${MAX_VARIANTS} variants per outcome`;
        const vseen = new Set<string>();
        o.variants.forEach((v, j) => {
            const vpath = `${path}.variants.${j}`;
            validateText(v, vpath, errors);
            if (OUTCOME_PART_RE.test(v.id)) {
                if (vseen.has(v.id)) errors[`${vpath}.id`] = `duplicate variant id '${v.id}'`;
                vseen.add(v.id);
            }
            validateExpect(v.expect, `${vpath}.expect`, errors);
        });
    });
    return errors;
}

function expectPayload(e: ExpectDraft): Record<string, unknown> | undefined {
    const out: Record<string, unknown> = {};
    for (const key of ['max_actions', 'max_model_calls', 'max_cost_usd'] as const) {
        const v = e[key].trim();
        if (v) out[key] = Number(v);
    }
    for (const key of ['requires_capabilities', 'forbids_capabilities'] as const) {
        const list = capabilityList(e[key]);
        if (list.length) out[key] = list;
    }
    return Object.keys(out).length ? out : undefined;
}

/** The PUT body's `outcomes`: the drafts as the catalogue document, empty
 *  optional members omitted (the server normalises the rest). */
export function draftsToCatalogue(drafts: OutcomeDraft[]): Record<string, unknown>[] {
    return drafts.map((o) => {
        const out: Record<string, unknown> = {
            id: o.id.trim(),
            label: o.label.trim(),
            counts_toward_baseline: o.counts_toward_baseline,
        };
        if (o.description.trim()) out.description = o.description.trim();
        const expect = expectPayload(o.expect);
        if (expect) out.expect = expect;
        if (o.variants.length) {
            out.variants = o.variants.map((v) => {
                const vo: Record<string, unknown> = { id: v.id.trim(), label: v.label.trim() };
                if (v.description.trim()) vo.description = v.description.trim();
                const ve = expectPayload(v.expect);
                if (ve) vo.expect = ve;
                return vo;
            });
        }
        return out;
    });
}

/** The server's 422 field errors (`detail: [{loc, msg}]`) on the same paths
 *  the form uses. A FastAPI body error's leading `body` is dropped. Empty
 *  when the error carries none. */
export function catalogueServerErrors(err: unknown): CatalogueErrors {
    const details = getRecord(err, 'details');
    const list = getArray(details, 'detail') ?? getArray(err, 'validationErrors') ?? [];
    const out: CatalogueErrors = {};
    for (const item of list) {
        if (!isRecord(item)) continue;
        const loc = getArray(item, 'loc');
        const msg = getString(item, 'msg');
        if (!loc || !msg) continue;
        const parts = loc.map((p) => String(p));
        if (parts[0] === 'body') parts.shift();
        const key = parts.join('.') || 'outcomes';
        out[key] = out[key] ? `${out[key]}; ${msg}` : msg;
    }
    return out;
}

// ---------------------------------------------------------------------------
// Drift within one outcome
// ---------------------------------------------------------------------------

/** The outcome bucket a drift finding was measured in — continuous metrics
 *  are also learned per outcome / variant, and such a finding says which
 *  kind of work moved. Null for the system-wide baseline (`all`). */
export function driftOutcomeBucket(causeEvidence: Record<string, unknown> | null | undefined): string | null {
    const bucket = getString(causeEvidence, 'bucket');
    return bucket && bucket !== 'all' ? bucket : null;
}

// ---------------------------------------------------------------------------
// Envelope terms that name an outcome — `<term>:<outcome>[.<variant>]`
// ---------------------------------------------------------------------------

export const ENVELOPE_PARAM_SEPARATOR = ':';

/** `"max_outcome_share:a.b"` → { base: 'max_outcome_share', outcome: 'a.b' };
 *  a plain term has `outcome: null`. */
export function splitEnvelopeTerm(key: string): { base: string; outcome: string | null } {
    const i = key.indexOf(ENVELOPE_PARAM_SEPARATOR);
    return i < 0 ? { base: key, outcome: null } : { base: key.slice(0, i), outcome: key.slice(i + 1) };
}

/** The envelope key for one outcome's bound; null when the id is not a
 *  well-formed outcome id (the server would refuse it). */
export function outcomeTermKey(base: string, outcomeId: string): string | null {
    const id = outcomeId.trim();
    return OUTCOME_ID_RE.test(id) ? `${base}${ENVELOPE_PARAM_SEPARATOR}${id}` : null;
}

// ---------------------------------------------------------------------------
// Liveness — the "no processed run" bound
// ---------------------------------------------------------------------------

export const QUIET_UNITS = { minutes: 60, hours: 3600, days: 86400 } as const;
export type QuietUnit = keyof typeof QUIET_UNITS;

/** The control plane's bounds on `max_quiet_processed_seconds`. */
export const QUIET_MIN_SECONDS = 60;
export const QUIET_MAX_SECONDS = 366 * 86400;

/** The form value for a stored bound: the largest unit that divides it. */
export function quietBoundParts(seconds: number | null | undefined): { value: string; unit: QuietUnit } {
    if (!seconds) return { value: '', unit: 'hours' };
    for (const unit of ['days', 'hours', 'minutes'] as const) {
        if (seconds % QUIET_UNITS[unit] === 0) return { value: String(seconds / QUIET_UNITS[unit]), unit };
    }
    return { value: String(Math.round(seconds / 60)), unit: 'minutes' };
}

/** Parse the form: an empty value clears the bound (null); otherwise a
 *  positive number of the unit, between 1 minute and 366 days. */
export function parseQuietBound(value: string, unit: QuietUnit): { seconds: number | null; error?: string } {
    const v = value.trim();
    if (!v) return { seconds: null };
    if (!DECIMAL.test(v)) return { seconds: null, error: 'Enter a number.' };
    const seconds = Math.round(Number(v) * QUIET_UNITS[unit]);
    if (seconds < QUIET_MIN_SECONDS) return { seconds: null, error: 'At least 1 minute.' };
    if (seconds > QUIET_MAX_SECONDS) return { seconds: null, error: 'At most 366 days.' };
    return { seconds };
}

/**
 * The chip for one of the latest finished runs on Signals → Overview: its
 * class, plus why it failed or which outcome it ended in. Falls back to the
 * terminal state for a payload without a class.
 */
export function recentRunChipText(run: {
    terminal_state: string;
    run_class?: string | null;
    outcome_id?: string | null;
    error_cause?: string | null;
}): string {
    if (!isRunClass(run.run_class)) return run.terminal_state;
    const label = RUN_CLASS_LABELS[run.run_class];
    if (run.run_class === 'failed' && run.error_cause) return `${label}: ${errorCauseLabel(run.error_cause).toLowerCase()}`;
    if ((run.run_class === 'success' || run.run_class === 'no_work') && run.outcome_id) return `${label}: ${run.outcome_id}`;
    return label;
}
