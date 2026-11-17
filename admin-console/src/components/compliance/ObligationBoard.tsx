/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The obligation board: one expandable row per obligation of the
 * selected framework across every AI System, grouped in force / upcoming /
 * not applicable (no system in scope), each dated. The board keeps two
 * rules:
 *
 *   * applicability is DATED — `applies_from` and whether it binds today;
 *     nothing here is a checkbox;
 *   * `undetermined` is a to-do, not a result — a system that has not
 *     declared the fact a rule needs is amber with the missing declaration
 *     named.
 *
 * "Evaluated evidence →" on a row opens that obligation's drill-in under
 * the row (`detail`, rendered by the page); the row whose evidence is open
 * is always expanded.
 */

import { useState } from 'react';
import { AlertTriangle, CalendarClock, ChevronDown, ChevronRight } from 'lucide-react';

import type { ComplianceBoardObligation } from '../../lib/complianceEvidenceTypes';
import { EU_AI_ACT_ROLE_LABEL } from '../../lib/obligationTypes';
import { EvaluatedTier } from './ComplianceUi';
import ObligationEvidenceList from './ObligationEvidenceList';
import { groupObligations, LINK_BUTTON } from './complianceFormat';

const STATUS_CHIP: Record<string, string> = {
    applies: 'bg-green-100 text-green-800',
    undetermined: 'bg-amber-100 text-amber-800',
    not_applicable: 'bg-surface-raised text-secondary',
};

interface Props {
    obligations: ComplianceBoardObligation[];
    /** Open the AI System's Compliance tab. */
    onOpenSystem?: (aiSystemId: string) => void;
    /** Open the evidence drill-in for one obligation (all systems). */
    onOpenEvidence?: (obligationKey: string) => void;
    /** The obligation whose evaluated evidence is open, and that evidence. */
    selectedKey?: string | null;
    detail?: React.ReactNode;
}

export default function ObligationBoard({ obligations, onOpenSystem, onOpenEvidence, selectedKey, detail }: Props) {
    const [open, setOpen] = useState<Set<string>>(new Set());
    const { inForce, upcoming, notApplicable } = groupObligations(obligations, (o) =>
        (o.systems ?? []).some((s) => s.status !== 'not_applicable'),
    );

    const toggle = (key: string) =>
        setOpen((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            return next;
        });

    const renderObligation = (o: ComplianceBoardObligation) => {
        const evidenceOpen = selectedKey === o.key && !!detail;
        const expanded = open.has(o.key) || evidenceOpen;
        const systems = (o.systems ?? []).filter((s) => s.status !== 'not_applicable');
        const inScope = systems.length;
        return (
            <div key={o.key} data-testid={`obligation-${o.key}`} className="border border-default rounded-lg bg-surface">
                <button
                    type="button"
                    className="w-full flex items-center gap-3 p-3 text-left"
                    aria-expanded={expanded}
                    onClick={() => toggle(o.key)}
                >
                    {expanded ? (
                        <ChevronDown className="w-4 h-4 text-muted flex-none" />
                    ) : (
                        <ChevronRight className="w-4 h-4 text-muted flex-none" />
                    )}
                    <div className="min-w-0 flex-1 min-h-0">
                        <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-sm font-medium text-default">{o.title}</span>
                            <span className="text-xs text-muted">{o.reference}</span>
                            {o.needs_review && (
                                <span
                                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-amber-100 text-amber-800"
                                    title={o.review_note ?? undefined}
                                >
                                    <AlertTriangle className="w-3 h-3" /> review
                                </span>
                            )}
                            {(o.evidence?.length ?? 0) > 0 && (
                                <span className="text-[10px] text-muted" data-testid={`obligation-evidence-count-${o.key}`}>
                                    {o.evidence?.length} statement{o.evidence?.length === 1 ? '' : 's'}
                                </span>
                            )}
                        </div>
                    </div>
                    <span
                        className="inline-flex items-center gap-1 text-xs text-secondary flex-none"
                        title={o.in_force ? `In force since ${o.applies_from}` : `Applies from ${o.applies_from}`}
                    >
                        <CalendarClock className="w-3.5 h-3.5" />
                        {o.in_force ? 'in force' : `from ${o.applies_from}`}
                    </span>
                    <span
                        className={`flex-none px-2 py-0.5 rounded-full text-xs font-medium ${
                            inScope
                                ? o.undetermined_count
                                    ? STATUS_CHIP.undetermined
                                    : STATUS_CHIP.applies
                                : STATUS_CHIP.not_applicable
                        }`}
                        data-testid={`obligation-scope-${o.key}`}
                    >
                        {inScope
                            ? `${inScope} system${inScope === 1 ? '' : 's'}${
                                  o.undetermined_count ? ` · ${o.undetermined_count} undetermined` : ''
                              }`
                            : 'no systems in scope'}
                    </span>
                </button>

                {expanded && (
                    <div className="border-t border-default p-3 space-y-3">
                        <p className="text-xs text-secondary leading-relaxed">{o.summary}</p>
                        {o.review_note && <p className="text-xs text-amber-800 bg-amber-100 rounded p-2">{o.review_note}</p>}

                        <div>
                            <h4 className="text-[10px] font-semibold text-muted uppercase tracking-wide mb-1">Systems in scope</h4>
                            {systems.length === 0 && (
                                <p className="text-xs text-muted italic">No AI System is in scope for this obligation.</p>
                            )}
                            <div className="space-y-1">
                                {systems.map((s) => (
                                    <div key={s.id} className="flex items-center gap-2 text-xs p-2 rounded bg-surface-alt border border-default">
                                        <button
                                            type="button"
                                            className="text-primary-600 hover:underline font-medium"
                                            onClick={() => onOpenSystem?.(s.id)}
                                        >
                                            {s.name}
                                        </button>
                                        <span className="text-muted">
                                            <EvaluatedTier
                                                declared={s.eu_ai_act_risk_tier}
                                                evaluated={s.risk_tier_evaluated}
                                                testId={`board-tier-${o.key}-${s.id}`}
                                            />{' '}
                                            ·{' '}
                                            {s.eu_ai_act_role ? (EU_AI_ACT_ROLE_LABEL[s.eu_ai_act_role] ?? s.eu_ai_act_role) : 'role not declared'}
                                        </span>
                                        <span
                                            className={`ml-auto px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${STATUS_CHIP[s.status] ?? STATUS_CHIP.not_applicable}`}
                                            title={s.reason}
                                        >
                                            {s.status.replace('_', ' ')}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        </div>

                        <div>
                            <div className="flex items-center justify-between mb-1">
                                <h4 className="text-[10px] font-semibold text-muted uppercase tracking-wide">Evidence statements</h4>
                                {onOpenEvidence && !evidenceOpen && (
                                    <button
                                        type="button"
                                        className={LINK_BUTTON}
                                        onClick={() => onOpenEvidence(o.key)}
                                        data-testid={`obligation-open-evidence-${o.key}`}
                                    >
                                        Evaluated evidence →
                                    </button>
                                )}
                            </div>
                            <ObligationEvidenceList evidence={o.evidence} testId={`obligation-evidence-${o.key}`} />
                        </div>
                        {evidenceOpen && <div data-testid={`obligation-detail-${o.key}`}>{detail}</div>}
                    </div>
                )}
            </div>
        );
    };

    const group = (title: string, testId: string, items: ComplianceBoardObligation[], emptyText: string) => (
        <div>
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wide mb-2">
                {title} ({items.length})
            </h3>
            <div className="space-y-2" data-testid={testId}>
                {items.length === 0 ? <p className="text-xs text-muted italic">{emptyText}</p> : items.map(renderObligation)}
            </div>
        </div>
    );

    return (
        <div className="space-y-5" data-testid="obligation-board">
            {group('In force', 'obligations-in-force', inForce, 'No obligation in force has a system in scope.')}
            {group('Upcoming', 'obligations-upcoming', upcoming, 'No upcoming obligation has a system in scope.')}
            {notApplicable.length > 0 &&
                group('Not applicable — no system in scope', 'obligations-not-applicable', notApplicable, '')}
        </div>
    );
}
