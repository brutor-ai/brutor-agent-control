/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Composite AI Systems — the words the health panel,
 * the assurance report and the printed report use for a system's
 * dependencies and for a shared service's evidence basis. Pure, so every
 * surface says the same thing and the tests can pin the wording.
 *
 * Two rules the wording carries:
 *
 *  - **Tested vs assumed.** A shared service's status is tested (it is an
 *    AI System here, assured from its own evidence). An external agent is
 *    never given a tested status: Brutor sees only the gateway boundary,
 *    so its line says "assumed, not tested" and shows boundary metrics
 *    and the root's declared assumptions instead.
 *  - **The cap names its cause.** A critical dependency that is not
 *    healthy caps the root at degraded; the banner names the dependency
 *    and the signal it fails on, never just "degraded".
 */

import {
    BoundaryMetrics,
    DependencyCap,
    DependencyHealthEntry,
    DriftEvent,
    FallbackCheck,
    SignalBasis,
    dependencyMetricTarget,
    failingSignalLabel,
} from './aiSystemTypes';
import { aiSystemLink, assumptionFacts, taskStateMix } from './composition';
import { getString } from './narrow';
import { ago } from './relativeTime';

/** The evidence a system's signals rest on, in one sentence. `servedCalls`
 *  is the report's `service.served_calls` when known. */
export function signalBasisSentence(
    basis: SignalBasis | null | undefined,
    servedCalls?: number | null,
): string | null {
    if (basis !== 'segments') return null;
    const n =
        typeof servedCalls === 'number'
            ? `${servedCalls} served call${servedCalls === 1 ? '' : 's'}`
            : 'the calls it served';
    return (
        `Assured from ${n} as a shared service: it roots no runs of its own, ` +
        `so every signal is computed from what it did for its callers.`
    );
}

/** "Capped at degraded: critical dependency Scoring Service is not healthy
 *  (failing on liveness)." */
export function dependencyCapSentence(cap: DependencyCap): string {
    const name = cap.name ?? cap.system_id ?? 'a critical dependency';
    const signal = failingSignalLabel(cap.failing_signal);
    return (
        `Capped at degraded: critical dependency ${name} is not healthy` +
        `${signal ? ` (failing on ${signal})` : ''}. ` +
        `A system cannot be healthier than a dependency it cannot work without.`
    );
}

/** Where a dependency is worked: a shared service's own signals, or the
 *  agent-card registry for an external agent. */
export function dependencyLink(entry: Pick<DependencyHealthEntry, 'role' | 'system_id'>): string {
    return entry.role === 'shared_service' && entry.system_id
        ? aiSystemLink(entry.system_id, 'signals')
        : 'a2a-agent-cards';
}

/** The cap's link — the capping dependency's own signals. */
export function dependencyCapLink(cap: DependencyCap): string | null {
    return cap.system_id ? aiSystemLink(cap.system_id, 'signals') : null;
}

/** Did the declared fallback hold when the dependency failed? */
export function fallbackCheckText(check: FallbackCheck): string {
    if (check.held === null) return 'Fallback not exercised in the window.';
    if (check.held) {
        return (
            `Fallback held: ${check.unanswered_calls} unanswered call` +
            `${check.unanswered_calls === 1 ? '' : 's'}, every affected run still completed.`
        );
    }
    const failed = check.unanswered_calls - check.root_runs_completed;
    return (
        `Fallback did NOT hold: ${failed} of ${check.unanswered_calls} unanswered ` +
        `call${check.unanswered_calls === 1 ? '' : 's'} left the run incomplete.`
    );
}

/** "12 calls · 1 error (8%) · p95 340 ms · last seen 3h ago" */
export function boundaryText(b: BoundaryMetrics): string {
    if (b.calls === 0) return 'No calls through the gateway in the window.';
    const parts = [`${b.calls} call${b.calls === 1 ? '' : 's'}`];
    // The rates below grade only the judged calls; say so.
    if (b.excluded_calls) parts.push(`${b.excluded_calls} inside incident windows, not judged`);
    const rate = b.error_rate !== null ? ` (${Math.round(b.error_rate * 100)}%)` : '';
    parts.push(`${b.errors} error${b.errors === 1 ? '' : 's'}${rate}`);
    if (b.p95_latency_ms !== null) parts.push(`p95 ${b.p95_latency_ms} ms`);
    if (b.last_seen_at) parts.push(`last seen ${ago(b.last_seen_at)}`);
    // How the delegate's tasks ended, as the root saw it.
    const mix = taskStateMix(b.task_states, ', ');
    if (mix) parts.push(`task states: ${mix}`);
    return parts.join(' · ');
}

/** Declared assumptions as readable rows — the typed schema's wording
 *  (`assumptionFacts`), shared with the dialog and the report. */
export function assumptionRows(assumptions: Record<string, unknown> | null): [string, string][] {
    return assumptionFacts(assumptions);
}

/** "failing on liveness via Ledger Service" — or null when healthy. */
export function failingText(entry: DependencyHealthEntry): string | null {
    const signal = failingSignalLabel(entry.failing_signal);
    if (!signal) return null;
    const via = entry.via_system_id ? ` via ${entry.via_system_name ?? entry.via_system_id}` : '';
    return `failing on ${signal}${via}`;
}

/** The dependency a `dependency`-class drift finding on a ROOT names
 *  (`metric_key` = `dependency:<target>`): its system id — from the
 *  evidence, else the metric key — and its display name when recorded. */
export function driftDependency(event: DriftEvent): { id: string; name: string | null } | null {
    const target = dependencyMetricTarget(event.metric_key);
    if (event.drift_class !== 'dependency' || target === null) return null;
    const evidence = event.cause_evidence ?? {};
    return {
        id: getString(evidence, 'dependency_system_id') ?? target,
        name: getString(evidence, 'dependency_name') ?? null,
    };
}
