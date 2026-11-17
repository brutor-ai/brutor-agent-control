/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Compliance console's page catalogue.
 *
 * Lives beside ComplianceConsoleTab rather than inside it so the component
 * file exports only its component (react-refresh/only-export-components).
 * The catalogue is the single source of truth for:
 *   * the route id the sidebar dispatches on (`compliance-overview` …);
 *   * the label and icon shared by the sidebar leaf and the tab strip.
 *
 * Every page is live.
 */

import type React from 'react';
import {
    Eye,
    FileText,
    LayoutDashboard,
    Layers,
    Library,
    ListChecks,
    ScrollText,
    Siren,
    UserCheck,
} from 'lucide-react';

export type ComplianceSection =
    | 'overview'
    | 'frameworks'
    | 'profiles'
    | 'obligations'
    | 'evidence'
    | 'oversight'
    | 'transparency'
    | 'reports'
    | 'incidents';

export interface ComplianceSectionMeta {
    id: ComplianceSection;
    routeId: string;
    label: string;
    icon: React.FC<{ className?: string }>;
    /** One paragraph: what the page shows. */
    description: string;
}

export const COMPLIANCE_SECTIONS: ComplianceSectionMeta[] = [
    {
        id: 'overview',
        routeId: 'compliance-overview',
        label: 'Overview',
        icon: LayoutDashboard,
        description:
            'Per framework: sealed records and their verdicts, the tenant log and its latest head grade, which frameworks are enabled, and the dated timeline of obligations in force and upcoming.',
    },
    {
        id: 'frameworks',
        routeId: 'compliance-frameworks',
        label: 'Frameworks',
        icon: Library,
        description:
            'The registry: enable a framework for this tenant, see its catalog version, whether the catalog was checked against the published instrument, its retention floor and deadline rules. Request-log tagging and the auditor exports live here too.',
    },
    {
        id: 'profiles',
        routeId: 'compliance-profiles',
        label: 'Profiles',
        icon: Layers,
        description:
            'Compliance profile per AI System: the neutral core (who operates it, where, what it touches) plus the facts each framework needs. Each save is a declaration the ledger seals.',
    },
    {
        id: 'obligations',
        routeId: 'compliance-obligations',
        label: 'Obligations',
        icon: ListChecks,
        description:
            'The selected framework’s obligations, dated: for one AI System, or across all of them as a board or a system × obligation matrix. Each obligation lists the evidence statements it is shown against with their basis — recomputed, judged, indicator or declared.',
    },
    {
        id: 'evidence',
        routeId: 'compliance-evidence',
        label: 'Evidence Ledger',
        icon: ScrollText,
        description:
            'Record explorer: sealed records by system, verdict, kind, surface, window and run; the tenant log with its heads and receipts; proof and recomputation for any record; disclosure records.',
    },
    {
        id: 'oversight',
        routeId: 'compliance-oversight',
        label: 'Human Oversight',
        icon: UserCheck,
        description:
            'Oversight assignment per system; approvals requested, decided and lapsed; the blind review queue; agreement between judged rows and human grades per check, with movement indicators over time.',
    },
    {
        id: 'transparency',
        routeId: 'compliance-transparency',
        label: 'Transparency',
        icon: Eye,
        description:
            'AI-interaction notice configuration per system — text per locale, surface, exemption assertions with their rationale — with notice evidence as n of m conversations and synthetic-content declarations with their stated limits.',
    },
    {
        id: 'reports',
        routeId: 'compliance-reports',
        label: 'Reports',
        icon: FileText,
        description:
            'Daily rollups, weekly grades and monthly Evidence Reports per framework: generate, view, bundle and permalink each one, with its countersignature status and the catalog version it was computed under.',
    },
    {
        id: 'incidents',
        routeId: 'compliance-incidents',
        label: 'Incidents',
        icon: Siren,
        description:
            'The incident register with per-framework reporting deadlines, linked runs and inbox items, and a sealed history of every change.',
    },
];

export const COMPLIANCE_SECTION_BY_ROUTE: Record<string, ComplianceSectionMeta> = Object.fromEntries(
    COMPLIANCE_SECTIONS.map((s) => [s.routeId, s]),
);

/** Where the framework switcher remembers its selection. */
export const FRAMEWORK_STORAGE_KEY = 'brutor.compliance.framework';
export const DEFAULT_FRAMEWORK = 'eu-ai-act';

/** The page of a `compliance-*` route id; Overview for an id that names none. */
export function resolveComplianceRoute(activeTab: string): ComplianceSectionMeta {
    return COMPLIANCE_SECTION_BY_ROUTE[activeTab] ?? COMPLIANCE_SECTIONS[0];
}

export function readStoredFramework(): string | null {
    try {
        return window.localStorage.getItem(FRAMEWORK_STORAGE_KEY);
    } catch {
        return null;
    }
}

export function storeFramework(id: string): void {
    try {
        window.localStorage.setItem(FRAMEWORK_STORAGE_KEY, id);
    } catch {
        // Storage may be unavailable (private mode, quota); the in-memory
        // selection still works for the session.
    }
}

/** Props every console page receives. */
export interface CompliancePageProps {
    /** The registry id of the framework the switcher has selected. */
    framework: string;
    /** The OFFERED frameworks — verified against their published text — as
     *  loaded by the console shell (may be empty while loading). Pickers and
     *  pages that evaluate, report or export use these only. */
    frameworks: import('../../lib/complianceEvidenceTypes').ComplianceFramework[];
    /** The whole registry, including frameworks not offered (with the reason);
     *  for the Frameworks page, which explains them. */
    registry: import('../../lib/complianceEvidenceTypes').ComplianceFramework[];
    /** Re-read the registry after a page changes it (enable / disable). */
    refreshFrameworks: () => void;
    /** Move within the console (`compliance-frameworks` …). */
    onTabChange: (routeId: string) => void;
    /** Move anywhere in the console app (`ai-systems:{id}` …). */
    setActiveTab?: (tab: string) => void;
    /** An obligation the Obligations page should open on arrival (set by a
     *  cross-framework link). */
    focusObligation?: string | null;
    /** Switch framework and open one of its obligations ("show this
     *  evidence under other frameworks"). */
    openObligation?: (frameworkId: string, obligationKey: string) => void;
}
