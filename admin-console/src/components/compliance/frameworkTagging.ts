/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Request-log tagging guide, keyed by the registry's `tag_key`.
 *
 * A framework that has a `tag_key` is one the core's tagging engine stamps
 * on request-log (proxy-log) rows while the framework is enabled. This is
 * the operator-facing explanation of what a tag on a row means and what an
 * auditor asks for. The tag names themselves are NOT listed here: the
 * dashboard endpoint returns every tag the engine emits with its count, so
 * the page shows the engine's own vocabulary rather than a copy of it.
 *
 * `csvExportRegime` is the regime token of the CSV export endpoint. Only
 * the two regimes whose evidence model is a per-call list have one (GDPR
 * Art. 30 record of processing, HIPAA accounting of disclosures); the
 * others audit a management system, not a row dump.
 */

export type CsvExportRegime = 'gdpr/article-30' | 'hipaa/phi-access';

export interface FrameworkTaggingGuide {
    whatItIs: string;
    whoNeedsIt: string;
    whatGetsTagged: string;
    auditEvidence: string;
    /** Set where a per-resource-group field drives the tag. */
    perGroupField?: string;
    csvExportRegime?: CsvExportRegime;
    csvExportNote?: string;
}

export const NO_CSV_EXPORT_NOTE =
    'No per-call CSV export: this framework audits a management system, not a row dump. ' +
    'The tagged-row counts support operating-effectiveness sampling; the auditor deliverable is your control dossier.';

export const TAGGING_SUBTITLE =
    'Tags are stamped on request-log (proxy-log) rows by the core as each call is written — the rows on the ' +
    'Proxy Logs page, not Admin Audit Logs. Frameworks compose: a row carries every tag that applies. ' +
    'Disabling a framework stops tagging; rows already tagged keep their tags.';

export const FRAMEWORK_TAGGING_GUIDE: Record<string, FrameworkTaggingGuide> = {
    gdpr_art_30: {
        whatItIs:
            'EU General Data Protection Regulation, Article 30. Requires a written Record of Processing Activities for every operation that processes personal data of an identifiable EU data subject.',
        whoNeedsIt:
            'Any controller or processor offering goods or services to EU residents — including non-EU SaaS vendors with EU customers.',
        whatGetsTagged:
            'Every request-log row where the PII detector flagged identifiable personal data on the wire, or where the call is routed through a resource group bound to an EU-resident end-user group.',
        auditEvidence:
            'Article 30 supervisory inspections want a row-per-processing-event list with timestamp, data category, purpose, subject identifier and processing region. The CSV export produces that shape.',
        csvExportRegime: 'gdpr/article-30',
        csvExportNote:
            'One row per detected processing event. Hand this to the DPO or supervisory authority for an Article 30 record.',
    },
    eu_ai_act: {
        whatItIs:
            'EU Artificial Intelligence Act (Regulation 2024/1689). Classifies AI systems into risk tiers with graduated obligations — transparency for limited risk, conformity assessment, risk management and human oversight for high risk, prohibition for unacceptable risk.',
        whoNeedsIt:
            'Providers and deployers of AI systems placed on the EU market or affecting people in the EU. Tier obligations attach to the role you play in the AI value chain.',
        whatGetsTagged:
            'Every request-log row from a resource group whose EU AI Act risk tier is set. The tier becomes the tag suffix, so an auditor can isolate every call that fell under a given tier.',
        auditEvidence:
            'EU AI Act auditors want the technical documentation package (Annex IV), conformity-assessment artefacts, post-market monitoring records and human-oversight evidence — not a per-call CSV. The tagged-row counts show post-market monitoring scope; the obligations themselves are evaluated on the Obligations page.',
        perGroupField:
            'The risk tier is set per resource group: Resource Groups → Governance → Compliance.',
    },
    soc2: {
        whatItIs:
            'AICPA SOC 2 (Trust Services Criteria). Independent attestation that controls supporting Security, Availability, Processing Integrity, Confidentiality and Privacy operate effectively over a period — typically 6 or 12 months.',
        whoNeedsIt:
            'B2B SaaS vendors whose enterprise customers contractually require a Type II report.',
        whatGetsTagged:
            'Request-log rows that exercise the Common Criteria control families the platform covers: CC6 logical access (authentication and authorisation events) and CC7 system monitoring (rate-limit and guardrail trips). Each tag carries the CC reference so a row maps directly to a control.',
        auditEvidence:
            'A SOC 2 audit walks each control. The tagged-row counts demonstrate operating-effectiveness sampling — "the CC7.2 monitoring control fired X times in the quarter" — but the deliverable is the control narrative, system description and management assertion, not a CSV.',
    },
    hipaa: {
        whatItIs:
            'US Health Insurance Portability and Accountability Act. Requires covered entities and business associates to safeguard Protected Health Information and account for every disclosure to a third party (45 CFR 164.528).',
        whoNeedsIt:
            'US healthcare providers, payers, clearinghouses, and any vendor that touches PHI on their behalf (business associates under a BAA).',
        whatGetsTagged:
            'Request-log rows where PHI was detected on the wire by the PII engine, or where the call routed through a resource group marked as a PHI resource. The group flag is operator attestation — use it when the resources in that group are known to handle PHI regardless of what the detector saw.',
        auditEvidence:
            'HIPAA auditors and OCR investigators expect an Accounting of Disclosures: a per-event list of who accessed which PHI, when, why, and to whom it was disclosed. The CSV export produces that record across the selected window.',
        perGroupField:
            'The PHI-resource flag is set per resource group: Resource Groups → Governance → Compliance.',
        csvExportRegime: 'hipaa/phi-access',
        csvExportNote:
            'One row per PHI access event. Suitable for 45 CFR 164.528 Accounting-of-Disclosures requests and BAA-driven evidence requests.',
    },
    iso_42001: {
        whatItIs:
            'ISO/IEC 42001:2023 — the international standard for AI Management Systems (AIMS). A Plan-Do-Check-Act framework for managing AI risk, lifecycle and stakeholder impact, structurally similar to ISO 27001 for security.',
        whoNeedsIt:
            'Any organisation seeking independent assurance that its AI use is governed end to end. Often paired with ISO 27001 in enterprise procurement.',
        whatGetsTagged:
            'Every request-log row from this tenant, by design. ISO 42001 is the activity-baseline tag — while the framework is enabled, the row count is the volume metric the AIMS asks you to track.',
        auditEvidence:
            'A 42001 audit reviews your AIMS documentation (policy, scope, risk register, lifecycle controls) and samples activity records. The total row count is the per-window activity metric for the AIMS performance evaluation step (clause 9).',
    },
};

/** One framework's tag counts as the dashboard endpoint returns them. */
export interface TaggingCard {
    framework: string;
    declared: boolean;
    total_tagged_rows: number;
    by_tag: Record<string, number>;
}

/** Cards indexed by tag key. */
export function cardsByTagKey(cards: TaggingCard[] | null | undefined): Record<string, TaggingCard> {
    const out: Record<string, TaggingCard> = {};
    for (const c of cards ?? []) out[c.framework] = c;
    return out;
}
