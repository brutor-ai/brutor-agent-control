/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The assurance report's "Composition and dependencies" section (spec 1.6):
 * each declared edge with its role, current status (the
 * SAME dependency-health entries the report's health block carries),
 * evidence basis, contract version, supplier and Art 25(4) supplier
 * information, the separability claims on either side, the callers and
 * whether they are enforced, and the open composition findings.
 *
 * The section is frozen in the report; this renders what was sealed, it
 * does not recompute. Wording is shared with the printed report
 * (`lib/compositionReport.ts`).
 */

import { AlertTriangle } from 'lucide-react';

import type { CompositionReportSection as Section } from '../../lib/aiSystemTypes';
import { acceptedCallersText, findingLabel, separabilityDeclarationText, sortFindings } from '../../lib/composition';
import {
    reportAgentReleaseText,
    reportCallerText,
    reportEdgeContract,
    reportEdgeStatus,
    reportEdgeTarget,
    reportEdgeTerms,
    reportRoleText,
    reportTierText,
    supplierInformationOk,
    supplierInformationText,
} from '../../lib/compositionReport';
import { boundaryText } from '../../lib/dependencyHealth';
import { agentFindingLabel } from '../../lib/agentRelease';
import { AgentReleaseChip, NoRelease } from '../agent/AgentReleaseBadges';
import { HelpTip } from '../ui/HelpTip';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';

export function CompositionReportSection({ section }: { section: Section }) {
    const findings = sortFindings(section.findings);
    return (
        <div className="bg-surface border border-default rounded-lg p-4 space-y-3" data-testid="report-composition">
            <div className="flex items-center gap-2">
                <h4 className="text-sm font-semibold text-default">{section.title || 'Composition and dependencies'}</h4>
                <HelpTip
                    title="Composition and dependencies"
                    text="Each declared dependency as the report sealed it: its role, current status (tested for a shared service, assumed for an external agent), contract, supplier and supplier agreement, plus each member agent's release (with its trust and approval), the separability claims, the callers and the open composition findings."
                />
            </div>
            <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs" data-testid="report-composition-facts">
                <div className="flex gap-1.5">
                    <dt className="text-muted">Role</dt>
                    <dd className="text-default">{reportRoleText(section.role)}</dd>
                </div>
                <div className="flex gap-1.5">
                    <dt className="text-muted">Risk tier</dt>
                    <dd className="text-default" data-testid="report-composition-tier">
                        {reportTierText(section)}
                    </dd>
                </div>
                <div className="flex gap-1.5">
                    <dt className="text-muted">Callers</dt>
                    <dd className="text-default" data-testid="report-composition-enforcement">
                        {acceptedCallersText(section.enforce_accepted_callers)}
                    </dd>
                </div>
            </dl>

            {section.edges.length === 0 ? (
                <p className="text-xs italic text-muted" data-testid="report-composition-no-edges">
                    No dependencies declared.
                </p>
            ) : (
                <div className={TABLE_CONTAINER}>
                    <table className="min-w-full text-xs">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={TABLE_TH}>Dependency</th>
                                <th className={TABLE_TH}>Status</th>
                                <th className={TABLE_TH}>Contract</th>
                                <th className={TABLE_TH}>Supplier</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {section.edges.map((e) => (
                                <tr key={e.edge_id} data-testid={`report-edge-${e.edge_id}`}>
                                    <td className="px-4 py-2 align-top">
                                        <span className="block font-medium text-default">{reportEdgeTarget(e)}</span>
                                        <span className="block text-muted">{reportEdgeTerms(e)}</span>
                                        {e.separability?.rationale && (
                                            <span className="block text-secondary">Separability: {e.separability.rationale}</span>
                                        )}
                                    </td>
                                    <td className="px-4 py-2 align-top text-secondary" data-testid={`report-edge-status-${e.edge_id}`}>
                                        {reportEdgeStatus(e)}
                                        {e.boundary && (
                                            <span className="block text-muted">At the boundary: {boundaryText(e.boundary)}</span>
                                        )}
                                    </td>
                                    <td className="px-4 py-2 align-top text-secondary">{reportEdgeContract(e)}</td>
                                    <td
                                        className={`px-4 py-2 align-top ${
                                            supplierInformationOk(e.supplier_information) ? 'text-secondary' : 'font-medium text-amber-800'
                                        }`}
                                        data-testid={`report-edge-supplier-${e.edge_id}`}
                                    >
                                        {supplierInformationText(e.supplier_legal_entity, e.supplier_information)}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {/* Shown where the section names its agents. */}
            {section.agents && section.agents.length > 0 && (
                <div data-testid="report-composition-agents">
                    <h5 className="text-xs font-semibold text-default mb-1">Member agents and their releases</h5>
                    <ul className="space-y-1 text-xs">
                        {section.agents.map((a) => (
                            <li
                                key={a.id}
                                className="flex flex-wrap items-center gap-1.5 text-secondary"
                                data-testid={`report-agent-${a.id}`}
                                aria-label={`${a.name ?? a.id}: ${reportAgentReleaseText(a)}`}
                            >
                                <span className="font-medium text-default">{a.name ?? a.id}</span>
                                {a.release ? (
                                    <AgentReleaseChip release={a.release} approved={a.release.approved} />
                                ) : (
                                    <NoRelease />
                                )}
                            </li>
                        ))}
                    </ul>
                    {section.agent_findings && section.agent_findings.length > 0 && (
                        <ul className="mt-1 space-y-1 text-xs" data-testid="report-agent-findings">
                            {section.agent_findings.map((f, i) => (
                                <li key={`${f.code}-${f.agent_id}-${i}`} className="flex items-start gap-1 text-amber-800">
                                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                                    <span>
                                        <strong>{agentFindingLabel(f.code)}:</strong> {f.message ?? f.agent_name ?? f.agent_id}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}

            {section.separability_declarations.length > 0 && (
                <div data-testid="report-composition-separability">
                    <h5 className="text-xs font-semibold text-default mb-1">Separability declarations</h5>
                    <ul className="space-y-0.5 text-xs">
                        {section.separability_declarations.map((d) => (
                            <li key={d.edge_id} className="text-secondary">
                                {separabilityDeclarationText(d)} —{' '}
                                {d.sealed && d.record_id ? (
                                    <>
                                        sealed as <code className="font-mono">{d.record_id}</code>
                                    </>
                                ) : (
                                    <span className="text-amber-800">not sealed</span>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {section.callers.length > 0 && (
                <div data-testid="report-composition-callers">
                    <h5 className="text-xs font-semibold text-default mb-1">Callers</h5>
                    <ul className="space-y-0.5 text-xs text-secondary">
                        {section.callers.map((c) => (
                            <li key={c.system_id}>{reportCallerText(c, section.enforce_accepted_callers)}</li>
                        ))}
                    </ul>
                </div>
            )}

            <div data-testid="report-composition-findings">
                <h5 className="text-xs font-semibold text-default mb-1">Open composition findings</h5>
                {findings.length === 0 ? (
                    <p className="text-xs text-muted">None.</p>
                ) : (
                    <ul className="space-y-1 text-xs">
                        {findings.map((f, i) => (
                            <li key={`${f.code}-${f.subject_id ?? i}`} className="flex items-start gap-1 text-amber-800">
                                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                                <span>
                                    <strong>{findingLabel(f.code)}:</strong> {f.message}
                                </span>
                            </li>
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}

export default CompositionReportSection;
