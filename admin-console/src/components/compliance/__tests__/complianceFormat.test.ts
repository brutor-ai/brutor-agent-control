/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Display rules: n of m and never a
 * percentage; the judged-row sentence exactly as the CP renders it;
 * deadlines as labels, never enforced; matrix cells that never read
 * `undetermined` as met; the verifier's three outcomes; movement as signed
 * indicators.
 */

import { describe, expect, it } from 'vitest';

import {
    agreementText,
    byBasisText,
    cellSummary,
    countersignatureLabel,
    deadlineStatusLabel,
    deadlineText,
    formatHours,
    incidentEventText,
    deadlineAnchorText,
    judgedRowText,
    leadDeadline,
    matrixColumns,
    movementText,
    nOfM,
    parseIdList,
    periodText,
    readAgreement,
    readOversightStatement,
    reviewQueueText,
    statementSummary,
    STATEMENT_STATUS_CHIP,
    statusChip,
    verifyOutcome,
    VERIFY_OUTCOME_TITLE,
    windowFor,
} from '../complianceFormat';
import type { ComplianceBoardObligation } from '../../../lib/complianceEvidenceTypes';

describe('n of m', () => {
    it('never renders a percentage', () => {
        expect(nOfM(57, 60)).toBe('57 of 60');
        expect(nOfM(0, 0)).toBe('0 of 0');
        expect(nOfM(null, null)).toBe('—');
        expect(nOfM(1234, 5678)).toBe('1,234 of 5,678');
        expect(statementSummary('met', 3, 4)).toBe('met · 3 of 4');
        expect(statementSummary('not_evaluable', 0, 4)).toBe('not evaluable');
        expect(statementSummary('declared')).toBe('declared');
        expect(nOfM(57, 60)).not.toMatch(/%/);
    });

    it('keeps bases apart', () => {
        expect(byBasisText({ recomputed: { met: 3, measured: 4 }, judged: { met: 1, measured: 1 }, declared: { measured: 2 } })).toBe(
            'recomputed: 3 of 4 met · judged: 1 of 1 met · declared: 2 stated',
        );
        expect(byBasisText(null)).toBe('');
    });
});

describe('status colours', () => {
    it('only met is green; unknown states are never green', () => {
        expect(statusChip('met')).toContain('green');
        for (const s of ['not_evaluable', 'undetermined', 'not_covered', 'indicator', 'declared', 'whatever']) {
            expect(statusChip(s)).not.toContain('green');
        }
        expect(Object.entries(STATEMENT_STATUS_CHIP).filter(([, v]) => v.includes('green')).map(([k]) => k)).toEqual(['met']);
    });
});

describe('judged rows', () => {
    const block = { n: 34, observed_agreement: 0.92, ci95: [0.83, 0.97], kappa: 0.8, blind: true, insufficient_sample: false, below_threshold: false };

    it('renders the CP sentence verbatim', () => {
        expect(agreementText(readAgreement(block))).toBe('human agreement 0.92 [0.83–0.97], n = 34, blind');
        expect(judgedRowText('met', 57, 60, block)).toBe('JUDGED · met 57 of 60 · human agreement 0.92 [0.83–0.97], n = 34, blind');
    });

    it('states an insufficient sample and the absence of grades', () => {
        expect(agreementText(readAgreement({ ...block, n: 4, insufficient_sample: true }))).toBe('human agreement 0.92 [0.83–0.97], n = 4, blind (insufficient sample)');
        expect(agreementText(null)).toBe('no human grades yet');
        expect(judgedRowText('not_met', 1, 3, null)).toBe('JUDGED · not met 1 of 3 · no human grades yet');
        expect(judgedRowText('met', 2, 2, { 'chk-1': block, 'chk-2': { ...block, n: 12, observed_agreement: 0.5 } })).toContain('; human agreement 0.50');
    });
});

describe('deadlines', () => {
    it('labels every status and never enforces', () => {
        expect(deadlineStatusLabel('due_soon')).toBe('due soon');
        expect(deadlineStatusLabel('reported_on_time')).toBe('reported on time');
        expect(deadlineStatusLabel('reported_late')).toBe('reported late');
        expect(deadlineStatusLabel('overdue')).toBe('overdue');
        expect(deadlineStatusLabel('open')).toBe('open');
        expect(deadlineStatusLabel('closed')).toBe('closed');
        expect(deadlineStatusLabel(undefined)).toBe('—');
        expect(deadlineStatusLabel('something_new')).toBe('something new');
    });

    it('phrases remaining time', () => {
        expect(formatHours(0.5)).toBe('under 1 h');
        expect(formatHours(5)).toBe('5 h');
        expect(formatHours(76)).toBe('3 d 4 h');
        expect(formatHours(48)).toBe('2 d');
        expect(deadlineText('due_soon', 5)).toBe('due soon · 5 h left');
        expect(deadlineText('overdue', -30)).toBe('overdue by 1 d 6 h');
        expect(deadlineText('reported_on_time', 100)).toBe('reported on time');
        expect(deadlineText('open', null)).toBe('open');
    });

    it('leads with the most pressing deadline', () => {
        const d = (status: string, due_at: string) => ({ status, due_at });
        expect(leadDeadline([d('open', '2026-10-01'), d('overdue', '2026-09-01'), d('due_soon', '2026-09-20')])?.status).toBe('overdue');
        expect(leadDeadline([d('open', '2026-10-05'), d('open', '2026-10-01')])?.due_at).toBe('2026-10-01');
        expect(leadDeadline([d('closed', '2026-10-05'), d('reported_late', '2026-10-01')])?.status).toBe('reported_late');
        expect(leadDeadline([])).toBeNull();
        expect(leadDeadline(undefined)).toBeNull();
    });

    it('staged regimes: waiting and closed-unsubmitted stages, and what each clock runs from', () => {
        expect(deadlineStatusLabel('waiting')).toBe('waiting');
        expect(deadlineStatusLabel('closed_unsubmitted')).toBe('closed, not submitted');
        // A waiting stage has no due time; it never leads over a running clock.
        expect(leadDeadline([{ status: 'waiting', due_at: null }, { status: 'reported_on_time', due_at: '2026-09-01' }])?.status).toBe('reported_on_time');
        expect(leadDeadline([{ status: 'closed_unsubmitted', due_at: null }, { status: 'reported_late', due_at: '2026-09-01' }])?.status).toBe('closed_unsubmitted');
        expect(deadlineAnchorText({ anchor: 'aware', anchor_at: null, due_at: '2026-09-02' })).toBe('from awareness');
        expect(deadlineAnchorText({ anchor: 'classified', waiting_for: 'classification', due_at: '2026-09-02' })).toMatch(/classification not recorded — bounded at 24 h/);
        expect(deadlineAnchorText({ anchor: 'classified', anchor_at: '2026-09-01T18:00:00Z', due_at: '2026-09-01T22:00:00Z' })).toMatch(/^from classification \(.+\), at most 24 h from awareness$/);
        expect(deadlineAnchorText({ anchor: 'stage:initial', waiting_for: 'initial', due_at: null })).toBe('waiting for the initial submission');
        expect(deadlineAnchorText({ anchor: 'stage:notification', anchor_at: '2026-09-03T10:00:00Z', due_at: '2026-10-03T10:00:00Z' })).toMatch(/^from the notification submission/);
        expect(incidentEventText('stage_submitted', { framework_id: 'dora', stage: 'initial', status: 'reported_on_time' })).toBe('stage submitted · dora initial · reported on time');
    });

    it('describes incident events from their detail block', () => {
        expect(incidentEventText('opened', {})).toBe('opened');
        expect(incidentEventText('updated', { fields: ['title', 'severity'] })).toBe('updated · title, severity');
        expect(incidentEventText('reported', { authority: 'AI Office', external_reference: 'REF-1' })).toBe('reported · to AI Office · ref REF-1');
        expect(incidentEventText('note_added', { note: 'called the DPO' })).toBe('note added · called the DPO');
        expect(incidentEventText('closed', null)).toBe('closed');
    });
});

describe('matrix cells', () => {
    it('summarises by applicability first, then by the CP text, then by basis', () => {
        expect(cellSummary({ applicability: 'not_applicable', summary: 'ignored' })).toBe('not applicable');
        expect(cellSummary({ applicability: 'undetermined', reason: 'role not declared' })).toBe('undetermined — role not declared');
        expect(cellSummary({ applicability: 'applies', summary: '3 of 4 recomputed met' })).toBe('3 of 4 recomputed met');
        expect(cellSummary({ applicability: 'applies', summary: '', by_basis: { judged: { met: 1, measured: 2 } } })).toBe('judged: 1 of 2 met');
        expect(cellSummary({ applicability: 'applies', summary: '' })).toBe('no statement');
        // A cell whose statements are all declared / not evaluable has no
        // n of m; it must not read "no statement" next to a status chip.
        expect(cellSummary({ applicability: 'applies', summary: '', by_basis: {}, status_counts: { declared: 1, met: 0 } })).toBe('1 declared');
        expect(cellSummary({ applicability: 'applies', summary: '', status_counts: { declared: 2, not_evaluable: 1 } })).toBe('2 declared · 1 not evaluable');
    });

    it('orders columns in force first, then by date', () => {
        const o = (key: string, in_force: boolean, applies_from: string) => ({ key, in_force, applies_from }) as ComplianceBoardObligation;
        expect(matrixColumns([o('c', false, '2027-08-02'), o('b', true, '2026-08-02'), o('a', true, '2025-02-02')]).map((x) => x.key)).toEqual(['a', 'b', 'c']);
    });
});

describe('verifier outcomes', () => {
    it('separates "could not run" from the two results', () => {
        expect(verifyOutcome({ ok: false, findings: [{ check: 'verifier', code: 'verifier_unavailable', severity: 'error', detail: 'core down' }] })).toBe('unavailable');
        expect(verifyOutcome({ ok: true, findings: [{ check: 'semantic', code: 'x', severity: 'info', detail: '' }] })).toBe('consistent');
        expect(verifyOutcome({ ok: true, findings: [{ check: 'sig', code: 'bad', severity: 'error', detail: '' }] })).toBe('inconsistent');
        expect(verifyOutcome({ ok: false, findings: [] })).toBe('inconsistent');
        expect(VERIFY_OUTCOME_TITLE.unavailable).toMatch(/nothing was checked/);
    });
});

describe('oversight statements', () => {
    it('reads a resolver statement and phrases movement as signed indicators', () => {
        const s = readOversightStatement({ basis: 'recomputed', status: 'met', n: 8, m: 10, detail: 'd', limit: null, citations: ['r1', 2], facts: { a: 1 } });
        expect(s).toEqual({ basis: 'recomputed', status: 'met', n: 8, m: 10, detail: 'd', limit: null, citations: ['r1'], facts: { a: 1 } });
        expect(readOversightStatement({})).toBeNull();
        expect(movementText({ latency_change: 0.123, denial_rate_change: -0.03, baseline_n: 40 })).toBe(
            'approval latency +12 % vs baseline · denial rate −3 % vs baseline · baseline n = 40',
        );
        expect(movementText(null)).toBe('');
        expect(reviewQueueText({ pending: 3, graded: 12, skipped: 0 })).toBe('3 pending · 12 graded');
        expect(reviewQueueText({ pending: 0, graded: 1, skipped: 2 })).toBe('0 pending · 1 graded · 2 skipped');
    });
});

describe('reports', () => {
    it('labels countersignature status and periods', () => {
        expect(countersignatureLabel('countersigned')).toBe('countersigned');
        expect(countersignatureLabel('awaiting_countersignature')).toBe('awaiting countersignature');
        expect(countersignatureLabel('none')).toBe('no bundle');
        expect(periodText('2026-09-01', '2026-09-30')).toBe('2026-09-01 – 2026-09-30');
        expect(periodText('2026-09-19', '2026-09-19')).toBe('2026-09-19');
        expect(periodText(null, null)).toBe('—');
    });
});

describe('inputs', () => {
    it('parses id lists and windows', () => {
        expect(parseIdList('a\nb, c;a  d')).toEqual(['a', 'b', 'c', 'd']);
        expect(parseIdList('')).toEqual([]);
        const w = windowFor('7d', new Date('2026-09-22T10:30:45Z'));
        expect(w).toEqual({ from: '2026-09-15T10:30:00.000Z', to: '2026-09-22T10:30:00.000Z' });
    });
});
