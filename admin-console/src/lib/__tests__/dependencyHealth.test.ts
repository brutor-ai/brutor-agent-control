/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The wording every surface uses for dependency health
 * and for a shared service's evidence basis, pinned without rendering.
 */

import { describe, it, expect } from 'vitest';

import {
    boundaryText,
    dependencyCapLink,
    dependencyCapSentence,
    dependencyLink,
    driftDependency,
    failingText,
    fallbackCheckText,
    signalBasisSentence,
} from '../dependencyHealth';
import {
    COMPOSITION_FINDING_LABELS,
    DRIFT_METRIC_LABELS,
    driftMetricLabel,
    type AISystemContract,
    type DriftEvent,
} from '../aiSystemTypes';
import { closureDependencyContract, contractFact, pinStatusText, pinnableContracts } from '../composition';
import {
    ADDRESS_DEGRADABLE,
    BUREAU_EXTERNAL,
    CLOSURE_DEPENDENCIES,
    SCORING_CAP,
    SCORING_CAPPING,
} from '../../components/ai-systems/__tests__/fixtures/dependencyHealthPayloads';
import { COMPOSITION_PAYLOAD } from '../../components/ai-systems/__tests__/fixtures/compositionPayload';

describe('signal basis', () => {
    it('says nothing for a system on its own runs', () => {
        expect(signalBasisSentence('runs', 10)).toBeNull();
        expect(signalBasisSentence(undefined)).toBeNull();
    });

    it('names the served-call count for a shared service', () => {
        expect(signalBasisSentence('segments', 128)).toMatch(/^Assured from 128 served calls as a shared service/);
        expect(signalBasisSentence('segments', 1)).toMatch(/1 served call as/);
        expect(signalBasisSentence('segments', null)).toMatch(/Assured from the calls it served/);
    });
});

describe('dependency cap', () => {
    it('names the dependency and the signal it fails on', () => {
        const s = dependencyCapSentence(SCORING_CAP);
        expect(s).toContain('Scoring Service');
        expect(s).toContain('failing on liveness');
        expect(s).toMatch(/^Capped at degraded/);
    });

    it("links to the dependency's own signals", () => {
        expect(dependencyCapLink(SCORING_CAP)).toBe('ai-systems:sys-scoring:signals');
        expect(dependencyCapLink({ ...SCORING_CAP, system_id: null })).toBeNull();
    });
});

describe('dependency entries', () => {
    it('links a shared service to its signals and an external agent to the card registry', () => {
        expect(dependencyLink(SCORING_CAPPING)).toBe('ai-systems:sys-scoring:signals');
        expect(dependencyLink(BUREAU_EXTERNAL)).toBe('a2a-agent-cards');
    });

    it('names the failing signal and the system it fails through', () => {
        expect(failingText(SCORING_CAPPING)).toBe('failing on liveness');
        expect(failingText(ADDRESS_DEGRADABLE)).toBe('failing on its own dependency via Geo Lookup');
        expect(failingText(BUREAU_EXTERNAL)).toBeNull();
    });

    it('states the fallback check in all three states', () => {
        expect(fallbackCheckText({ unanswered_calls: 0, root_runs_completed: 0, held: null })).toMatch(/not exercised/);
        expect(fallbackCheckText({ unanswered_calls: 2, root_runs_completed: 2, held: true })).toMatch(/^Fallback held: 2 unanswered calls/);
        expect(fallbackCheckText({ unanswered_calls: 3, root_runs_completed: 2, held: false })).toBe(
            'Fallback did NOT hold: 1 of 3 unanswered calls left the run incomplete.',
        );
    });

    it('renders boundary metrics as calls, errors, rate and p95', () => {
        expect(boundaryText(BUREAU_EXTERNAL.boundary!)).toBe('20 calls · 2 errors (10%) · p95 840 ms');
        // Calls inside a declared incident window are counted but
        // not graded, and the text says which is which.
        expect(boundaryText({ ...BUREAU_EXTERNAL.boundary!, excluded_calls: 5 })).toBe(
            '20 calls · 5 inside incident windows, not judged · 2 errors (10%) · p95 840 ms',
        );
        expect(boundaryText({ calls: 0, errors: 0, error_rate: null, p95_latency_ms: null, last_seen_at: null })).toMatch(
            /No calls/,
        );
    });
});

describe('dependency drift on the root', () => {
    const base: Omit<DriftEvent, 'drift_class' | 'metric_key' | 'cause_evidence'> = {
        id: 'd1', ai_system_id: 'sys-demo', severity: 'critical', attributed_cause: 'dependency_degraded',
        correlated_metrics: [], response_taken: {}, status: 'active', is_open: true, is_explained: true,
    };

    it('resolves the dependency from the evidence, else from the metric key', () => {
        const withEvidence: DriftEvent = {
            ...base, drift_class: 'dependency', metric_key: 'dependency:sys-scoring',
            cause_evidence: { dependency_system_id: 'sys-scoring', dependency_name: 'Scoring Service' },
        };
        expect(driftDependency(withEvidence)).toEqual({ id: 'sys-scoring', name: 'Scoring Service' });
        const bare: DriftEvent = { ...withEvidence, cause_evidence: {} };
        expect(driftDependency(bare)).toEqual({ id: 'sys-scoring', name: null });
    });

    it('is null for any other finding', () => {
        const own: DriftEvent = { ...base, drift_class: 'behavioural', metric_key: 'action_count', cause_evidence: {} };
        expect(driftDependency(own)).toBeNull();
    });
});

describe('drift metric labels (service metrics)', () => {
    it('labels the new service metrics', () => {
        for (const key of ['llm_call_count', 'total_tokens', 'error_rate']) {
            expect(DRIFT_METRIC_LABELS[key]).toBeTruthy();
            expect(driftMetricLabel(key)).toBe(DRIFT_METRIC_LABELS[key]);
        }
    });

    it('reads "per served call" on the segment basis', () => {
        expect(driftMetricLabel('action_count', 'segments')).toBe('Actions per served call');
        expect(driftMetricLabel('cost_per_run', 'segments')).toBe('Cost per served call');
        expect(driftMetricLabel('action_count', 'runs')).toBe('Actions per run');
    });

    it('names a dependency finding by the dependency', () => {
        expect(driftMetricLabel('dependency:sys-scoring', 'runs', 'Scoring Service')).toBe('Dependency Scoring Service');
        expect(driftMetricLabel('dependency:sys-scoring')).toBe('Dependency sys-scoring');
    });
});

describe('composition finding codes', () => {
    it('labels every new check code', () => {
        expect(COMPOSITION_FINDING_LABELS['composition.grant_without_edge']).toBeTruthy();
        expect(COMPOSITION_FINDING_LABELS['composition.edge_without_grant']).toBeTruthy();
        expect(COMPOSITION_FINDING_LABELS['composition.dependency_pin_stale']).toBeTruthy();
    });
});

describe('pins', () => {
    const address = COMPOSITION_PAYLOAD.dependencies.find((d) => d.id === 'dep-address')!;

    it('shows where a pinned target is now', () => {
        expect(contractFact(address)).toBe('pinned to v2');
        expect(contractFact(address, 2)).toBe('pinned to v2 · target active v2');
        expect(contractFact(address, 3)).toBe('pinned to v2 · target active v3 — behind');
        expect(contractFact(address, null)).toBe('pinned to v2 · target has no active contract');
    });

    it('says whether a pin is behind', () => {
        expect(pinStatusText(2, 3)).toMatch(/^Pinned v2, target active v3 — this pin is behind/);
        expect(pinStatusText(3, 3)).toMatch(/up to date/);
    });

    it('offers only signed contracts, newest first', () => {
        const c = (version: number, status: AISystemContract['status'], is_approved = true) =>
            ({ id: `c${version}`, version, status, is_approved, is_active: status === 'active' }) as AISystemContract;
        const offered = pinnableContracts([
            c(1, 'superseded'), c(2, 'rejected', false), c(3, 'active'), c(4, 'draft', false), c(5, 'draft', true),
        ]);
        expect(offered.map((x) => x.version)).toEqual([5, 3, 1]);
    });

    it('describes a sealed edge’s contract', () => {
        expect(closureDependencyContract(CLOSURE_DEPENDENCIES[0])).toBe('no contract (external)');
        expect(closureDependencyContract(CLOSURE_DEPENDENCIES[1])).toBe('pinned v2 · abcdef01');
        expect(closureDependencyContract(CLOSURE_DEPENDENCIES[2])).toBe('follows v2 · 98765432');
    });
});
