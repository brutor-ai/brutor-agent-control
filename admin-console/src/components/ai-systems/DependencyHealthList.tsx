/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Dependency health and the shared-service evidence basis — rendered the same way on Signals → Overview and in the
 * Assurance report, from the same `dependency_health` entries the control
 * plane computes once for the health endpoint and the report.
 *
 * Tested and assumed are kept visibly apart: a shared service carries a
 * tested status chip; an external agent carries "assumed, not tested", a
 * dashed outline, its boundary metrics and the root's declared
 * assumptions — never a status that reads like a measurement.
 */

import React from 'react';
import { AlertTriangle, Waypoints } from 'lucide-react';

import {
    DEPENDENCY_CRITICALITY_LABELS,
    DEPENDENCY_HEALTH_CLASSES,
    DEPENDENCY_HEALTH_LABELS,
    DEPENDENCY_ROLE_LABELS,
    DependencyCap,
    DependencyHealthEntry,
    SignalBasis,
} from '../../lib/aiSystemTypes';
import {
    assumptionRows,
    boundaryText,
    dependencyCapLink,
    dependencyCapSentence,
    dependencyLink,
    failingText,
    fallbackCheckText,
    signalBasisSentence,
} from '../../lib/dependencyHealth';
import { HelpTip } from '../ui/HelpTip';

function Link({
    to,
    open,
    children,
    testId,
    className,
}: {
    to: string | null;
    open?: (tab: string) => void;
    children: React.ReactNode;
    testId?: string;
    className?: string;
}) {
    if (!to || !open) {
        return (
            <span className={className} data-testid={testId}>
                {children}
            </span>
        );
    }
    return (
        <button
            type="button"
            onClick={() => open(to)}
            className={`${className ?? ''} text-left hover:underline`}
            data-link={to}
            data-testid={testId}
        >
            {children}
        </button>
    );
}

/** The banner above the verdict when a critical dependency caps it. */
export function DependencyCapBanner({
    cap,
    open,
}: {
    cap: DependencyCap;
    open?: (tab: string) => void;
}) {
    return (
        <div
            className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3"
            role="status"
            data-testid="dependency-cap-banner"
        >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" />
            <div className="text-sm text-amber-900">
                <p data-testid="dependency-cap-text">{dependencyCapSentence(cap)}</p>
                <Link
                    to={dependencyCapLink(cap)}
                    open={open}
                    className="mt-1 text-xs font-medium text-amber-900 underline"
                    testId="dependency-cap-link"
                >
                    Open {cap.name ?? cap.system_id ?? 'the dependency'}
                </Link>
            </div>
        </div>
    );
}

/** "Assured from N served calls as a shared service…" — nothing for a
 *  system on the runs basis. */
export function SignalBasisNote({
    basis,
    servedCalls,
}: {
    basis: SignalBasis | null | undefined;
    servedCalls?: number | null;
}) {
    const text = signalBasisSentence(basis, servedCalls);
    if (!text) return null;
    return (
        <p
            className="rounded-md border border-default bg-surface-alt px-3 py-2 text-xs text-secondary"
            data-testid="signal-basis-note"
        >
            {text}
        </p>
    );
}

function DependencyRow({
    entry,
    open,
}: {
    entry: DependencyHealthEntry;
    open?: (tab: string) => void;
}) {
    const assumed = entry.basis === 'assumed';
    const failing = failingText(entry);
    const assumptions = assumptionRows(entry.assumptions);
    return (
        <li
            className={`rounded-md border px-3 py-2 ${
                assumed ? 'border-dashed border-strong' : 'border-default'
            }`}
            data-testid={`dependency-health-${entry.edge_id}`}
            data-basis={entry.basis}
            data-status={entry.status}
        >
            <div className="flex flex-wrap items-center gap-2">
                <Waypoints className="h-3.5 w-3.5 shrink-0 text-muted" />
                <Link
                    to={dependencyLink(entry)}
                    open={open}
                    className="text-sm font-medium text-default"
                    testId={`dependency-health-link-${entry.edge_id}`}
                >
                    {entry.name ?? entry.system_id ?? entry.edge_id}
                </Link>
                <span className="text-[11px] uppercase tracking-wide text-muted">
                    {DEPENDENCY_ROLE_LABELS[entry.role]} · {DEPENDENCY_CRITICALITY_LABELS[entry.criticality]}
                    {entry.contributes_to_decision ? '' : ' · separable'}
                </span>
                {assumed ? (
                    /* Never a status chip: an external agent is not measured. */
                    <span
                        className="ml-auto rounded border border-dashed border-strong px-1.5 py-0.5 text-[11px] font-medium text-secondary"
                        data-testid={`dependency-basis-${entry.edge_id}`}
                    >
                        Assumed, not tested
                    </span>
                ) : (
                    <span className="ml-auto flex items-center gap-1.5">
                        <span
                            className="text-[10px] uppercase tracking-wide text-muted"
                            data-testid={`dependency-basis-${entry.edge_id}`}
                        >
                            tested
                        </span>
                        <span
                            className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${DEPENDENCY_HEALTH_CLASSES[entry.status]}`}
                            data-testid={`dependency-status-${entry.edge_id}`}
                        >
                            {DEPENDENCY_HEALTH_LABELS[entry.status]}
                        </span>
                    </span>
                )}
            </div>
            {(failing || entry.caps_root) && (
                <p className="mt-1 text-xs text-amber-800" data-testid={`dependency-failing-${entry.edge_id}`}>
                    {failing}
                    {entry.caps_root && (
                        <span className="font-medium">{failing ? ' — ' : ''}caps this system at degraded</span>
                    )}
                </p>
            )}
            {entry.note && (
                <p className="mt-1 text-xs text-secondary" data-testid={`dependency-note-${entry.edge_id}`}>
                    {entry.note}
                </p>
            )}
            {entry.fallback_check && (
                <p
                    className={`mt-1 text-xs ${
                        entry.fallback_check.held === false ? 'font-medium text-red-700' : 'text-muted'
                    }`}
                    data-testid={`dependency-fallback-${entry.edge_id}`}
                    data-held={String(entry.fallback_check.held)}
                >
                    {fallbackCheckText(entry.fallback_check)}
                </p>
            )}
            {entry.boundary && (
                <p className="mt-1 text-xs text-muted" data-testid={`dependency-boundary-${entry.edge_id}`}>
                    <span className="text-secondary">At the gateway boundary: </span>
                    {boundaryText(entry.boundary)}
                </p>
            )}
            {assumed && (
                <div className="mt-1 text-xs" data-testid={`dependency-assumptions-${entry.edge_id}`}>
                    {assumptions.length === 0 ? (
                        <p className="italic text-muted">No assumptions declared.</p>
                    ) : (
                        <dl className="grid grid-cols-[auto_1fr] gap-x-2">
                            {assumptions.map(([k, v]) => (
                                <React.Fragment key={k}>
                                    <dt className="text-muted">{k}</dt>
                                    <dd className="text-secondary break-words">{v}</dd>
                                </React.Fragment>
                            ))}
                        </dl>
                    )}
                </div>
            )}
        </li>
    );
}

/** Every declared dependency's health. Renders nothing when none are
 *  declared — a system with no dependencies has no section to show. */
export function DependencyHealthList({
    entries,
    open,
}: {
    entries: DependencyHealthEntry[] | null | undefined;
    open?: (tab: string) => void;
}) {
    if (!entries || entries.length === 0) return null;
    return (
        <section className="space-y-2" data-testid="dependency-health">
            <div className="flex items-center gap-2">
                <h4 className="text-sm font-semibold text-default">Dependencies</h4>
                <span className="text-xs text-muted">{entries.length}</span>
                <HelpTip
                    title="Dependency health"
                    text="What this system relies on, as its verdict sees it. A shared service is tested: it is an AI System here, assured from its own evidence, and a critical one that is not healthy caps this system at degraded. An external agent is assumed, not tested: Brutor sees only the gateway boundary, so its calls, errors and latency are shown with the assumptions declared for it."
                />
            </div>
            <ul className="space-y-1.5">
                {entries.map((e) => (
                    <DependencyRow key={e.edge_id} entry={e} open={open} />
                ))}
            </ul>
        </section>
    );
}

export default DependencyHealthList;
