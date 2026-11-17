/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Display rules for the Compliance console.
 *
 * One place decides how a statement, a judged row, a deadline or a window
 * reads, so no page can drift into a percentage, a green `unknown`, or a
 * word the vocabulary lint bans:
 *
 *   * counts are `n of m`, never a percentage;
 *   * `not_evaluable` / `undetermined` / `not_covered` are shown as what
 *     they are, never in the colour of `met`;
 *   * a judged row carries its human agreement beside it, never folded
 *     into n / m;
 *   * a deadline is shown, not enforced.
 */

import { getArray, getBoolean, getNumber, getRecord, getString, isRecord } from '../../lib/narrow';
import {
    isVerifierUnavailable,
    type ComplianceBoardObligation,
    type ComplianceMatrixCell,
    type EvidenceBasis,
    type EvidenceVerifyFinding,
} from '../../lib/complianceEvidenceTypes';
import { splitObligations } from '../../lib/obligationTypes';

// ─── n of m ──────────────────────────────────────────────────────────────────

/** `57 of 60`; `—` when nothing was measured (m absent). Never a percentage. */
export function nOfM(n: number | null | undefined, m: number | null | undefined): string {
    if (m === null || m === undefined) return '—';
    return `${(n ?? 0).toLocaleString()} of ${m.toLocaleString()}`;
}

// ─── Statement status ────────────────────────────────────────────────────────

export const STATEMENT_STATUS_LABEL: Record<string, string> = {
    met: 'met',
    not_met: 'not met',
    not_evaluable: 'not evaluable',
    undetermined: 'undetermined',
    indicator: 'indicator',
    declared: 'declared',
    not_covered: 'not covered',
};

/** Only `met` is green. Unknown states are amber or neutral — never green. */
export const STATEMENT_STATUS_CHIP: Record<string, string> = {
    met: 'bg-green-100 text-green-800',
    not_met: 'bg-red-100 text-red-800',
    not_evaluable: 'bg-amber-100 text-amber-800',
    undetermined: 'bg-amber-100 text-amber-800',
    indicator: 'bg-surface-raised text-secondary',
    declared: 'bg-surface-raised text-secondary',
    not_covered: 'bg-amber-100 text-amber-800',
};

export function statusLabel(status: string | null | undefined): string {
    if (!status) return '—';
    return STATEMENT_STATUS_LABEL[status] ?? status.replace(/_/g, ' ');
}

export function statusChip(status: string | null | undefined): string {
    return STATEMENT_STATUS_CHIP[status ?? ''] ?? 'bg-surface-raised text-secondary';
}

/** A statement as one line: `met 57 of 60` / `not evaluable` / `declared`. */
export function statementSummary(status: string, n?: number | null, m?: number | null): string {
    const label = statusLabel(status);
    if ((status === 'met' || status === 'not_met') && m !== null && m !== undefined) {
        return `${label} · ${nOfM(n, m)}`;
    }
    return label;
}

/** `recomputed: 3 of 4 statements met · judged: 1 of 1 statements met` from
 *  a cell's `by_basis` — bases side by side, never summed together. */
export function byBasisText(byBasis: Record<string, Record<string, number>> | null | undefined): string {
    if (!byBasis) return '';
    const parts: string[] = [];
    for (const basis of ['recomputed', 'judged', 'indicator', 'declared']) {
        const v = byBasis[basis];
        if (!v) continue;
        const measured = v.measured ?? 0;
        if (basis === 'indicator' || basis === 'declared') {
            parts.push(`${basis}: ${measured.toLocaleString()} stated`);
        } else {
            parts.push(`${basis}: ${nOfM(v.met ?? 0, measured)} met`);
        }
    }
    return parts.join(' · ');
}

// ─── Basis ───────────────────────────────────────────────────────────────────

export const BASIS_LABEL: Record<EvidenceBasis, string> = {
    recomputed: 'RECOMPUTED',
    judged: 'JUDGED',
    indicator: 'INDICATOR',
    declared: 'DECLARED',
};

export const BASIS_TITLE: Record<EvidenceBasis, string> = {
    recomputed: 'Recomputed from sealed records — anyone holding the records can re-derive it.',
    judged: 'Judged — a governed judge graded sampled runs against a pinned rubric; human agreement is stated beside it.',
    indicator: 'Indicator — movement worth a look, never a threshold or a result.',
    declared: 'Declared — the operator stated it; the declaration is sealed, the fact is not recomputed.',
};

export function basisLabel(basis: string | null | undefined): string {
    return (BASIS_LABEL as Record<string, string>)[basis ?? ''] ?? (basis ?? '—').toUpperCase();
}

// ─── Judge–human agreement ───────────────────────────────────────────

export interface AgreementBlock {
    n: number;
    observed: number | null;
    ci: [number, number] | null;
    kappa: number | null;
    blind: boolean;
    insufficient: boolean;
    belowThreshold: boolean;
}

/** Read an agreement block (`app/services/compliance/agreement.py`). */
export function readAgreement(raw: unknown): AgreementBlock | null {
    if (!isRecord(raw)) return null;
    const ciRaw = raw.ci95;
    const ci =
        Array.isArray(ciRaw) && typeof ciRaw[0] === 'number' && typeof ciRaw[1] === 'number'
            ? ([ciRaw[0], ciRaw[1]] as [number, number])
            : null;
    return {
        n: getNumber(raw, 'n') ?? 0,
        observed: getNumber(raw, 'observed_agreement') ?? null,
        ci,
        kappa: getNumber(raw, 'kappa') ?? null,
        blind: getBoolean(raw, 'blind') ?? false,
        insufficient: getBoolean(raw, 'insufficient_sample') ?? false,
        belowThreshold: getBoolean(raw, 'below_threshold') ?? false,
    };
}

/** `human agreement 0.92 [0.83–0.97], n = 34, blind` — the CP's own
 *  rendering (`agreement.render`), reproduced so the console and the
 *  report read identically. */
export function agreementText(block: AgreementBlock | null): string {
    if (!block || !block.n || block.observed === null) return 'no human grades yet';
    let text = `human agreement ${block.observed.toFixed(2)}`;
    if (block.ci) text += ` [${block.ci[0].toFixed(2)}–${block.ci[1].toFixed(2)}]`;
    text += `, n = ${block.n}`;
    if (block.blind) text += ', blind';
    if (block.insufficient) text += ' (insufficient sample)';
    return text;
}

/** The agreement blocks of a judged statement: `facts.agreement` is
 *  `{check_id: block}` on obligation statements and a single block (or the
 *  same map) on report rows. */
export function statementAgreements(raw: unknown): Array<{ checkId: string | null; block: AgreementBlock }> {
    if (!isRecord(raw)) return [];
    if ('observed_agreement' in raw || 'n' in raw) {
        const block = readAgreement(raw);
        return block ? [{ checkId: getString(raw, 'check_id') ?? null, block }] : [];
    }
    const out: Array<{ checkId: string | null; block: AgreementBlock }> = [];
    for (const [checkId, value] of Object.entries(raw)) {
        const block = readAgreement(value);
        if (block) out.push({ checkId, block });
    }
    return out;
}

/** `JUDGED · met 57 of 60 · human agreement 0.92 [0.83–0.97], n = 34, blind`. */
export function judgedRowText(
    status: string,
    n: number | null | undefined,
    m: number | null | undefined,
    agreementRaw: unknown,
): string {
    const agreements = statementAgreements(agreementRaw);
    const agreement = agreements.length
        ? agreements.map((a) => agreementText(a.block)).join('; ')
        : 'no human grades yet';
    return `JUDGED · ${statementSummary(status, n, m).replace(' · ', ' ')} · ${agreement}`;
}

/** The agreement facts of a statement (obligation detail `facts.agreement`). */
export function agreementFacts(facts: unknown): unknown {
    return getRecord(facts, 'agreement') ?? null;
}

// ─── Deadlines ───────────────────────────────────────────────────────

export const DEADLINE_STATUS_LABEL: Record<string, string> = {
    open: 'open',
    due_soon: 'due soon',
    overdue: 'overdue',
    reported_on_time: 'reported on time',
    reported_late: 'reported late',
    closed: 'closed',
    waiting: 'waiting',
    closed_unsubmitted: 'closed, not submitted',
};

export const DEADLINE_STATUS_CHIP: Record<string, string> = {
    open: 'bg-surface-raised text-secondary',
    due_soon: 'bg-amber-100 text-amber-800',
    overdue: 'bg-red-100 text-red-800',
    reported_on_time: 'bg-green-100 text-green-800',
    reported_late: 'bg-amber-100 text-amber-800',
    closed: 'bg-surface-raised text-secondary',
    waiting: 'bg-surface-raised text-muted',
    closed_unsubmitted: 'bg-amber-100 text-amber-800',
};

/** `3 d 4 h` / `5 h` / `under 1 h` for a positive number of hours. */
export function formatHours(hours: number): string {
    const h = Math.abs(hours);
    if (h < 1) return 'under 1 h';
    const days = Math.floor(h / 24);
    const rest = Math.floor(h - days * 24);
    if (days === 0) return `${rest} h`;
    return rest ? `${days} d ${rest} h` : `${days} d`;
}

/** One deadline as text: `due soon · 5 h left` / `overdue by 2 d` /
 *  `reported on time`. */
export function deadlineText(status: string, hoursRemaining: number | null | undefined): string {
    const label = DEADLINE_STATUS_LABEL[status] ?? status.replace(/_/g, ' ');
    if (hoursRemaining === null || hoursRemaining === undefined) return label;
    if (status === 'overdue') return `${label} by ${formatHours(hoursRemaining)}`;
    if (status === 'open' || status === 'due_soon') return `${label} · ${formatHours(hoursRemaining)} left`;
    return label;
}

/** A framework's deadline rules as text. The registry stores
 *  `{name: {standard_days: 15, death_days: 10}}`; the catalog summary
 *  stores `{rule: {hours, days}}`. Both read as `standard 15 d · death 10 d`. */
/**
 * A short delay, in the unit that shows it: "8 ms", "3.3 s", "2 min".
 * One decimal of seconds turned a real 8 ms median into "0.0 s", which read
 * as "not measured".
 */
export function formatDelay(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    if (seconds < 0.0005) return '0 ms';
    if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
    if (seconds < 120) return `${seconds.toFixed(1)} s`;
    return `${Math.round(seconds / 60)} min`;
}

export function deadlineRulesText(rules: unknown): string {
    if (!isRecord(rules)) return '';
    const parts: string[] = [];
    for (const [name, value] of Object.entries(rules)) {
        if (!isRecord(value)) continue;
        const hours = getNumber(value, 'hours');
        if (hours !== undefined) {
            parts.push(`${name.replace(/_/g, ' ')} ${hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} d`}`);
            continue;
        }
        for (const [k, v] of Object.entries(value)) {
            if (typeof v !== 'number') continue;
            const m = /^(.*)_(days|hours)$/.exec(k);
            if (!m) continue;
            parts.push(`${m[1].replace(/_/g, ' ')} ${v} ${m[2] === 'days' ? 'd' : 'h'}`);
        }
    }
    return parts.join(' · ');
}

// ─── Windows ─────────────────────────────────────────────────────────────────

export const WINDOW_PRESETS = [
    { id: '7d', label: 'Last 7 days', days: 7 },
    { id: '30d', label: 'Last 30 days', days: 30 },
    { id: '90d', label: 'Last 90 days', days: 90 },
] as const;
export type WindowPreset = (typeof WINDOW_PRESETS)[number]['id'];

/** `{from, to}` ISO instants for a preset, ending now (minute precision so a
 *  re-render does not change the cache key). */
export function windowFor(preset: WindowPreset, now: Date = new Date()): { from: string; to: string } {
    const days = WINDOW_PRESETS.find((p) => p.id === preset)?.days ?? 30;
    const end = new Date(now);
    end.setSeconds(0, 0);
    const start = new Date(end.getTime() - days * 86_400_000);
    return { from: start.toISOString(), to: end.toISOString() };
}

export function formatWhen(value: string | null | undefined): string {
    if (!value) return '—';
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export function formatDay(value: string | null | undefined): string {
    if (!value) return '—';
    return value.slice(0, 10);
}

/** Download a JSON value as a file (bundle, report bundle). */
export function downloadJson(value: unknown, filename: string): void {
    const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The docs-site path of the offline verifier (the VERIFY package's page). */
export const OFFLINE_VERIFIER_PATH = '/tools/verify/';
export const OFFLINE_VERIFIER_URL = `https://docs.brutor.ai${OFFLINE_VERIFIER_PATH}`;

/** Every provision string is a pointer, not advice. */
export const NOT_LEGAL_ADVICE = 'Not legal advice — a pointer into the instrument, not an interpretation of it.';

// ─── Shared control classes (tokens only — both themes) ──────────────────────

export const INPUT_CLASS = 'rounded-md border border-default bg-surface px-2 py-1.5 text-xs text-default';
export const BUTTON_PRIMARY =
    'inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50';
export const BUTTON_SECONDARY =
    'inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-strong text-default hover:bg-surface-hover disabled:opacity-50';
export const LINK_BUTTON = 'text-xs text-primary-600 hover:underline';

// ─── Cross-framework references ──────────────────────────────────────────────

export interface OtherFrameworkRef {
    frameworkId: string;
    frameworkTitle: string;
    obligationKey: string;
    title: string;
    reference: string;
}

export function readOtherFrameworks(raw: unknown): OtherFrameworkRef[] {
    const out: OtherFrameworkRef[] = [];
    for (const item of Array.isArray(raw) ? (raw as unknown[]) : []) {
        const frameworkId = getString(item, 'framework_id');
        const obligationKey = getString(item, 'obligation_key');
        if (!frameworkId || !obligationKey) continue;
        out.push({
            frameworkId,
            frameworkTitle: getString(item, 'framework_title') ?? frameworkId,
            obligationKey,
            title: getString(item, 'title') ?? obligationKey,
            reference: getString(item, 'reference') ?? '',
        });
    }
    return out;
}

// ─── Matrix cells (Obligations) ───────────────────────────────────────────

/** What a matrix cell says under its status chip: the CP's own summary
 *  (`3 of 4 recomputed met · 1 judged`) or, failing that, the per-basis
 *  counts. Never a percentage, never a merged total. */
export function cellSummary(cell: {
    summary?: string | null;
    by_basis?: Record<string, Record<string, number>> | null;
    status_counts?: Record<string, number> | null;
    applicability?: string;
    reason?: string | null;
}): string {
    if (cell.applicability === 'not_applicable') return 'not applicable';
    if (cell.applicability === 'undetermined') return cell.reason ? `undetermined — ${cell.reason}` : 'undetermined';
    // Declared and not-evaluable statements carry no `n of m`, so neither the
    // summary nor the per-basis counts mention them; the status counts do.
    const counted = Object.entries(cell.status_counts ?? {})
        .filter(([, n]) => n > 0)
        .map(([status, n]) => `${n} ${statusLabel(status)}`)
        .join(' · ');
    return cell.summary?.trim() || byBasisText(cell.by_basis) || counted || 'no statement';
}

// ─── Incidents ───────────────────────────────────────────────────────

export const INCIDENT_STATUS_CHIP: Record<string, string> = {
    open: 'bg-amber-100 text-amber-800',
    reported: 'bg-blue-100 text-blue-800',
    closed: 'bg-surface-raised text-secondary',
};

export const INCIDENT_SEVERITY_CHIP: Record<string, string> = {
    low: 'bg-surface-raised text-secondary',
    medium: 'bg-amber-100 text-amber-800',
    high: 'bg-red-100 text-red-800',
    critical: 'bg-red-100 text-red-800',
};

export function deadlineStatusLabel(status: string | null | undefined): string {
    if (!status) return '—';
    return DEADLINE_STATUS_LABEL[status] ?? status.replace(/_/g, ' ');
}

/** Urgency order for choosing the deadline a register row leads with. */
const DEADLINE_URGENCY = ['overdue', 'due_soon', 'open', 'closed_unsubmitted', 'reported_late', 'reported_on_time', 'waiting', 'closed'];

/** The most pressing deadline of an incident — the one the register shows
 *  first. `null` when there is none (no framework in scope has a rule). */
export function leadDeadline<T extends { status: string; due_at?: string | null }>(deadlines: T[] | null | undefined): T | null {
    if (!deadlines || deadlines.length === 0) return null;
    return [...deadlines].sort((a, b) => {
        const ua = DEADLINE_URGENCY.indexOf(a.status);
        const ub = DEADLINE_URGENCY.indexOf(b.status);
        if (ua !== ub) return (ua === -1 ? 99 : ua) - (ub === -1 ? 99 : ub);
        // A stage still waiting for its anchor has no due time: last.
        if (!a.due_at || !b.due_at) return (a.due_at ? 0 : 1) - (b.due_at ? 0 : 1);
        return a.due_at.localeCompare(b.due_at);
    })[0];
}

/** What a deadline's clock runs from, in words: `from awareness`,
 *  `from classification (<date and time>)`, `waiting for the initial
 *  submission`, `24 h bound until classified`. */
export function deadlineAnchorText(d: {
    anchor?: string | null;
    anchor_at?: string | null;
    waiting_for?: string | null;
    due_at?: string | null;
}): string {
    const anchor = d.anchor ?? 'aware';
    if (d.waiting_for === 'classification') return 'classification not recorded — bounded at 24 h from awareness';
    if (!d.due_at && d.waiting_for) return `waiting for the ${d.waiting_for.replace(/_/g, ' ')} submission`;
    const when = d.anchor_at ? ` (${formatWhen(d.anchor_at)})` : '';
    if (anchor === 'aware') return `from awareness${when}`;
    if (anchor === 'classified') return `from classification${when}, at most 24 h from awareness`;
    if (anchor.startsWith('stage:')) return `from the ${anchor.slice(6).replace(/_/g, ' ')} submission${when}`;
    return anchor;
}

/** The phrase every incident surface carries when `deadlines_enforced` is
 *  false (always, today): the platform computes and shows deadlines, and
 *  never acts on them. */
export const DEADLINES_NOT_ENFORCED = 'Deadlines shown, not enforced.';

/** `edited · title, severity` from an event's `detail` block. */
export function incidentEventText(event: string, detail: unknown): string {
    const label = event.replace(/_/g, ' ');
    if (!isRecord(detail)) return label;
    const parts: string[] = [];
    const fields = getArray(detail, 'fields');
    if (fields && fields.length) parts.push(fields.filter((f): f is string => typeof f === 'string').join(', '));
    const authority = getString(detail, 'authority');
    if (authority) parts.push(`to ${authority}`);
    const reference = getString(detail, 'external_reference');
    if (reference) parts.push(`ref ${reference}`);
    const note = getString(detail, 'note');
    if (note) parts.push(note);
    const framework = getString(detail, 'framework_id');
    const stage = getString(detail, 'stage');
    if (framework && stage) parts.push(`${framework} ${stage.replace(/_/g, ' ')}`);
    const stageStatus = event === 'stage_submitted' ? getString(detail, 'status') : undefined;
    if (stageStatus) parts.push(deadlineStatusLabel(stageStatus));
    return parts.length ? `${label} · ${parts.join(' · ')}` : label;
}

// ─── Reports ─────────────────────────────────────────────────────────

export const COUNTERSIGNATURE_LABEL: Record<string, string> = {
    countersigned: 'countersigned',
    awaiting_countersignature: 'awaiting countersignature',
    none: 'no bundle',
};

export function countersignatureLabel(status: string | null | undefined): string {
    return COUNTERSIGNATURE_LABEL[status ?? ''] ?? (status || '—');
}

export const REPORT_STATUS_CHIP: Record<string, string> = {
    ready: 'bg-green-100 text-green-800',
    failed: 'bg-red-100 text-red-800',
    pending: 'bg-amber-100 text-amber-800',
    generating: 'bg-amber-100 text-amber-800',
};

/** `2026-09-01 – 2026-09-30` / `2026-09-19` for a report period. */
export function periodText(start: string | null | undefined, end: string | null | undefined): string {
    if (!start) return '—';
    if (!end || end === start) return start;
    return `${start} – ${end}`;
}

// ─── Oversight ──────────────────────────────────────────────────────────

/** `3 pending · 12 graded · 1 skipped` from a check's review queue. */
export function reviewQueueText(queue: unknown): string {
    const pending = getNumber(queue, 'pending') ?? 0;
    const graded = getNumber(queue, 'graded') ?? 0;
    const skipped = getNumber(queue, 'skipped') ?? 0;
    const parts = [`${pending} pending`, `${graded} graded`];
    if (skipped) parts.push(`${skipped} skipped`);
    return parts.join(' · ');
}

// ─── Inputs ──────────────────────────────────────────────────────────────────

/** Record ids or run ids pasted one per line (or comma / space separated),
 *  de-duplicated in order. */
export function parseIdList(text: string): string[] {
    const seen = new Set<string>();
    for (const raw of text.split(/[\s,;]+/)) {
        const v = raw.trim();
        if (v) seen.add(v);
    }
    return [...seen];
}

/** An `<input type="date">` value as the ISO start / end of that day (UTC). */
export function dayStart(day: string): string {
    return `${day}T00:00:00Z`;
}
export function dayEnd(day: string): string {
    return `${day}T23:59:59Z`;
}

// ─── Verifier outcomes ────────────────────────────────────────────────

/** The three honest outcomes of a verification: the verifier could not
 *  run (a state of the tool, shown apart from any result), recomputed and
 *  consistent, or recomputed and not consistent. */
export type VerifyOutcome = 'unavailable' | 'consistent' | 'inconsistent';

export function verifyOutcome(result: { ok: boolean; findings: EvidenceVerifyFinding[] }): VerifyOutcome {
    if (isVerifierUnavailable(result)) return 'unavailable';
    return result.ok && !result.findings.some((f) => f.severity === 'error') ? 'consistent' : 'inconsistent';
}

export const VERIFY_OUTCOME_TITLE: Record<VerifyOutcome, string> = {
    unavailable: 'Verifier unavailable — nothing was checked',
    consistent: 'Recomputed: consistent',
    inconsistent: 'Recomputed: not consistent',
};

export const VERIFY_OUTCOME_CLASS: Record<VerifyOutcome, string> = {
    unavailable: 'border-amber-300 bg-amber-50',
    consistent: 'border-green-300 bg-green-50',
    inconsistent: 'border-red-300 bg-red-50',
};

// ─── Matrix columns ──────────────────────────────────────────────────────────

/** Column order: in force first (by date), then upcoming. */
export function matrixColumns(obligations: ComplianceBoardObligation[]): ComplianceBoardObligation[] {
    return [...obligations].sort((a, b) => {
        if (a.in_force !== b.in_force) return a.in_force ? -1 : 1;
        return a.applies_from.localeCompare(b.applies_from) || a.key.localeCompare(b.key);
    });
}

// ─── Obligation groups (Obligations) ──────────────────────────────────────

export interface ObligationGroups<T> {
    inForce: T[];
    upcoming: T[];
    notApplicable: T[];
}

/** In force / upcoming / not applicable. An obligation nothing is in scope
 *  for is not applicable whatever its date; `undetermined` IS in scope — it
 *  is a to-do, so it stays on the dated side where it will be seen. */
export function groupObligations<T extends { in_force: boolean; applies_from: string }>(
    items: T[],
    inScope: (item: T) => boolean,
): ObligationGroups<T> {
    const { inForce, upcoming } = splitObligations(items.filter(inScope));
    return { inForce, upcoming, notApplicable: items.filter((i) => !inScope(i)) };
}

/** One obligation as one system sees it: the board's dated obligation and
 *  the matrix cell evaluated for that system (absent when the system had no
 *  cell in the window). */
export interface SystemObligationRow {
    key: string;
    in_force: boolean;
    applies_from: string;
    obligation: ComplianceBoardObligation;
    cell: ComplianceMatrixCell | undefined;
}

export function systemObligationGroups(
    obligations: ComplianceBoardObligation[],
    cells: ComplianceMatrixCell[],
): ObligationGroups<SystemObligationRow> {
    const byKey = new Map(cells.map((c) => [c.obligation_key, c]));
    const rows = matrixColumns(obligations).map((o) => ({
        key: o.key,
        in_force: o.in_force,
        applies_from: o.applies_from,
        obligation: o,
        cell: byKey.get(o.key),
    }));
    return groupObligations(rows, (r) => !!r.cell && r.cell.applicability !== 'not_applicable');
}

// ─── Oversight statements ────────────────────────────────────────────────────

/** One resolver statement as the oversight summary carries it. */
export interface OversightStatement {
    basis: string | null;
    status: string;
    n: number | null;
    m: number | null;
    detail: string;
    limit: string | null;
    citations: string[];
    facts: Record<string, unknown> | null;
}

export function readOversightStatement(raw: unknown): OversightStatement | null {
    const status = getString(raw, 'status');
    if (!status) return null;
    return {
        basis: getString(raw, 'basis') ?? null,
        status,
        n: getNumber(raw, 'n') ?? null,
        m: getNumber(raw, 'm') ?? null,
        detail: getString(raw, 'detail') ?? '',
        limit: getString(raw, 'limit') ?? null,
        citations: (getArray(raw, 'citations') ?? []).filter((c): c is string => typeof c === 'string'),
        facts: getRecord(raw, 'facts') ?? null,
    };
}

/** Movement facts as neutral text: `approval latency +12 % vs baseline ·
 *  denial rate −3 % vs baseline`. Signs and magnitudes only — no colour,
 *  no threshold. */
export function movementText(facts: Record<string, unknown> | null): string {
    if (!facts) return '';
    const parts: string[] = [];
    for (const [key, label] of [
        ['latency_change', 'approval latency'],
        ['denial_rate_change', 'denial rate'],
    ] as const) {
        const v = getNumber(facts, key);
        if (v === undefined) continue;
        const pct = Math.round(v * 100);
        parts.push(`${label} ${pct >= 0 ? '+' : '−'}${Math.abs(pct)} % vs baseline`);
    }
    const baseline = getNumber(facts, 'baseline_n');
    if (baseline !== undefined) parts.push(`baseline n = ${baseline}`);
    return parts.join(' · ');
}
