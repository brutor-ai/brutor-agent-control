/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Evaluated evidence statements: one row per
 * statement with its basis chip, status, `n of m` (never a percentage), the
 * resolver's detail, the limit text that says what the statement does NOT
 * show, the catalog version it was evaluated under, its citations (sealed
 * records — opened in the record dialog) and, for a judged row, the human
 * agreement stated beside it:
 *
 *   JUDGED · met 57 of 60 · human agreement 0.92 [0.83–0.97], n = 34, blind
 *
 * `other_frameworks` becomes "show this evidence under other frameworks".
 * Exported for the AI System Compliance tab; keep the props stable.
 */

import { getRecord } from '../../lib/narrow';
import { BasisChip, RecordLink, StatusChip } from './ComplianceUi';
import { judgedRowText, nOfM, readOtherFrameworks } from './complianceFormat';

/** The fields a statement row reads — satisfied by the obligation detail's
 *  statements, the transparency statements and report rows. */
export interface StatementLike {
    key: string;
    statement?: string | null;
    basis: string | null;
    resolver?: string | null;
    status: string;
    n?: number | null;
    m?: number | null;
    detail?: string | null;
    limit?: string | null;
    citations?: string[] | null;
    catalog_version?: string | null;
    facts?: Record<string, unknown> | null;
    other_frameworks?: Array<Record<string, unknown>> | null;
    /** Report rows carry agreement here instead of in `facts`. */
    human_agreement?: unknown;
    /** Non-record pointers (evidence pointers, exports) the resolver read. */
    refs?: unknown[] | null;
}

interface Props {
    statements: StatementLike[];
    onOpenRecord?: (recordId: string) => void;
    /** Switch the console to another framework's obligation. */
    onOtherFramework?: (frameworkId: string, obligationKey: string) => void;
    testId?: string;
    emptyText?: string;
}

const MAX_CITATIONS_SHOWN = 6;

export default function StatementList({
    statements,
    onOpenRecord,
    onOtherFramework,
    testId = 'statement-list',
    emptyText = 'No evidence statement is catalogued for this obligation.',
}: Props) {
    if (statements.length === 0) {
        return (
            <p className="text-xs text-muted italic" data-testid={`${testId}-empty`}>
                {emptyText}
            </p>
        );
    }
    return (
        <ul className="space-y-2" data-testid={testId}>
            {statements.map((s, i) => {
                const rowId = `${testId}-${s.key}`;
                const citations = s.citations ?? [];
                const others = readOtherFrameworks(s.other_frameworks);
                const agreementRaw = s.human_agreement ?? getRecord(s.facts, 'agreement');
                const judged = s.basis === 'judged';
                const refs = s.refs ?? null;
                return (
                    <li
                        key={`${s.key}-${s.catalog_version ?? ''}-${i}`}
                        className="rounded border border-default bg-surface-alt p-2.5 text-xs min-w-0"
                        data-testid={rowId}
                    >
                        <div className="flex items-start gap-2 flex-wrap">
                            <BasisChip basis={s.basis} testId={`${rowId}-basis`} />
                            <StatusChip status={s.status} testId={`${rowId}-status`} />
                            {s.m !== null && s.m !== undefined && (
                                <span className="text-default font-medium" data-testid={`${rowId}-nofm`}>
                                    {nOfM(s.n, s.m)}
                                </span>
                            )}
                            {s.catalog_version && (
                                <span className="ml-auto text-[10px] text-muted font-mono" title="Catalog version">
                                    catalog {s.catalog_version}
                                </span>
                            )}
                        </div>
                        {s.statement && <p className="mt-1.5 text-default leading-relaxed">{s.statement}</p>}
                        {judged && (
                            <p className="mt-1 text-secondary font-mono text-[11px]" data-testid={`${rowId}-judged`}>
                                {judgedRowText(s.status, s.n, s.m, agreementRaw)}
                            </p>
                        )}
                        {s.detail && <p className="mt-1 text-secondary">{s.detail}</p>}
                        {s.limit && (
                            <p className="mt-1 text-amber-800" data-testid={`${rowId}-limit`}>
                                Limit: {s.limit}
                            </p>
                        )}
                        {s.resolver && (
                            <p className="mt-1 text-muted">
                                resolver <code className="font-mono">{s.resolver}</code>
                            </p>
                        )}
                        {citations.length > 0 && (
                            <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1" data-testid={`${rowId}-citations`}>
                                <span className="text-muted">cites</span>
                                {citations.slice(0, MAX_CITATIONS_SHOWN).map((rid) =>
                                    onOpenRecord ? (
                                        <RecordLink key={rid} recordId={rid} onOpen={onOpenRecord} />
                                    ) : (
                                        <code key={rid} className="font-mono text-secondary">
                                            {rid.slice(0, 12)}…
                                        </code>
                                    ),
                                )}
                                {citations.length > MAX_CITATIONS_SHOWN && (
                                    <span className="text-muted">+{citations.length - MAX_CITATIONS_SHOWN} more</span>
                                )}
                            </div>
                        )}
                        {refs && refs.length > 0 && (
                            <p className="mt-1 text-muted">{refs.length} non-record reference{refs.length === 1 ? '' : 's'}</p>
                        )}
                        {others.length > 0 && (
                            <div className="mt-1.5" data-testid={`${rowId}-other-frameworks`}>
                                <span className="text-muted">Show this evidence under other frameworks: </span>
                                {others.map((o) => (
                                    <button
                                        key={`${o.frameworkId}:${o.obligationKey}`}
                                        type="button"
                                        className="mr-2 text-primary-600 hover:underline disabled:text-muted disabled:no-underline"
                                        disabled={!onOtherFramework}
                                        onClick={() => onOtherFramework?.(o.frameworkId, o.obligationKey)}
                                        data-testid={`other-framework-${o.frameworkId}-${o.obligationKey}`}
                                        title={`${o.title} ${o.reference}`}
                                    >
                                        {o.frameworkTitle} · {o.reference || o.title}
                                    </button>
                                ))}
                            </div>
                        )}
                    </li>
                );
            })}
        </ul>
    );
}
