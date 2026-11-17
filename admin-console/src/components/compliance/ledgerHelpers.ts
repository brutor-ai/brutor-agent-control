/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure helpers behind the Evidence Ledger panels, kept
 * out of the component files so each of those exports only its component:
 * the bundle request a selector becomes, the children of a record in its
 * run, and the latest receipt each witness produced.
 */

import type { BundleBuildRequest, EvidenceRecordSummary, EvidenceTreeHead } from '../../lib/complianceEvidenceTypes';
import { readChain } from '../../lib/evidenceSurfaceTypes';
import { dayEnd, dayStart, parseIdList } from './complianceFormat';

export type BundleSelectorKind = 'record_ids' | 'root_task_id' | 'ai_system_id';

export interface BundleRequestInput {
    kind: BundleSelectorKind;
    recordIds: string;
    rootTaskId: string;
    aiSystemId: string;
    from: string;
    to: string;
    audience: string;
    frameworkId: string;
    closureDepth: number;
}

/** The request body for the chosen selector, or the reason there is none. */
export function buildBundleRequest(input: BundleRequestInput): { body: BundleBuildRequest } | { error: string } {
    let selector: BundleBuildRequest['selector'];
    if (input.kind === 'record_ids') {
        const ids = parseIdList(input.recordIds);
        if (ids.length === 0) return { error: 'Enter at least one record id.' };
        selector = { record_ids: ids };
    } else if (input.kind === 'root_task_id') {
        const id = input.rootTaskId.trim();
        if (!id) return { error: 'Enter a run id.' };
        selector = { root_task_id: id };
    } else {
        if (!input.aiSystemId) return { error: 'Choose an AI System.' };
        if (!input.from || !input.to) return { error: 'A system selector needs a from and a to day.' };
        if (input.to < input.from) return { error: 'The window ends before it starts.' };
        selector = { ai_system_id: input.aiSystemId, from: dayStart(input.from), to: dayEnd(input.to) };
    }
    const depth = Number.isFinite(input.closureDepth) ? Math.max(0, Math.min(8, Math.floor(input.closureDepth))) : 1;
    return {
        body: {
            selector,
            audience: input.audience.trim() || null,
            framework_id: input.frameworkId || null,
            closure_depth: depth,
        },
    };
}

/** Children = records of the run whose chain names `recordId` as parent. */
export function childrenOf(recordId: string, siblings: EvidenceRecordSummary[]): EvidenceRecordSummary[] {
    return siblings.filter((s) => readChain(s.chain)?.parentRecordId === recordId);
}

export interface LatestReceipt {
    verdict: string;
    treeSize: number;
    error: string | null;
}

/** The latest receipt each witness produced across the loaded heads
 *  (newest head first), keyed by witness id. */
export function latestReceiptByWitness(heads: EvidenceTreeHead[]): Map<string, LatestReceipt> {
    const out = new Map<string, LatestReceipt>();
    for (const h of heads) {
        for (const r of h.receipts ?? []) {
            if (!r.witness_id || out.has(r.witness_id)) continue;
            out.set(r.witness_id, { verdict: r.verdict, treeSize: h.tree_size, error: r.error ?? null });
        }
    }
    return out;
}
