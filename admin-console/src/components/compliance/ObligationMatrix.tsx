/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System × obligation for one framework. A cell is the worst
 * statement status over the window with its `n of m` summary per basis —
 * recomputed and judged side by side, never summed. Only `met` is green;
 * `undetermined` and `not_evaluable` are amber; a not-applicable cell is
 * neutral and says so. Clicking a cell opens the drill-in for that system
 * and obligation; clicking a column header opens it for every system.
 */

import { CalendarClock } from 'lucide-react';

import type { ComplianceBoardObligation, ComplianceMatrix, ComplianceMatrixCell } from '../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';
import { EvaluatedTier, StatusChip } from './ComplianceUi';
import { cellSummary, matrixColumns, statusChip } from './complianceFormat';

export interface MatrixSelection {
    obligationKey: string;
    aiSystemId: string | null;
}

interface Props {
    matrix: ComplianceMatrix;
    /** The board's obligations — titles, references and dates for the columns. */
    obligations: ComplianceBoardObligation[];
    selection: MatrixSelection | null;
    onSelect: (selection: MatrixSelection) => void;
}

function cellClass(cell: ComplianceMatrixCell | undefined): string {
    if (!cell || cell.applicability === 'not_applicable') return 'bg-surface-raised text-muted';
    if (cell.applicability === 'undetermined') return 'bg-amber-100 text-amber-800';
    return statusChip(cell.worst_status ?? undefined);
}

export default function ObligationMatrix({ matrix, obligations, selection, onSelect }: Props) {
    const columns = matrixColumns(obligations);
    if (matrix.systems.length === 0) {
        return (
            <p className="text-xs text-muted" data-testid="matrix-empty">
                No active AI System — create one under AI Estate → AI Systems and declare its profile.
            </p>
        );
    }
    return (
        <div className={`${TABLE_CONTAINER} overflow-x-auto`} data-testid="obligation-matrix">
            <table className="text-xs min-w-full">
                <thead className={TABLE_THEAD}>
                    <tr>
                        <th className={`${TABLE_TH} sticky left-0 bg-surface-alt z-10`}>AI System</th>
                        {columns.map((o) => {
                            const active = selection?.obligationKey === o.key && selection.aiSystemId === null;
                            return (
                                <th key={o.key} className={`${TABLE_TH} align-bottom min-w-[9rem]`} scope="col">
                                    <button
                                        type="button"
                                        className={`text-left normal-case tracking-normal hover:underline ${active ? 'text-primary-600' : 'text-default'}`}
                                        onClick={() => onSelect({ obligationKey: o.key, aiSystemId: null })}
                                        title={`${o.title} · ${o.reference} · open for every system`}
                                        data-testid={`matrix-col-${o.key}`}
                                    >
                                        <span className="block font-medium">{o.title}</span>
                                        <span className="block text-muted font-normal">{o.reference}</span>
                                        <span className="inline-flex items-center gap-1 text-muted font-normal">
                                            <CalendarClock className="w-3 h-3" />
                                            {o.in_force ? 'in force' : `from ${o.applies_from}`}
                                        </span>
                                    </button>
                                </th>
                            );
                        })}
                    </tr>
                </thead>
                <tbody className={TABLE_TBODY}>
                    {matrix.systems.map((s) => {
                        const byKey = new Map(s.cells.map((c) => [c.obligation_key, c]));
                        return (
                            <tr key={s.ai_system_id} data-testid={`matrix-row-${s.ai_system_id}`}>
                                <th scope="row" className="px-4 py-2 text-left sticky left-0 bg-surface z-10 align-top">
                                    <span className="block font-medium text-default">{s.name}</span>
                                    <span className="block text-muted font-normal">
                                        {s.profile_declared ? (
                                            <>
                                                <EvaluatedTier
                                                    declared={s.risk_tier_declared ?? s.risk_tier}
                                                    evaluated={s.risk_tier}
                                                    sources={s.effective_tier_sources}
                                                    testId={`matrix-tier-${s.ai_system_id}`}
                                                />
                                                {` · ${s.role ?? 'role not declared'}`}
                                            </>
                                        ) : (
                                            'profile not declared'
                                        )}
                                    </span>
                                </th>
                                {columns.map((o) => {
                                    const cell = byKey.get(o.key);
                                    const active = selection?.obligationKey === o.key && selection.aiSystemId === s.ai_system_id;
                                    return (
                                        <td key={o.key} className="px-2 py-1.5 align-top">
                                            <button
                                                type="button"
                                                onClick={() => onSelect({ obligationKey: o.key, aiSystemId: s.ai_system_id })}
                                                className={`w-full text-left rounded border p-1.5 hover:border-strong ${
                                                    active ? 'border-primary-500 ring-1 ring-primary-500' : 'border-default'
                                                } ${cellClass(cell)}`}
                                                data-testid={`matrix-cell-${s.ai_system_id}-${o.key}`}
                                                title={cell?.reason ?? undefined}
                                            >
                                                {cell && cell.applicability === 'applies' && cell.worst_status ? (
                                                    <StatusChip status={cell.worst_status} />
                                                ) : (
                                                    <span className="text-[10px] font-semibold">
                                                        {cell ? cell.applicability.replace('_', ' ') : 'not in window'}
                                                    </span>
                                                )}
                                                <span className="block mt-1 leading-snug" data-testid={`matrix-cell-summary-${s.ai_system_id}-${o.key}`}>
                                                    {cell ? cellSummary(cell) : '—'}
                                                </span>
                                            </button>
                                        </td>
                                    );
                                })}
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}
