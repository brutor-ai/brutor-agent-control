/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The words for the assurance report's "Composition and
 * dependencies" section (spec 1.6). Pure, so the report panel and the
 * printed report say exactly the same thing and the tests pin it.
 *
 * The section is frozen inside the report (its digest and signature cover
 * it); the report schema makes the `composition` key optional, and
 * `readCompositionSection` returns null for a report without it rather
 * than guessing.
 */

import {
    AI_SYSTEM_ROLE_LABELS,
    AISystemRole,
    CompositionAgent,
    CompositionReportEdge,
    CompositionReportSection,
    DEPENDENCY_CRITICALITY_LABELS,
    DEPENDENCY_HEALTH_LABELS,
    DEPENDENCY_ROLE_LABELS,
    SupplierInformationBlock,
    failingSignalLabel,
} from './aiSystemTypes';
import { APPROVAL_LABELS, TRUST_LABELS, approvalState, releaseLongLabel } from './agentRelease';
import { effectiveTierText, separabilityText } from './composition';
import { isRecord } from './narrow';

/** The section from a report payload, or null when the report has none. */
export function readCompositionSection(report: unknown): CompositionReportSection | null {
    if (!isRecord(report)) return null;
    const section = report.composition;
    if (!isRecord(section) || !Array.isArray(section.edges)) return null;
    // The report schema (spec 1.6) is the contract; the shape is trusted
    // once the discriminating keys are present.
    return section as unknown as CompositionReportSection;
}

export function reportEdgeTarget(e: CompositionReportEdge): string {
    return (
        e.depends_on_system_name ??
        e.agent_card_name ??
        e.depends_on_system_id ??
        e.agent_card_id ??
        e.edge_id
    );
}

/** "External · Degradable · contributes to decision" */
export function reportEdgeTerms(e: CompositionReportEdge): string {
    const contribution = e.contributes_to_decision
        ? 'contributes to decision'
        : separabilityText(e.separability?.record_id, e.separability?.sealed_at);
    return [
        DEPENDENCY_ROLE_LABELS[e.role] ?? e.role,
        DEPENDENCY_CRITICALITY_LABELS[e.criticality] ?? e.criticality,
        contribution,
    ].join(' · ');
}

/** Tested status for a shared service; never a tested status for an
 *  external agent — "assumed, not tested". */
export function reportEdgeStatus(e: CompositionReportEdge): string {
    if (e.assurance_basis === 'assumed') return 'assumed, not tested';
    const status = e.status ? (DEPENDENCY_HEALTH_LABELS[e.status] ?? e.status) : 'unknown';
    const failing = failingSignalLabel(e.failing_signal);
    return `tested · ${status}${failing ? ` (failing on ${failing})` : ''}${e.caps_root ? ' · caps this system' : ''}`;
}

/** "pinned v2 · 1a2b3c4d" / "follows v3" / "no contract (external)". */
export function reportEdgeContract(e: CompositionReportEdge): string {
    if (e.role === 'external') return 'no contract (external)';
    if (!e.contract) return 'target has no active contract';
    const hash = e.contract.hash ? ` · ${e.contract.hash.slice(0, 8)}` : '';
    return `${e.contract.pinned ? 'pinned' : 'follows'} v${e.contract.version}${hash}`;
}

/** Supplier and its Art 25(4) information: "Nordic Credit Bureau AB ·
 *  supplier information on file (1)", "… missing", or not required. */
export function supplierInformationText(
    entity: string | null | undefined,
    block: SupplierInformationBlock | null | undefined,
): string {
    const who = entity ? `${entity} · ` : '';
    if (!block || !block.required) return entity ? `${entity} · no supplier agreement required` : 'deployer (no supplier)';
    if (block.unexpired > 0) return `${who}supplier information on file (${block.unexpired})`;
    if (block.on_file > 0) return `${who}supplier information EXPIRED — renew it`;
    return `${who}supplier information MISSING`;
}

export function supplierInformationOk(block: SupplierInformationBlock | null | undefined): boolean {
    return !block || !block.required || block.unexpired > 0;
}

export function reportRoleText(role: string | null | undefined): string {
    if (!role) return '—';
    return AI_SYSTEM_ROLE_LABELS[role as AISystemRole] ?? role;
}

export function reportTierText(section: CompositionReportSection): string {
    return (
        effectiveTierText(
            section.risk_tier_declared,
            section.risk_tier_effective,
            section.effective_tier_sources,
        ) ?? 'not declared'
    );
}

/** "Brutor Demo System — accepted · 12 calls" / "… — NOT accepted (refused
 *  at the gateway) · 3 calls". */
export function reportCallerText(
    caller: CompositionReportSection['callers'][number],
    enforced: boolean,
): string {
    const name = caller.system_name ?? caller.system_id;
    const acceptance = caller.accepted
        ? 'accepted'
        : enforced
          ? 'NOT accepted (refused at the gateway)'
          : 'NOT accepted (advisory)';
    return `${name} — ${acceptance} · ${caller.observed_calls} call${caller.observed_calls === 1 ? '' : 's'}`;
}

/** Report spec 1.7 — a member agent's release line, the same on screen
 *  and in print: "loan-intake@1.4.2 (sha256:ab12…) · verified · approved",
 *  or "no release declared". */
export function reportAgentReleaseText(agent: CompositionAgent): string {
    const r = agent.release;
    if (!r) return 'no release declared';
    return [releaseLongLabel(r), TRUST_LABELS[r.trust] ?? r.trust, APPROVAL_LABELS[approvalState(r.approved)]].join(' · ');
}
