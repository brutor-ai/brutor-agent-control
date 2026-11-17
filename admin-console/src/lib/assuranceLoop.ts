/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The assurance-loop position rule: which node of the continuous loop a
 * system has reached, derived from report facts. Pure, so it is testable
 * without rendering and importable by any surface that wants to say
 * "this system is at …".
 */

export interface AssuranceLoopFacts {
    /** Runs observed in the reporting period. */
    totalRuns: number;
    /** Learned baselines by state. */
    baselinesReady: number;
    baselinesLearning: number;
    /** Materialised health state (`healthy`, `degraded`, `learning`, `unknown`, …). */
    healthState?: string | null;
    /** The period verdict (`assured`, `assured_with_exceptions`, `violations`, `insufficient_evidence`). */
    verdictStatus?: string | null;
    /** Human loop: findings raised and still open in the period. */
    findingsOpen?: number | null;
    findingsRaised?: number | null;
    reviewAvailable?: boolean;
    /** Report snapshots captured for this system. */
    snapshots: number;
    lastSnapshotAt?: string | null;
}

export type LoopNodeState = 'done' | 'here' | 'ahead';

export interface LoopNode {
    id: 'evidence' | 'normal' | 'signals' | 'verdict' | 'review' | 'report';
    label: string;
    /** The number the node rests on, in the operator's words. */
    fact: string;
    state: LoopNodeState;
}

/** Turn report facts into the six nodes with their states. Exported so the
 *  rule is testable without rendering. */
export function loopNodes(f: AssuranceLoopFacts): LoopNode[] {
    const evidenceOk = f.totalRuns > 0;
    const normalOk = f.baselinesReady > 0;
    const signalsOk =
        !!f.healthState && !['unknown', 'learning', 'insufficient'].includes(f.healthState);
    const verdictOk = !!f.verdictStatus && f.verdictStatus !== 'insufficient_evidence';
    const open = f.findingsOpen ?? 0;
    const reviewOk = f.reviewAvailable !== false && open === 0;
    const reportOk = f.snapshots > 0;

    const ordered: Array<[LoopNode['id'], string, string, boolean]> = [
        [
            'evidence',
            'Evidence',
            evidenceOk ? `${f.totalRuns.toLocaleString()} runs in the ledger` : 'no runs observed yet',
            evidenceOk,
        ],
        [
            'normal',
            'Learned normal',
            normalOk
                ? `${f.baselinesReady} baselines ready${f.baselinesLearning ? `, ${f.baselinesLearning} learning` : ''}`
                : f.baselinesLearning
                  ? `${f.baselinesLearning} baselines still learning`
                  : 'no baselines yet',
            normalOk,
        ],
        [
            'signals',
            'Signals → health',
            signalsOk ? `health: ${f.healthState}` : `health: ${f.healthState ?? 'not computed'}`,
            signalsOk,
        ],
        [
            'verdict',
            'Verdict',
            verdictOk ? (f.verdictStatus ?? '').replace(/_/g, ' ') : 'evidence insufficient',
            verdictOk,
        ],
        [
            'review',
            'A human decides',
            f.reviewAvailable === false
                ? 'inbox not enabled'
                : open > 0
                  ? `${open} finding${open === 1 ? '' : 's'} open`
                  : `${f.findingsRaised ?? 0} raised, all decided`,
            reviewOk,
        ],
        [
            'report',
            'Signed report',
            reportOk
                ? `${f.snapshots} snapshot${f.snapshots === 1 ? '' : 's'}${
                      f.lastSnapshotAt ? `, last ${new Date(f.lastSnapshotAt).toLocaleDateString()}` : ''
                  }`
                : 'no snapshot captured',
            reportOk,
        ],
    ];

    // "Here" is the first node not yet satisfied; everything before it is
    // done, everything after is ahead. If all are satisfied the loop is
    // simply running — nothing is marked.
    let hereSeen = false;
    return ordered.map(([id, label, fact, ok]) => {
        let state: LoopNodeState = 'done';
        if (!ok && !hereSeen) {
            state = 'here';
            hereSeen = true;
        } else if (hereSeen) {
            state = 'ahead';
        }
        return { id, label, fact, state };
    });
}
