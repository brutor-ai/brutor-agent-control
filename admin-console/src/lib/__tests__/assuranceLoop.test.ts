/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where a system stands in the continuous assurance loop. "Here" is the
 * FIRST unsatisfied node — a later node that happens to be satisfied must
 * still read as ahead, or the diagram claims progress the system has not
 * made.
 */

import { describe, it, expect } from 'vitest';
import { loopNodes, type AssuranceLoopFacts } from '../assuranceLoop';

const facts = (over: Partial<AssuranceLoopFacts> = {}): AssuranceLoopFacts => ({
    totalRuns: 0,
    baselinesReady: 0,
    baselinesLearning: 0,
    snapshots: 0,
    ...over,
});

const complete = facts({
    totalRuns: 1200,
    baselinesReady: 4,
    healthState: 'healthy',
    verdictStatus: 'assured',
    findingsOpen: 0,
    findingsRaised: 3,
    snapshots: 2,
    lastSnapshotAt: '2026-08-01T00:00:00Z',
});

const states = (f: AssuranceLoopFacts) => loopNodes(f).map((n) => n.state);
const node = (f: AssuranceLoopFacts, id: string) => loopNodes(f).find((n) => n.id === id)!;

describe('loopNodes', () => {
    it('always returns the six nodes in loop order', () => {
        expect(loopNodes(facts()).map((n) => n.id)).toEqual([
            'evidence',
            'normal',
            'signals',
            'verdict',
            'review',
            'report',
        ]);
    });

    it('marks nothing when every node is satisfied — the loop is just running', () => {
        expect(states(complete)).toEqual(Array(6).fill('done'));
    });

    it('puts "here" on the first unsatisfied node and everything after it ahead', () => {
        expect(states(facts())).toEqual(['here', 'ahead', 'ahead', 'ahead', 'ahead', 'ahead']);
        expect(states({ ...complete, baselinesReady: 0 })).toEqual([
            'done',
            'here',
            'ahead',
            'ahead',
            'ahead',
            'ahead',
        ]);
    });

    it('does not credit a later satisfied node once "here" has been passed', () => {
        // Snapshots exist, but signals were never computed — the report node
        // must read `ahead`, not `done`.
        const f = { ...complete, healthState: 'unknown' };
        expect(node(f, 'signals').state).toBe('here');
        expect(node(f, 'report').state).toBe('ahead');
    });
});

describe('evidence node', () => {
    it('counts runs with thousands separators, and says so when there are none', () => {
        expect(node(facts({ totalRuns: 12345 }), 'evidence').fact).toBe('12,345 runs in the ledger');
        expect(node(facts(), 'evidence').fact).toBe('no runs observed yet');
    });
});

describe('learned-normal node', () => {
    it('reports ready baselines and mentions any still learning', () => {
        expect(node(facts({ baselinesReady: 3 }), 'normal').fact).toBe('3 baselines ready');
        expect(node(facts({ baselinesReady: 3, baselinesLearning: 2 }), 'normal').fact).toBe(
            '3 baselines ready, 2 learning',
        );
    });

    it('is not satisfied by baselines that are only learning', () => {
        const n = node(facts({ totalRuns: 5, baselinesLearning: 2 }), 'normal');
        expect(n.fact).toBe('2 baselines still learning');
        expect(n.state).toBe('here');
    });

    it('says there are no baselines at all when there are none', () => {
        expect(node(facts({ totalRuns: 5 }), 'normal').fact).toBe('no baselines yet');
    });
});

describe('signals node', () => {
    it('does not treat unknown, learning or insufficient as a computed health', () => {
        const unscored = ['unknown', 'learning', 'insufficient'];
        expect(
            unscored.map((s) => node({ ...complete, healthState: s }, 'signals').state),
        ).toEqual(unscored.map(() => 'here'));
        expect(node({ ...complete, healthState: 'degraded' }, 'signals').state).toBe('done');
    });

    it('says "not computed" rather than printing null health', () => {
        expect(node(facts({ totalRuns: 1, baselinesReady: 1 }), 'signals').fact).toBe(
            'health: not computed',
        );
        expect(node({ ...complete, healthState: null }, 'signals').fact).toBe(
            'health: not computed',
        );
    });
});

describe('verdict node', () => {
    it('accepts any verdict except insufficient_evidence', () => {
        expect(node({ ...complete, verdictStatus: 'violations' }, 'verdict').state).toBe('done');
        expect(node({ ...complete, verdictStatus: 'insufficient_evidence' }, 'verdict')).toMatchObject(
            { state: 'here', fact: 'evidence insufficient' },
        );
    });

    it('reads the verdict back without underscores', () => {
        expect(node({ ...complete, verdictStatus: 'assured_with_exceptions' }, 'verdict').fact).toBe(
            'assured with exceptions',
        );
    });
});

describe('review node', () => {
    it('is unsatisfied while findings are open, and pluralises correctly', () => {
        expect(node({ ...complete, findingsOpen: 1 }, 'review')).toMatchObject({
            state: 'here',
            fact: '1 finding open',
        });
        expect(node({ ...complete, findingsOpen: 4 }, 'review').fact).toBe('4 findings open');
    });

    it('reports how many were raised once all are decided', () => {
        expect(node(complete, 'review').fact).toBe('3 raised, all decided');
        expect(node({ ...complete, findingsRaised: undefined }, 'review').fact).toBe(
            '0 raised, all decided',
        );
    });

    it('a disabled inbox still leaves the loop unclosed, and says why', () => {
        // The human decision is a node of the loop, not an optional feature:
        // with the inbox off the loop stops here rather than reading complete.
        const n = node({ ...complete, reviewAvailable: false, findingsOpen: 9 }, 'review');
        expect(n.fact).toBe('inbox not enabled');
        expect(n.state).toBe('here');
        expect(node({ ...complete, reviewAvailable: false }, 'report').state).toBe('ahead');
    });

    it('treats an absent findingsOpen as none open', () => {
        expect(node({ ...complete, findingsOpen: null }, 'review').state).toBe('done');
    });
});

describe('report node', () => {
    it('pluralises snapshots and appends the last capture date', () => {
        expect(node({ ...complete, snapshots: 1, lastSnapshotAt: null }, 'report').fact).toBe(
            '1 snapshot',
        );
        expect(node(complete, 'report').fact).toMatch(/^2 snapshots, last /);
    });

    it('says nothing was captured when there are no snapshots', () => {
        expect(node({ ...complete, snapshots: 0 }, 'report')).toMatchObject({
            state: 'here',
            fact: 'no snapshot captured',
        });
    });
});
