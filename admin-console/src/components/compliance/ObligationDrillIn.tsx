/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One obligation's evaluated evidence: per system,
 * the statements it is shown against with their basis chip, status,
 * `n of m`, limit, catalog version and citations (sealed records opened in
 * the record dialog); judged rows carry their human agreement; and every
 * statement's "show this evidence under other frameworks" links.
 */

import { useCallback } from 'react';
import { X } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import type { ObligationDetailResponse } from '../../lib/complianceEvidenceTypes';
import { ErrorNote, Loading, Section, StatusChip } from './ComplianceUi';
import StatementList from './StatementList';
import { byBasisText, formatDay, NOT_LEGAL_ADVICE } from './complianceFormat';

interface Props {
    framework: string;
    obligationKey: string;
    /** One system, or `null` for every system in scope. */
    aiSystemId: string | null;
    window: { from: string; to: string };
    onClose: () => void;
    onOpenRecord: (recordId: string) => void;
    onOtherFramework?: (frameworkId: string, obligationKey: string) => void;
    onOpenSystem?: (aiSystemId: string) => void;
}

export default function ObligationDrillIn({
    framework,
    obligationKey,
    aiSystemId,
    window,
    onClose,
    onOpenRecord,
    onOtherFramework,
    onOpenSystem,
}: Props) {
    const { gatewayClient } = useGateway();
    const loader = useCallback(
        (): Promise<ObligationDetailResponse> =>
            gatewayClient.getComplianceObligationDetail(obligationKey, {
                framework,
                ai_system_id: aiSystemId ?? undefined,
                from: window.from,
                to: window.to,
            }),
        [gatewayClient, framework, obligationKey, aiSystemId, window.from, window.to],
    );
    const { data, error, loading } = useKeyedLoad(
        `${framework}:${obligationKey}:${aiSystemId ?? '*'}:${window.from}:${window.to}`,
        loader,
        'Failed to evaluate the obligation',
    );

    return (
        <Section
            title={
                data ? (
                    <>
                        {data.title} <span className="font-normal text-muted">{data.reference}</span>
                    </>
                ) : (
                    obligationKey
                )
            }
            subtitle={
                data
                    ? `${data.framework.title} · window ${formatDay(data.window.from)} → ${formatDay(data.window.to)} · ${NOT_LEGAL_ADVICE}`
                    : undefined
            }
            actions={
                <button
                    type="button"
                    onClick={onClose}
                    aria-label="Close evidence"
                    className="p-1 rounded hover:bg-surface-raised text-muted"
                    data-testid="drill-in-close"
                >
                    <X className="w-4 h-4" />
                </button>
            }
            testId="obligation-drill-in"
        >
            {loading && !data && <Loading label="Evaluating statements…" testId="drill-in-loading" />}
            <ErrorNote message={error} testId="drill-in-error" />
            {data && (
                <div className="space-y-4">
                    {data.summary && <p className="text-xs text-secondary leading-relaxed">{data.summary}</p>}
                    {data.limit && (
                        <p className="text-xs text-amber-800" data-testid="drill-in-limit">
                            Limit: {data.limit}
                        </p>
                    )}
                    {data.resolved_keys.length > 1 && (
                        <p className="text-xs text-muted">
                            Resolved from {data.resolved_keys.filter((k) => k !== data.key).join(', ')} (an earlier catalog key).
                        </p>
                    )}
                    {data.systems.length === 0 && (
                        <p className="text-xs text-muted" data-testid="drill-in-no-systems">
                            No AI System is in scope for this obligation in the window.
                        </p>
                    )}
                    {data.systems.map((s) => (
                        <div key={s.ai_system_id} className="rounded-lg border border-default p-3 space-y-2" data-testid={`drill-in-system-${s.ai_system_id}`}>
                            <div className="flex items-center gap-2 flex-wrap">
                                {onOpenSystem ? (
                                    <button type="button" className="text-sm font-medium text-primary-600 hover:underline" onClick={() => onOpenSystem(s.ai_system_id)}>
                                        {s.name}
                                    </button>
                                ) : (
                                    <span className="text-sm font-medium text-default">{s.name}</span>
                                )}
                            </div>
                            {s.obligations.map((o) => (
                                <div key={`${o.key}-${o.applies_from}`} className="space-y-2" data-testid={`drill-in-obligation-${s.ai_system_id}-${o.key}`}>
                                    <div className="flex items-center gap-2 flex-wrap text-xs">
                                        <span
                                            className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                                                o.applicability === 'applies'
                                                    ? 'bg-green-100 text-green-800'
                                                    : o.applicability === 'undetermined'
                                                      ? 'bg-amber-100 text-amber-800'
                                                      : 'bg-surface-raised text-secondary'
                                            }`}
                                            title={o.reason}
                                        >
                                            {o.applicability.replace('_', ' ')}
                                        </span>
                                        <span className="text-muted">{o.in_force ? 'in force' : `from ${o.applies_from}`}</span>
                                        {o.worst_status && <StatusChip status={o.worst_status} />}
                                        {byBasisText(o.by_basis) && <span className="text-secondary">{byBasisText(o.by_basis)}</span>}
                                        {o.needs_review && o.review_note && <span className="text-amber-800">{o.review_note}</span>}
                                    </div>
                                    {o.applicability === 'undetermined' && (
                                        <p className="text-xs text-amber-800" data-testid={`drill-in-undetermined-${s.ai_system_id}`}>
                                            {o.reason}
                                        </p>
                                    )}
                                    <StatementList
                                        statements={o.statements ?? []}
                                        onOpenRecord={onOpenRecord}
                                        onOtherFramework={onOtherFramework}
                                        testId={`statements-${s.ai_system_id}`}
                                    />
                                </div>
                            ))}
                        </div>
                    ))}
                </div>
            )}
        </Section>
    );
}
