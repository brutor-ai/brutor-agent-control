/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Dependencies as contract terms — the sealed closure's
 * `dependencies` section, and the diff entries a dependency change
 * produces.
 *
 * A shared service the system FOLLOWS carries the service's active
 * contract in the caller's closure, so a service promotion drifts the
 * caller and its diff shows "dependency X widened (v1 → v2): …". The entry
 * nests the service's own classified diff; an approver can open it and
 * read exactly what their system now relies on without leaving the page.
 */

import { useState } from 'react';
import { ChevronDown, ChevronRight, Minus, Plus } from 'lucide-react';

import { useAISystemNames } from '../../hooks/useAISystemNames';
import AISystemNamesNotice from './AISystemNamesNotice';
import {
    ClosureDependency,
    DEPENDENCY_CRITICALITY_LABELS,
    DEPENDENCY_ROLE_LABELS,
    DiffEntry,
} from '../../lib/aiSystemTypes';
import { closureDependencyContract, isDependencyContractChange } from '../../lib/composition';
import { assumptionRows } from '../../lib/dependencyHealth';
import { diffEntryText } from '../../lib/agentRelease';

/** One diff line. A `dependency_contract_changed` entry with the service's
 *  own diff gets a disclosure that lists it, widened first. */
export function DiffEntryLine({
    entry,
    tone,
}: {
    entry: DiffEntry;
    tone: 'widened' | 'narrowed' | 'changed';
}) {
    const [open, setOpen] = useState(false);
    const nested = isDependencyContractChange(entry) ? entry.dependency_diff ?? null : null;
    const Icon = tone === 'widened' ? Plus : Minus;
    const nestedCount = nested
        ? nested.widened.length + nested.narrowed.length + nested.changed.length
        : 0;
    return (
        <li
            className={`text-xs ${tone === 'widened' ? 'text-default' : 'text-muted'}`}
            data-testid={isDependencyContractChange(entry) ? 'diff-dependency-entry' : undefined}
        >
            <div className="flex items-start gap-1.5">
                {tone !== 'changed' && (
                    <Icon
                        className={`w-3 h-3 shrink-0 mt-0.5 ${tone === 'widened' ? 'text-amber-600' : ''}`}
                    />
                )}
                <span data-testid={entry.kind === 'agent_implementation' ? 'diff-agent-implementation' : undefined}>
                    {entry.section === 'outcomes' && (
                        <span
                            className="mr-1 px-1 py-px rounded bg-surface-alt border border-default text-[10px] uppercase tracking-wide text-muted"
                            data-testid="diff-outcome-entry"
                        >
                            outcomes
                        </span>
                    )}
                    {diffEntryText(entry)}
                    {entry.called_out && (
                        <span
                            className="ml-1.5 px-1 py-px rounded bg-amber-100 text-amber-800 text-[10px] font-medium"
                            title="This outcome's runs leave the success rate, the cost of the work and drift detection. Read why before approving."
                            data-testid="diff-called-out"
                        >
                            takes runs out of judgement
                        </span>
                    )}
                </span>
            </div>
            {nested && nestedCount > 0 && (
                <div className="ml-4 mt-0.5">
                    <button
                        type="button"
                        onClick={() => setOpen((v) => !v)}
                        className="flex items-center gap-1 text-[11px] font-medium text-primary-700 hover:underline"
                        aria-expanded={open}
                        data-testid="diff-dependency-toggle"
                    >
                        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                        The service&apos;s own diff ({nestedCount})
                    </button>
                    {open && (
                        <div
                            className="mt-1 space-y-1 border-l-2 border-default pl-2"
                            data-testid="diff-dependency-nested"
                        >
                            {(['widened', 'narrowed', 'changed'] as const).map((k) =>
                                nested[k].length > 0 ? (
                                    <div key={k} data-testid={`diff-dependency-nested-${k}`}>
                                        <p className="text-[11px] text-muted">
                                            {k === 'widened'
                                                ? 'Widened'
                                                : k === 'narrowed'
                                                  ? 'Tightened'
                                                  : 'Documentation'}
                                        </p>
                                        <ul className="space-y-0.5">
                                            {nested[k].map((e, i) => (
                                                <li key={`${e.kind}-${i}`} className="text-[11px] text-default">
                                                    {diffEntryText(e)}
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                ) : null,
                            )}
                        </div>
                    )}
                </div>
            )}
        </li>
    );
}

/** The sealed `dependencies` section: what the system relied on when the
 *  version was signed, and which contract of each service it ran against. */
export function ClosureDependencies({ dependencies }: { dependencies: ClosureDependency[] }) {
    // The closure stores ids, never names (a rename must not move a hash).
    const systemNames = useAISystemNames(dependencies.some((d) => Boolean(d.depends_on_system_id)));
    const { names } = systemNames;
    if (dependencies.length === 0) return null;
    return (
        <div data-testid="closure-dependencies">
            <p className="text-xs font-medium text-default">What it depends on</p>
            <p className="text-[11px] text-muted mb-1.5">
                Declared dependency edges, sealed with the contract of each shared service it ran
                against. A followed service promoting a new contract drifts this system.
            </p>
            <AISystemNamesNotice names={systemNames} className="block mb-1.5" />
            <ul className="space-y-1.5">
                {dependencies.map((d) => {
                    const target = d.depends_on_system_id
                        ? names[d.depends_on_system_id] ?? d.depends_on_system_id
                        : `agent card ${d.agent_card_id ?? '?'}`;
                    const assumptions = assumptionRows(d.role === 'external' ? d.assumptions : null);
                    return (
                        <li
                            key={`${d.role}-${d.depends_on_system_id ?? ''}-${d.agent_card_id ?? ''}`}
                            className={`rounded border px-2 py-1.5 text-xs ${
                                d.role === 'external' ? 'border-dashed border-strong' : 'border-default'
                            }`}
                            data-testid="closure-dependency-item"
                        >
                            <div className="flex flex-wrap items-center gap-2">
                                <span className="font-medium text-default">{target}</span>
                                <span className="text-[10px] uppercase tracking-wide text-muted">
                                    {DEPENDENCY_ROLE_LABELS[d.role]} ·{' '}
                                    {DEPENDENCY_CRITICALITY_LABELS[d.criticality]}
                                    {d.contributes_to_decision ? ' · contributes to decisions' : ' · separable'}
                                </span>
                                <span
                                    className="ml-auto font-mono text-[11px] text-secondary"
                                    data-testid="closure-dependency-contract"
                                >
                                    {closureDependencyContract(d)}
                                </span>
                            </div>
                            {d.fallback && (
                                <p className="mt-0.5 text-muted">
                                    <span className="text-secondary">Fallback:</span> {d.fallback}
                                </p>
                            )}
                            {d.separability_rationale && (
                                <p className="mt-0.5 text-muted">
                                    <span className="text-secondary">Separability:</span>{' '}
                                    {d.separability_rationale}
                                </p>
                            )}
                            {d.supplier_legal_entity && (
                                <p className="mt-0.5 text-muted" data-testid="closure-dependency-supplier">
                                    <span className="text-secondary">Supplier:</span> {d.supplier_legal_entity}
                                </p>
                            )}
                            {d.expected_outcomes && (
                                <p className="mt-0.5 text-muted" data-testid="closure-dependency-expected-outcomes">
                                    <span className="text-secondary">Expected outcomes:</span>{' '}
                                    {d.expected_outcomes.length ? (
                                        <span className="font-mono">{d.expected_outcomes.join(' · ')}</span>
                                    ) : (
                                        'none — every answer is a finding'
                                    )}
                                </p>
                            )}
                            {assumptions.length > 0 && (
                                <p className="mt-0.5 text-muted">
                                    <span className="text-secondary">Assumed, not tested:</span>{' '}
                                    {assumptions.map(([k, v]) => `${k} ${v}`).join(' · ')}
                                </p>
                            )}
                        </li>
                    );
                })}
            </ul>
        </div>
    );
}
