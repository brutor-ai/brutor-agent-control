/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The pure rules behind the dialog, the Composition tab,
 * the evidence panel and the report section:
 *
 *   * the assumptions form mirrors the server's strict schema (ranges,
 *     integer latency, bounds, only known keys, canonical order);
 *   * supplier information is required exactly where the control plane
 *     requires it (external, or a shared service with a supplier entity);
 *   * the task-state mix and the separability claim read the same on every
 *     surface; an external dependency never reads as tested.
 */

import { describe, it, expect } from 'vitest';

import {
    ASSUMPTION_LIMITS,
    assumptionFacts,
    assumptionsForm,
    acceptedCallersText,
    dependencyEdgeLabel,
    needsSupplierInformation,
    parseAssumptionsForm,
    readAssumptions,
    separabilityText,
    supplierEdgesFirst,
    taskStateMix,
} from '../composition';
import { boundaryText } from '../dependencyHealth';
import {
    readCompositionSection,
    reportCallerText,
    reportEdgeStatus,
    supplierInformationOk,
    supplierInformationText,
} from '../compositionReport';
import { DEPENDENCY_EVIDENCE_PROVIDED } from '../aiSystemTypes';
import { ADDRESS_EDGE, BUREAU_EDGE, FRAUD_EDGE } from '../../components/ai-systems/__tests__/fixtures/compositionPayload';
import { COMPOSITION_SECTION } from '../../components/ai-systems/__tests__/fixtures/compositionReportSection';

const blankForm = () => assumptionsForm({});

describe('parseAssumptionsForm — the strict schema, mirrored', () => {
    it('an empty form states nothing', () => {
        expect(parseAssumptionsForm(blankForm())).toEqual({ value: {}, errors: {} });
    });

    it.each([
        ['0', 0],
        ['100', 100],
        ['99.95', 99.95],
    ])('availability %s is accepted', (raw, n) => {
        expect(parseAssumptionsForm({ ...blankForm(), expected_availability_pct: raw }).value.expected_availability_pct).toBe(n);
    });

    it.each(['-0.1', '100.01', 'abc', 'Infinity'])('availability %s is refused', (raw) => {
        const r = parseAssumptionsForm({ ...blankForm(), expected_availability_pct: raw });
        expect(r.errors.expected_availability_pct).toBeTruthy();
        expect(r.value).not.toHaveProperty('expected_availability_pct');
    });

    it.each(['1.5', '-1', 'fast'])('latency %s is refused (integer >= 0)', (raw) => {
        expect(parseAssumptionsForm({ ...blankForm(), expected_p95_latency_ms: raw }).errors.expected_p95_latency_ms).toBeTruthy();
    });

    it('latency 0 is a statement, not an empty field', () => {
        expect(parseAssumptionsForm({ ...blankForm(), expected_p95_latency_ms: '0' }).value).toEqual({ expected_p95_latency_ms: 0 });
    });

    it('data classes are trimmed, deduped and bounded', () => {
        expect(parseAssumptionsForm({ ...blankForm(), data_classes_shared: ' a, b ,a,, ' }).value.data_classes_shared).toEqual(['a', 'b']);
        const many = Array.from({ length: ASSUMPTION_LIMITS.data_classes_shared + 1 }, (_, i) => `c${i}`).join(',');
        expect(parseAssumptionsForm({ ...blankForm(), data_classes_shared: many }).errors.data_classes_shared).toBeTruthy();
    });

    it('retention is bounded at 500 characters', () => {
        const long = 'x'.repeat(ASSUMPTION_LIMITS.retention_at_delegate + 1);
        expect(parseAssumptionsForm({ ...blankForm(), retention_at_delegate: long }).errors.retention_at_delegate).toBeTruthy();
    });

    it('oversight: unstated is omitted; yes/no are booleans', () => {
        expect(parseAssumptionsForm({ ...blankForm(), human_oversight_at_delegate: 'no' }).value).toEqual({ human_oversight_at_delegate: false });
        expect(parseAssumptionsForm(blankForm()).value).not.toHaveProperty('human_oversight_at_delegate');
    });

    it('evidence is sent in the canonical enum order', () => {
        const r = parseAssumptionsForm({ ...blankForm(), evidence_provided: ['other', 'contract', 'signed_agent_card'] });
        expect(r.value.evidence_provided).toEqual(['signed_agent_card', 'contract', 'other']);
    });

    it('round-trips through the form unchanged', () => {
        const stored = {
            data_classes_shared: ['personal_data'],
            retention_at_delegate: '30 days',
            human_oversight_at_delegate: true,
            expected_availability_pct: 99.5,
            expected_p95_latency_ms: 800,
            evidence_provided: [...DEPENDENCY_EVIDENCE_PROVIDED],
        };
        expect(parseAssumptionsForm(assumptionsForm(stored)).value).toEqual(stored);
    });
});

describe('readAssumptions — the keys of the schema, each by its type', () => {
    it('reads the schema keys and leaves out a wrongly typed value', () => {
        expect(
            readAssumptions({
                expected_availability_pct: 99,
                expected_p95_latency_ms: 'slow',
                evidence_provided: ['contract', 'handshake'],
            }),
        ).toEqual({ expected_availability_pct: 99, evidence_provided: ['contract'] });
        expect(readAssumptions(null)).toEqual({});
    });
});

describe('assumptionFacts — typed wording', () => {
    it('reads the schema in order with units', () => {
        expect(
            assumptionFacts({
                evidence_provided: ['signed_agent_card'],
                expected_p95_latency_ms: 800,
                expected_availability_pct: 99.5,
                human_oversight_at_delegate: false,
            }),
        ).toEqual([
            ['Human oversight at the delegate', 'no'],
            ['Expected availability', '99.5%'],
            ['Expected p95 latency', '800 ms'],
            ['Evidence it provides', 'Signed agent card'],
        ]);
    });
});

describe('task states and separability wording', () => {
    it('task-state mix: most frequent first, zero counts dropped, underscores spaced', () => {
        expect(taskStateMix({ failed: 2, completed: 12, input_required: 1, canceled: 0 })).toBe(
            '12 completed · 2 failed · 1 input required',
        );
        expect(taskStateMix({})).toBeNull();
        expect(taskStateMix(undefined)).toBeNull();
    });

    it('the boundary line carries the task states when present, and not otherwise', () => {
        const b = { calls: 3, errors: 0, error_rate: 0, p95_latency_ms: null, last_seen_at: null };
        expect(boundaryText({ ...b, task_states: { completed: 3 } })).toContain('task states: 3 completed');
        expect(boundaryText(b)).not.toContain('task states');
    });

    it('a separability claim is sealed with its record, or says it is not', () => {
        const sealedOn = '2026-09-21';
        expect(separabilityText('rec-sep-address-0001', `${sealedOn}T08:30:00Z`)).toBe(`separable · sealed ${sealedOn} as rec-sep-address-`);
        expect(separabilityText(null, null)).toContain('not sealed yet');
    });

    it('accepted callers: enforced vs advisory', () => {
        expect(acceptedCallersText(true)).toContain('enforced');
        expect(acceptedCallersText(false)).toContain('advisory');
    });
});

describe('supplier information (Art 25(4))', () => {
    it('is needed for external edges and for a shared service with a supplier entity', () => {
        expect(needsSupplierInformation(BUREAU_EDGE)).toBe(true);
        expect(needsSupplierInformation(FRAUD_EDGE)).toBe(false);
        expect(needsSupplierInformation({ ...FRAUD_EDGE, supplier_legal_entity: 'Group Ltd' })).toBe(true);
        expect(needsSupplierInformation({ ...FRAUD_EDGE, supplier_legal_entity: '   ' })).toBe(false);
    });

    it('the picker orders edges that need it first', () => {
        const order = supplierEdgesFirst([FRAUD_EDGE, ADDRESS_EDGE, BUREAU_EDGE]).map((e) => e.id);
        expect(order[0]).toBe('dep-bureau');
        expect(order.slice(1).sort()).toEqual(['dep-address', 'dep-fraud']);
        expect(dependencyEdgeLabel(BUREAU_EDGE)).toBe('Credit Bureau Agent · External · Nordic Credit Bureau AB');
    });

    it('report wording: on file / expired / missing / not required', () => {
        const block = { required: true, on_file: 1, unexpired: 1, evidence: [] };
        expect(supplierInformationText('Acme', block)).toBe('Acme · supplier information on file (1)');
        expect(supplierInformationText('Acme', { ...block, unexpired: 0 })).toBe('Acme · supplier information EXPIRED — renew it');
        expect(supplierInformationText('Acme', { ...block, on_file: 0, unexpired: 0 })).toBe('Acme · supplier information MISSING');
        expect(supplierInformationText(null, { ...block, required: false })).toBe('deployer (no supplier)');
        expect(supplierInformationOk({ ...block, on_file: 0, unexpired: 0 })).toBe(false);
        expect(supplierInformationOk({ ...block, required: false, unexpired: 0 })).toBe(true);
    });
});

describe('the report section', () => {
    it('an external edge is never given a tested status', () => {
        const external = COMPOSITION_SECTION.edges.filter((e) => e.role === 'external');
        expect(external.map(reportEdgeStatus)).toEqual(external.map(() => 'assumed, not tested'));
        // Even if a status slipped into the payload.
        expect(external.map((e) => reportEdgeStatus({ ...e, status: 'healthy' }))).toEqual(['assumed, not tested']);
    });

    it('reads the section only when the report carries it (spec 1.6)', () => {
        expect(readCompositionSection({ composition: COMPOSITION_SECTION })).toBe(COMPOSITION_SECTION);
        expect(readCompositionSection({})).toBeNull();
        expect(readCompositionSection({ composition: { title: 'x' } })).toBeNull();
        expect(readCompositionSection(null)).toBeNull();
    });

    it('a caller not accepted reads refused when enforced, advisory otherwise', () => {
        const c = { system_id: 's', system_name: 'Shadow', accepted: false, observed_calls: 1 };
        expect(reportCallerText(c, true)).toBe('Shadow — NOT accepted (refused at the gateway) · 1 call');
        expect(reportCallerText(c, false)).toBe('Shadow — NOT accepted (advisory) · 1 call');
    });
});
