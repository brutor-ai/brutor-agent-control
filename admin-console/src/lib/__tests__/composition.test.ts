/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The pure composition model and the dependency rules that
 * mirror the control plane's 422s. Each rule is checked in isolation:
 * a draft that satisfies every other rule fails exactly the one under test.
 */

import { describe, it, expect } from 'vitest';

import {
    buildCompositionModel,
    edgeState,
    effectiveTierText,
    emptyDependencyDraft,
    modelNodeKeys,
    normalizeDependencyDraft,
    parseExpectedOutcomes,
    validateDependencyDraft,
} from '../composition';
import { CONTRIBUTED_LINKS } from '../contributedLinks';
import type { AISystemCompositionView, DependencyDraft } from '../aiSystemTypes';
import { COMPOSITION_PAYLOAD } from '../../components/ai-systems/__tests__/fixtures/compositionPayload';

const valid = (over: Partial<DependencyDraft> = {}): DependencyDraft => ({
    ...emptyDependencyDraft(),
    depends_on_system_id: 'sys-fraud',
    ...over,
});

describe('validateDependencyDraft', () => {
    it('accepts a complete critical shared service', () => {
        expect(validateDependencyDraft(valid(), 'sys-demo')).toEqual({});
    });

    it.each<[string, Partial<DependencyDraft>, string]>([
        ['shared service without target', { depends_on_system_id: null }, 'depends_on_system_id'],
        ['self-edge', { depends_on_system_id: 'sys-demo' }, 'depends_on_system_id'],
        ['external without agent card', { role: 'external', depends_on_system_id: null, agent_card_id: null, supplier_legal_entity: 'Acme AB' }, 'agent_card_id'],
        ['external with a pin', { role: 'external', depends_on_system_id: null, agent_card_id: 'c', contract_pin: 'ctr', supplier_legal_entity: 'Acme AB' }, 'contract_pin'],
        // Art 25(4): an external agent names its supplier.
        ['external without supplier', { role: 'external', depends_on_system_id: null, agent_card_id: 'c' }, 'supplier_legal_entity'],
        ['external with a blank supplier', { role: 'external', depends_on_system_id: null, agent_card_id: 'c', supplier_legal_entity: '   ' }, 'supplier_legal_entity'],
        ['a supplier longer than the column', { supplier_legal_entity: 'x'.repeat(256) }, 'supplier_legal_entity'],
        ['degradable without fallback', { criticality: 'degradable', fallback: null }, 'fallback'],
        ['degradable with blank fallback', { criticality: 'degradable', fallback: '  ' }, 'fallback'],
        ['non-contributing without rationale', { contributes_to_decision: false }, 'separability_rationale'],
    ])('rejects %s', (_name, over, field) => {
        expect(Object.keys(validateDependencyDraft(valid(over), 'sys-demo'))).toEqual([field]);
    });

    it('accepts each rule once satisfied', () => {
        expect(validateDependencyDraft(valid({ criticality: 'degradable', fallback: 'manual' }), 'sys-demo')).toEqual({});
        expect(
            validateDependencyDraft(valid({ contributes_to_decision: false, separability_rationale: 'formatting only' }), 'sys-demo'),
        ).toEqual({});
        expect(
            validateDependencyDraft(
                valid({ role: 'external', depends_on_system_id: null, agent_card_id: 'card', supplier_legal_entity: 'Acme AB' }),
                'sys-demo',
            ),
        ).toEqual({});
        // A shared service may name a supplier (another legal entity) or not.
        expect(validateDependencyDraft(valid({ supplier_legal_entity: 'Group Services Ltd' }), 'sys-demo')).toEqual({});
    });
});

describe('normalizeDependencyDraft', () => {
    it('drops fields that no longer apply', () => {
        const out = normalizeDependencyDraft(
            valid({
                role: 'external',
                agent_card_id: 'card',
                criticality: 'critical',
                fallback: 'stale',
                contributes_to_decision: true,
                separability_rationale: 'stale',
                contract_pin: 'ctr',
            }),
        );
        expect(out.fallback).toBeNull();
        expect(out.separability_rationale).toBeNull();
        expect(out.contract_pin).toBeNull();
    });
});

describe('buildCompositionModel', () => {
    const model = buildCompositionModel(COMPOSITION_PAYLOAD);

    it('lays sections out agents → gateway → resources', () => {
        expect(model.sections.map((s) => `${s.column}:${s.id}`)).toEqual([
            'left:agents',
            'left:callers',
            'centre:policies',
            'centre:limits',
            'right:resources',
            'right:delegation',
        ]);
    });

    it('gives every node a unique key and a link', () => {
        const keys = modelNodeKeys(model);
        expect(new Set(keys).size).toBe(keys.length);
        for (const s of model.sections) for (const n of s.nodes) expect(n.link).not.toBe('');
    });

    it('links a node to a page of another distribution only when the resolver answers', () => {
        const view: AISystemCompositionView = {
            ...COMPOSITION_PAYLOAD,
            gateway: {
                ...COMPOSITION_PAYLOAD.gateway,
                policies: [
                    ...COMPOSITION_PAYLOAD.gateway.policies,
                    { kind: 'cache', id: 'cache-1', name: 'Answers cache', summary: null, inherited_from: null },
                ],
            },
        };
        const links = (m: ReturnType<typeof buildCompositionModel>) =>
            Object.fromEntries(m.sections.flatMap((s) => s.nodes.map((n) => [n.key, n.link])));

        // Nobody answers (the default): those nodes have no link, every other has one.
        const bare = links(buildCompositionModel(view));
        expect(Object.keys(bare).filter((key) => bare[key] === null).sort()).toEqual(['kb:kb-policy', 'policy:cache:cache-1']);
        expect(Object.values(bare).filter((link) => link !== null && !/\S/.test(link))).toEqual([]);

        // Somebody answers one of the two: that node links there, the other still nowhere.
        const asked: string[] = [];
        const answered = links(
            buildCompositionModel(view, {}, {}, (link) => {
                asked.push(link);
                return link === CONTRIBUTED_LINKS.knowledgeBases ? 'their-kb-page' : null;
            }),
        );
        expect(answered['kb:kb-policy']).toBe('their-kb-page');
        expect(answered['policy:cache:cache-1']).toBeNull();
        expect([...new Set(asked)].sort()).toEqual([CONTRIBUTED_LINKS.cachePolicies, CONTRIBUTED_LINKS.knowledgeBases].sort());
        // Nothing else changed.
        expect({ ...answered, 'kb:kb-policy': null }).toEqual(bare);
    });

    it('categorises by section: neutral agents/callers, then the four categories', () => {
        const cats = Object.fromEntries(
            model.sections.map((s) => [s.id, [...new Set(s.nodes.map((n) => n.category))]]),
        );
        expect(cats).toEqual({
            agents: [null],
            callers: [null],
            policies: ['policy'],
            limits: ['limit'],
            resources: ['ai_resource'],
            delegation: ['delegation_agent'],
        });
    });

    it('attaches findings to the node they name', () => {
        const all = model.sections.flatMap((s) => s.nodes);
        const byKey = Object.fromEntries(all.map((n) => [n.key, n]));
        expect(byKey['undeclared:sys-kyc'].findings.map((f) => f.code)).toEqual(['composition.undeclared_dependency']);
        expect(byKey['caller:sys-shadow'].findings.map((f) => f.code)).toEqual(['composition.unaccepted_caller']);
        expect(byKey['dep:dep-address'].findings.map((f) => f.code)).toEqual(['composition.declared_never_observed']);
        expect(byKey['dep:dep-fraud'].findings).toEqual([]);
    });
});

describe('edgeState / effectiveTierText', () => {
    it('classifies declared vs observed', () => {
        expect(edgeState(true, 3)).toBe('declared_observed');
        expect(edgeState(true, 0)).toBe('declared_unobserved');
        expect(edgeState(false, 3)).toBe('observed_undeclared');
    });

    it('names the source of a lifted tier', () => {
        expect(effectiveTierText('minimal', 'high', [{ system_id: 'sys-x', system_name: 'Loan Desk' }])).toBe('minimal → high via Loan Desk');
        expect(effectiveTierText('high', 'high', [])).toBe('high');
        expect(effectiveTierText(null, null, [])).toBeNull();
    });
});

describe('expected delegate outcomes', () => {
    it('is not declared by default — null, no check', () => {
        expect(emptyDependencyDraft().expected_outcomes).toBeNull();
        expect(normalizeDependencyDraft(valid()).expected_outcomes).toBeNull();
    });

    it('accepts <outcome> and <outcome>.<variant> ids', () => {
        expect(
            validateDependencyDraft(valid({ expected_outcomes: ['screening_completed', 'screening_completed.hit'] }), 'sys-demo'),
        ).toEqual({});
        expect(validateDependencyDraft(valid({ expected_outcomes: [] }), 'sys-demo')).toEqual({});
    });

    it('refuses a malformed id, naming it', () => {
        const e = validateDependencyDraft(valid({ expected_outcomes: ['ok.id', 'Bad', 'a.b.c'] }), 'sys-demo');
        expect(Object.keys(e)).toEqual(['expected_outcomes']);
        expect(e.expected_outcomes).toContain('Bad, a.b.c');
        expect(e.expected_outcomes).not.toContain('ok.id');
    });

    it('sends a declared list trimmed, de-duplicated and sorted, as the server stores it', () => {
        expect(
            normalizeDependencyDraft(valid({ expected_outcomes: [' screening_completed.review', 'screening_completed.clear', 'screening_completed.clear', ''] }))
                .expected_outcomes,
        ).toEqual(['screening_completed.clear', 'screening_completed.review']);
        expect(normalizeDependencyDraft(valid({ expected_outcomes: [] })).expected_outcomes).toEqual([]);
    });

    it('parses what the operator typed: commas or whitespace', () => {
        expect(parseExpectedOutcomes('b.x, a\n a  c.y,,')).toEqual(['a', 'b.x', 'c.y']);
        expect(parseExpectedOutcomes('   ')).toEqual([]);
    });
});
