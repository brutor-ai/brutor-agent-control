/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Obligations for ONE AI System: every obligation of the
 * selected framework as this system sees it, grouped in force / upcoming /
 * not applicable. A row is the obligation, its date, the worst statement
 * status over the window and the `n of m` per basis (recomputed and judged
 * side by side, never summed). `undetermined` is a to-do: it stays in the
 * dated groups, amber, naming the declaration that is missing.
 *
 * Selecting a row opens its evaluated evidence directly under the row
 * (`detail` is rendered by the page, which owns the drill-in).
 */

import { Fragment } from 'react';
import { AlertTriangle, CalendarClock, ChevronDown, ChevronRight } from 'lucide-react';

import type { ComplianceBoardObligation, ComplianceMatrixSystem } from '../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';
import { EvaluatedTier, StatusChip } from './ComplianceUi';
import { cellSummary, LINK_BUTTON, systemObligationGroups, type SystemObligationRow } from './complianceFormat';

interface Props {
    system: ComplianceMatrixSystem;
    obligations: ComplianceBoardObligation[];
    selectedKey: string | null;
    onSelect: (obligationKey: string | null) => void;
    /** The evaluated evidence of the selected obligation, shown under its row. */
    detail?: React.ReactNode;
    onOpenSystem?: (aiSystemId: string) => void;
    onEditProfile?: () => void;
}

const COUNT_TILE = 'rounded-md border border-default bg-surface-alt px-3 py-2 min-w-[7rem]';

export default function SystemObligations({ system, obligations, selectedKey, onSelect, detail, onOpenSystem, onEditProfile }: Props) {
    const groups = systemObligationGroups(obligations, system.cells);
    const dated = [...groups.inForce, ...groups.upcoming];
    const undetermined = dated.filter((r) => r.cell?.applicability === 'undetermined').length;

    const row = (r: SystemObligationRow) => {
        const o = r.obligation;
        const open = selectedKey === r.key;
        const isUndetermined = r.cell?.applicability === 'undetermined';
        return (
            <Fragment key={r.key}>
                <tr
                    data-testid={`system-obligation-${r.key}`}
                    className={`cursor-pointer hover:bg-surface-hover ${open ? 'bg-surface-alt' : ''}`}
                    onClick={() => onSelect(open ? null : r.key)}
                >
                    <td className="px-4 py-2.5 align-top">
                        <button
                            type="button"
                            className="flex items-start gap-2 text-left"
                            aria-expanded={open}
                            title={o.summary ?? undefined}
                        >
                            {open ? (
                                <ChevronDown className="w-4 h-4 text-muted flex-none mt-0.5" />
                            ) : (
                                <ChevronRight className="w-4 h-4 text-muted flex-none mt-0.5" />
                            )}
                            <span>
                                <span className="block text-sm font-medium text-default">{o.title}</span>
                                <span className="block text-xs text-muted">{o.reference}</span>
                            </span>
                        </button>
                    </td>
                    <td className="px-4 py-2.5 align-top text-xs text-secondary whitespace-nowrap">
                        <span className="inline-flex items-center gap-1">
                            <CalendarClock className="w-3.5 h-3.5" />
                            {o.in_force ? `since ${o.applies_from}` : `from ${o.applies_from}`}
                        </span>
                        {o.needs_review && (
                            <span
                                className="ml-2 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-amber-100 text-amber-800"
                                title={o.review_note ?? undefined}
                            >
                                <AlertTriangle className="w-3 h-3" /> review
                            </span>
                        )}
                    </td>
                    <td className="px-4 py-2.5 align-top" data-testid={`system-obligation-status-${r.key}`}>
                        {isUndetermined ? (
                            <span className="inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-800">
                                undetermined
                            </span>
                        ) : r.cell?.worst_status ? (
                            <StatusChip status={r.cell.worst_status} />
                        ) : (
                            <span className="text-xs text-muted">no statement</span>
                        )}
                    </td>
                    <td className="px-4 py-2.5 align-top text-xs text-secondary" data-testid={`system-obligation-summary-${r.key}`}>
                        {r.cell ? cellSummary(r.cell) : '—'}
                    </td>
                </tr>
                {open && detail && (
                    <tr data-testid={`system-obligation-detail-${r.key}`}>
                        <td colSpan={4} className="px-4 py-3 bg-surface-alt">
                            {detail}
                        </td>
                    </tr>
                )}
            </Fragment>
        );
    };

    const table = (title: string, testId: string, rows: SystemObligationRow[], emptyText: string) => (
        <div>
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wide mb-2">
                {title} ({rows.length})
            </h3>
            {rows.length === 0 ? (
                <p className="text-xs text-muted italic" data-testid={testId}>
                    {emptyText}
                </p>
            ) : (
                <div className={`${TABLE_CONTAINER} overflow-x-auto`} data-testid={testId}>
                    <table className="w-full">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={`${TABLE_TH} w-[34%]`}>Obligation</th>
                                <th className={`${TABLE_TH} w-[16%]`}>Applies</th>
                                <th className={`${TABLE_TH} w-[12%]`}>Status</th>
                                <th className={TABLE_TH}>Evidence in the window</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>{rows.map(row)}</tbody>
                    </table>
                </div>
            )}
        </div>
    );

    return (
        <div className="space-y-5" data-testid="system-obligations">
            <div className="rounded-lg border border-default bg-surface p-4 flex items-start justify-between gap-4 flex-wrap">
                <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-default">{system.name}</h3>
                    <p className="text-xs text-secondary mt-0.5" data-testid="system-obligations-profile">
                        {system.profile_declared ? (
                            <>
                                <EvaluatedTier
                                    declared={system.risk_tier_declared ?? system.risk_tier}
                                    evaluated={system.risk_tier}
                                    sources={system.effective_tier_sources}
                                    testId="system-obligations-tier"
                                />
                                {` · ${system.role ?? 'role not declared'}`}
                            </>
                        ) : (
                            'Profile not declared — applicability cannot be determined until it is.'
                        )}
                    </p>
                    <div className="mt-2 flex items-center gap-3">
                        {onEditProfile && (
                            <button type="button" className={LINK_BUTTON} onClick={onEditProfile} data-testid="system-obligations-edit-profile">
                                {system.profile_declared ? 'Edit profile →' : 'Declare profile →'}
                            </button>
                        )}
                        {onOpenSystem && (
                            <button type="button" className={LINK_BUTTON} onClick={() => onOpenSystem(system.ai_system_id)}>
                                Open AI System →
                            </button>
                        )}
                    </div>
                </div>
                <dl className="flex items-stretch gap-2 flex-wrap" data-testid="system-obligations-counts">
                    {(
                        [
                            ['In force', groups.inForce.length],
                            ['Upcoming', groups.upcoming.length],
                            ['Undetermined', undetermined],
                            ['Not applicable', groups.notApplicable.length],
                        ] as const
                    ).map(([label, n]) => (
                        <div key={label} className={COUNT_TILE}>
                            <dt className="text-[10px] text-muted uppercase tracking-wide">{label}</dt>
                            <dd className={`text-lg font-semibold ${label === 'Undetermined' && n > 0 ? 'text-amber-700' : 'text-default'}`}>{n}</dd>
                        </div>
                    ))}
                </dl>
            </div>

            {table('In force', 'system-obligations-in-force', groups.inForce, 'No obligation in force applies to this system.')}
            {table('Upcoming', 'system-obligations-upcoming', groups.upcoming, 'No upcoming obligation applies to this system.')}

            {groups.notApplicable.length > 0 && (
                <div>
                    <h3 className="text-xs font-semibold text-muted uppercase tracking-wide mb-2">
                        Not applicable ({groups.notApplicable.length})
                    </h3>
                    <ul className="rounded-lg border border-default bg-surface divide-y divide-default" data-testid="system-obligations-not-applicable">
                        {groups.notApplicable.map((r) => (
                            <li key={r.key} className="px-4 py-2 flex items-baseline gap-3 flex-wrap text-xs" data-testid={`system-obligation-${r.key}`}>
                                <span className="text-secondary font-medium">{r.obligation.title}</span>
                                <span className="text-muted">{r.obligation.reference}</span>
                                <span className="text-muted ml-auto">
                                    {r.cell ? (r.cell.reason ?? 'not applicable') : 'not evaluated in the window'}
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}
