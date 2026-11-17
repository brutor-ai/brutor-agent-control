/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The chain around one record: its parent — the record it
 * confirms, supersedes, assesses or adjudicates, from `chain.parent_record_id`
 * — and its children, the records of the same run that name it as their
 * parent. Every node opens the record dialog. A `planned` or
 * `awaiting_human` record with no child is shown as an open half: the
 * may/did gap the contracts make visible rather than hide.
 */

import { useCallback } from 'react';
import { ArrowDown, ArrowUp, X } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { chipFor, VERDICT_CHIP, type EvidenceRecordDetail, type EvidenceRecordSummary } from '../../lib/complianceEvidenceTypes';
import { describeChainLink, readChain } from '../../lib/evidenceSurfaceTypes';
import { ErrorNote, Loading, RecordLink, Section } from './ComplianceUi';
import { formatWhen } from './complianceFormat';
import { childrenOf } from './ledgerHelpers';

interface ChainView {
    detail: EvidenceRecordDetail;
    parent: EvidenceRecordSummary | EvidenceRecordDetail | null;
    parentRelation: string | null;
    children: EvidenceRecordSummary[];
}

interface Props {
    recordId: string;
    onOpenRecord: (recordId: string) => void;
    onClose: () => void;
}

export default function ChainPanel({ recordId, onOpenRecord, onClose }: Props) {
    const { gatewayClient } = useGateway();
    const loader = useCallback(async (): Promise<ChainView> => {
        const detail = await gatewayClient.getEvidenceRecord(recordId);
        const link = readChain(detail.chain);
        const siblings = detail.root_task_id
            ? (await gatewayClient.listEvidenceRecords({ root_task_id: detail.root_task_id, limit: 200 })).items
            : [];
        let parent: ChainView['parent'] = null;
        if (link) {
            parent = siblings.find((s) => s.record_id === link.parentRecordId) ?? null;
            if (!parent) {
                try {
                    parent = await gatewayClient.getEvidenceRecord(link.parentRecordId);
                } catch {
                    parent = null; // pruned or outside the tenant's view — the link still shows
                }
            }
        }
        return { detail, parent, parentRelation: link?.relation ?? null, children: childrenOf(recordId, siblings) };
    }, [gatewayClient, recordId]);
    const { data, error, loading } = useKeyedLoad(`chain:${recordId}`, loader, 'Failed to load the chain');

    const link = data ? readChain(data.detail.chain) : null;
    const openHalf = data && (data.detail.verdict === 'planned' || data.detail.verdict === 'awaiting_human') && data.children.length === 0;

    return (
        <Section
            title="Chain"
            subtitle="The record this one links to, and the records that link to it. Relations are read from each child's own chain block."
            actions={
                <button type="button" onClick={onClose} aria-label="Close chain" className="p-1 rounded hover:bg-surface-raised text-muted" data-testid="chain-close">
                    <X className="w-4 h-4" />
                </button>
            }
            testId="chain-panel"
        >
            {loading && !data && <Loading testId="chain-loading" />}
            <ErrorNote message={error} testId="chain-error" />
            {data && (
                <ol className="space-y-1.5 text-xs" data-testid="chain-nodes">
                    {link && (
                        <li className="flex items-center gap-2 flex-wrap" data-testid="chain-parent">
                            <ArrowUp className="w-3.5 h-3.5 text-muted flex-none" />
                            <span className="text-muted">{describeChainLink(data.detail.verdict, data.parentRelation ?? link.relation)}</span>
                            <ChainNode record={data.parent} recordId={link.parentRecordId} onOpen={onOpenRecord} />
                        </li>
                    )}
                    {!link && (
                        <li className="text-muted" data-testid="chain-no-parent">
                            No parent — this record starts its chain.
                        </li>
                    )}
                    <li className="flex items-center gap-2 flex-wrap rounded border border-primary-500 px-2 py-1" data-testid="chain-self">
                        <span className="text-muted">this record</span>
                        <ChainNode record={data.detail} recordId={data.detail.record_id} onOpen={onOpenRecord} />
                    </li>
                    {data.children.map((c) => {
                        const cl = readChain(c.chain);
                        return (
                            <li key={c.record_id} className="flex items-center gap-2 flex-wrap pl-4" data-testid={`chain-child-${c.record_id}`}>
                                <ArrowDown className="w-3.5 h-3.5 text-muted flex-none" />
                                <span className="text-muted">{cl ? describeChainLink(c.verdict, cl.relation) : 'child'}</span>
                                <ChainNode record={c} recordId={c.record_id} onOpen={onOpenRecord} />
                            </li>
                        );
                    })}
                    {openHalf && (
                        <li className="pl-4 text-amber-800" data-testid="chain-open-half">
                            No later record chains to this one — an open half (proposed, not yet confirmed or resolved).
                        </li>
                    )}
                    {!openHalf && data.children.length === 0 && (
                        <li className="pl-4 text-muted" data-testid="chain-no-children">
                            No record links to this one.
                        </li>
                    )}
                </ol>
            )}
        </Section>
    );
}

function ChainNode({
    record,
    recordId,
    onOpen,
}: {
    record: EvidenceRecordSummary | EvidenceRecordDetail | null;
    recordId: string;
    onOpen: (id: string) => void;
}) {
    return (
        <span className="inline-flex items-center gap-2 flex-wrap">
            {record && (
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${chipFor(VERDICT_CHIP, record.verdict)}`}>{record.verdict}</span>
            )}
            <RecordLink recordId={recordId} onOpen={onOpen} />
            {record ? (
                <span className="text-secondary">
                    {record.surface}
                    {record.effect_type ? ` · ${record.effect_type}` : ''} · {formatWhen(record.timestamp)}
                </span>
            ) : (
                <span className="text-muted">not held here (pruned, or outside this view)</span>
            )}
        </span>
    );
}
