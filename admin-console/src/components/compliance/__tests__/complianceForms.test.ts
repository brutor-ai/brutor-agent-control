/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Form state ⇄ wire bodies (incident register, notice config) and the
 * ledger helpers (bundle selector, chain children, receipts by witness).
 */

import { describe, expect, it } from 'vitest';

import { incidentValuesFrom, noticeFormFrom, toIncidentCreateBody, toIncidentUpdateBody, toNoticePutBody, validateIncident } from '../complianceForms';
import { buildBundleRequest, childrenOf, latestReceiptByWitness } from '../ledgerHelpers';
import type { ComplianceIncident, EvidenceRecordSummary, EvidenceTreeHead, NoticeConfig } from '../../../lib/complianceEvidenceTypes';

describe('incident form', () => {
    const values = {
        title: '  Model leaked PII ',
        description: 'desc',
        classification: 'serious_incident',
        severity: 'high',
        becameAwareAt: '2026-09-19T10:30',
        classifiedAt: '',
        frameworks: ['eu-ai-act', 'gdpr'],
        aiSystemId: '',
        linkedRunIds: 'run-1\nrun-2, run-1',
        linkedInboxItemIds: '',
    };

    it('validates the fields the deadline rules depend on', () => {
        expect(validateIncident(values)).toBeNull();
        expect(validateIncident({ ...values, title: ' ' })).toMatch(/title/);
        expect(validateIncident({ ...values, classification: '' })).toMatch(/classification/);
        expect(validateIncident({ ...values, frameworks: [] })).toMatch(/framework/);
        expect(validateIncident({ ...values, becameAwareAt: '' })).toMatch(/aware/);
    });

    it('builds the create and update bodies', () => {
        const body = toIncidentCreateBody(values);
        expect(body.title).toBe('Model leaked PII');
        expect(body.ai_system_id).toBeNull();
        expect(body.linked_run_ids).toEqual(['run-1', 'run-2']);
        expect(body.linked_inbox_item_ids).toEqual([]);
        expect(body.frameworks).toEqual(['eu-ai-act', 'gdpr']);
        expect(new Date(body.became_aware_at).getTime()).toBe(new Date('2026-09-19T10:30').getTime());
        expect(toIncidentUpdateBody({ ...values, aiSystemId: 'sys-1' })).toMatchObject({ ai_system_id: 'sys-1', severity: 'high' });
    });

    it('carries the optional classification time and refuses one before awareness', () => {
        expect(toIncidentCreateBody(values).classified_at).toBeNull();
        const classified = { ...values, classifiedAt: '2026-09-19T20:30' };
        expect(validateIncident(classified)).toBeNull();
        expect(new Date(toIncidentCreateBody(classified).classified_at ?? '').getTime()).toBe(new Date('2026-09-19T20:30').getTime());
        expect(toIncidentUpdateBody(classified).classified_at).toBe(toIncidentCreateBody(classified).classified_at);
        expect(validateIncident({ ...values, classifiedAt: '2026-09-19T09:00' })).toMatch(/classified before/);
        expect(incidentValuesFrom({ ...({} as ComplianceIncident), classified_at: '2026-09-01T18:00:00Z' }).classifiedAt).toMatch(/^2026-09-0\dT\d\d:\d\d$/);
        expect(incidentValuesFrom(null).classifiedAt).toBe('');
    });

    it('round-trips an incident into form values', () => {
        const incident = {
            id: 'inc-1', tenant_id: 't', title: 'T', description: 'D', classification: 'breach', severity: 'low',
            became_aware_at: '2026-09-01T08:00:00Z', frameworks: ['gdpr'], deadlines: {}, deadline_status: [], deadlines_enforced: false,
            status: 'open', linked_run_ids: ['r'], linked_inbox_item_ids: ['i'], ai_system_id: 'sys-1',
        } as unknown as ComplianceIncident;
        const v = incidentValuesFrom(incident);
        expect(v).toMatchObject({ title: 'T', classification: 'breach', severity: 'low', frameworks: ['gdpr'], aiSystemId: 'sys-1', linkedRunIds: 'r', linkedInboxItemIds: 'i' });
        expect(v.becameAwareAt).toMatch(/^2026-09-01T\d\d:\d\d$/);
        expect(incidentValuesFrom(null).severity).toBe('medium');
    });
});

describe('notice form', () => {
    const stored = {
        id: 'nc-1', tenant_id: 't', ai_system_id: 'sys-1', enabled: false, surface: 'inline',
        texts: { en: 'You are talking to an AI.', de: 'Sie sprechen mit einer KI.' }, text_digests: { en: 'aa', de: 'bb' },
        exemption: { asserted: true, basis: 'obvious_from_context', rationale: 'It says so in the title.', declaration_record_id: 'rec-d' },
    } as unknown as NoticeConfig;

    it('loads the stored config and falls back to the effective one', () => {
        expect(noticeFormFrom(stored, null)).toEqual({
            enabled: false, surface: 'inline', texts: stored.texts, exemptionAsserted: true, exemptionBasis: 'obvious_from_context', exemptionRationale: 'It says so in the title.',
        });
        const effective = { ...stored, id: null, ai_system_id: null, enabled: true, surface: 'both', exemption: null } as unknown as NoticeConfig;
        expect(noticeFormFrom(null, effective)).toMatchObject({ enabled: true, surface: 'both', texts: {}, exemptionAsserted: false });
    });

    it('drops empty locales and carries the exemption only when asserted', () => {
        expect(toNoticePutBody({ enabled: true, surface: 'banner', texts: { en: 'Hi', fr: '   ' }, exemptionAsserted: false, exemptionBasis: 'other', exemptionRationale: 'x' })).toEqual({
            enabled: true, surface: 'banner', texts: { en: 'Hi' }, exemption: null,
        });
        expect(toNoticePutBody({ enabled: false, surface: 'both', texts: {}, exemptionAsserted: true, exemptionBasis: 'no_natural_person', exemptionRationale: ' batch only ' })).toEqual({
            enabled: false, surface: 'both', texts: {}, exemption: { asserted: true, basis: 'no_natural_person', rationale: 'batch only' },
        });
    });
});

describe('bundle selector', () => {
    const base = { recordIds: '', rootTaskId: '', aiSystemId: '', from: '', to: '', audience: '', frameworkId: 'eu-ai-act', closureDepth: 1 };

    it('names exactly one selector and normalises the rest', () => {
        expect(buildBundleRequest({ ...base, kind: 'record_ids', recordIds: 'a\nb\na' })).toEqual({
            body: { selector: { record_ids: ['a', 'b'] }, audience: null, framework_id: 'eu-ai-act', closure_depth: 1 },
        });
        expect(buildBundleRequest({ ...base, kind: 'root_task_id', rootTaskId: ' run-9 ', audience: ' auditor ', frameworkId: '', closureDepth: 12 })).toEqual({
            body: { selector: { root_task_id: 'run-9' }, audience: 'auditor', framework_id: null, closure_depth: 8 },
        });
        expect(buildBundleRequest({ ...base, kind: 'ai_system_id', aiSystemId: 'sys-1', from: '2026-09-01', to: '2026-09-30', closureDepth: Number.NaN })).toEqual({
            body: { selector: { ai_system_id: 'sys-1', from: '2026-09-01T00:00:00Z', to: '2026-09-30T23:59:59Z' }, audience: null, framework_id: 'eu-ai-act', closure_depth: 1 },
        });
    });

    it('refuses an empty or inverted selector', () => {
        expect(buildBundleRequest({ ...base, kind: 'record_ids' })).toEqual({ error: 'Enter at least one record id.' });
        expect(buildBundleRequest({ ...base, kind: 'root_task_id' })).toEqual({ error: 'Enter a run id.' });
        expect(buildBundleRequest({ ...base, kind: 'ai_system_id' })).toEqual({ error: 'Choose an AI System.' });
        expect(buildBundleRequest({ ...base, kind: 'ai_system_id', aiSystemId: 's', from: '2026-09-30', to: '2026-09-01' })).toEqual({ error: 'The window ends before it starts.' });
    });
});

describe('chain and receipts', () => {
    it('finds the children of a record in its run', () => {
        const rec = (record_id: string, parent?: string) => ({ record_id, chain: parent ? { parent_record_id: parent, relation: 'confirms' } : null }) as unknown as EvidenceRecordSummary;
        expect(childrenOf('p', [rec('a', 'p'), rec('b', 'x'), rec('c', 'p'), rec('p')]).map((r) => r.record_id)).toEqual(['a', 'c']);
    });

    it('takes the newest receipt per witness', () => {
        const heads = [
            { id: 'h2', tree_size: 20, receipts: [{ id: 'r3', witness_id: 'w1', kind: 'scitt', verdict: 'witnessed' }] },
            { id: 'h1', tree_size: 10, receipts: [{ id: 'r1', witness_id: 'w1', kind: 'scitt', verdict: 'invalid', error: 'bad sig' }, { id: 'r2', witness_id: 'w2', kind: 'rfc3161', verdict: 'timestamped' }] },
        ] as unknown as EvidenceTreeHead[];
        const m = latestReceiptByWitness(heads);
        expect(m.get('w1')).toEqual({ verdict: 'witnessed', treeSize: 20, error: null });
        expect(m.get('w2')).toEqual({ verdict: 'timestamped', treeSize: 10, error: null });
    });
});
