/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance evidence programme — wire types and display vocabulary.
 *
 * Every response shape here is read straight out of the generated Control
 * Plane types, so a rename on the server fails the build rather than a
 * screen. The unions and chip maps below are the DISPLAY vocabulary, and
 * it is fixed: grades never go up, and the words on screen are sealed /
 * witnessed / timestamped / recomputed / judged / met / not met / not
 * evaluable / withheld / declared — never the ones the vocabulary lint bans.
 */

import type { ApiRequestBody, ApiResponse } from './api/controlPlaneTypes';
import { getArray, getBoolean, getRecord, getString, isRecord } from './narrow';

// ─── Evidence ledger ─────────────────────────────────────────────────────────

export type EvidenceRecordListResponse = ApiResponse<'/v1/admin/evidence/records', 'get'>;
export type EvidenceRecordSummary = EvidenceRecordListResponse['items'][number];
export type EvidenceRecordDetail = ApiResponse<'/v1/admin/evidence/records/{record_id}', 'get'>;
export type EvidenceInclusionProof = ApiResponse<'/v1/admin/evidence/records/{record_id}/proof', 'get'>;
export type EvidenceSummary = ApiResponse<'/v1/admin/evidence/summary', 'get'>;
export type EvidenceLogListResponse = ApiResponse<'/v1/admin/evidence/logs', 'get'>;
export type EvidenceLogItem = EvidenceLogListResponse['items'][number];
export type EvidenceTreeHeadListResponse = ApiResponse<'/v1/admin/evidence/logs/{log_id}/tree-heads', 'get'>;
export type EvidenceTreeHead = EvidenceTreeHeadListResponse['items'][number];
export type EvidenceReceipt = NonNullable<EvidenceTreeHead['receipts']>[number];
export type EvidenceWitnessListResponse = ApiResponse<'/v1/admin/evidence/witnesses', 'get'>;
export type EvidenceWitness = EvidenceWitnessListResponse['items'][number];
export type EvidenceDisclosureListResponse = ApiResponse<'/v1/admin/evidence/disclosures', 'get'>;
export type EvidenceDisclosure = EvidenceDisclosureListResponse['items'][number];
export type BundleBuildRequest = ApiRequestBody<'/v1/admin/evidence/bundles', 'post'>;

/** `POST /evidence/verify` — the core's answer, relayed by the CP. The CP
 *  declares no response model for it (the core owns the shape), so this is
 *  the one hand-written type in the module; every field is read through
 *  `narrow.ts` at the boundary. */
export interface EvidenceVerifyFinding {
    check: string;
    code: string;
    severity: 'error' | 'warning' | 'info';
    detail: string;
}

export interface EvidenceVerifyResult {
    ok: boolean;
    findings: EvidenceVerifyFinding[];
    record_id: string | null;
    effect_mode?: string | null;
    statement?: {
        verified?: boolean;
        iss?: string;
        sub?: string;
        kid?: string;
        error?: string;
    } | null;
}

export interface EvidenceRecordFilters {
    ai_system_id?: string;
    verdict?: string;
    kind?: string;
    surface?: string;
    root_task_id?: string;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
}

export interface EvidenceSummaryFilters {
    since?: string;
    until?: string;
    ai_system_id?: string;
}

// ─── Vocabularies (mirror app/database/models/{action_record,evidence_log,witness}.py)

/** The 14 record verdicts, pinned beside
 *  `app/database/models/action_record.py::RECORD_VERDICTS` and the core's
 *  `brutor_evidence::record::verdict::ALL`. Kept as a literal tuple so the
 *  union below is derived from it (one list, not two). */
export const RECORD_VERDICTS = [
    'executed',
    'errored',
    'timeout',
    'blocked',
    'denied',
    'engine_failure',
    'awaiting_human',
    'resolved',
    'expired',
    'epoch_boundary',
    'assessment',
    'adjudication',
    'recorded',
    'planned',
] as const;

export type RecordVerdict = (typeof RECORD_VERDICTS)[number];

/** Verdicts that never dispatch an effect: every verdict but
 *  executed / errored / timeout. */
export const NON_DISPATCHING_VERDICTS: ReadonlySet<RecordVerdict> = new Set(
    RECORD_VERDICTS.filter((v) => v !== 'executed' && v !== 'errored' && v !== 'timeout'),
);

export const RECORD_KINDS = ['decide', 'fyi'] as const;
export type RecordKind = (typeof RECORD_KINDS)[number];

/** Chain relations between records. */
export const RECORD_RELATIONS = ['confirms', 'supersedes', 'epoch_opens', 'assesses', 'adjudicates'] as const;
export type RecordRelation = (typeof RECORD_RELATIONS)[number];

/** Kinds of external evidence pointer (`ai_system_evidence.kind`), read by
 *  `evidence.pointer_on_file{kind}` statements. Mirrors
 *  `app/database/models/ai_system_evidence.py::EVIDENCE_KINDS`. */
export const EVIDENCE_KINDS = [
    'impact_assessment',
    'eval_report',
    'red_team_report',
    'pen_test_report',
    'risk_assessment',
    'data_governance',
    'dpia',
    // The written agreement with a dependency's
    // supplier (EU AI Act Art 25(4)); names one dependency edge and is
    // pinned by hash (sha256 required).
    'supplier_information',
    'other',
] as const;
export type EvidencePointerKind = (typeof EVIDENCE_KINDS)[number];
export const EVIDENCE_KIND_LABEL: Record<EvidencePointerKind, string> = {
    impact_assessment: 'Impact assessment',
    eval_report: 'Evaluation report',
    red_team_report: 'Red-team report',
    pen_test_report: 'Penetration-test report',
    risk_assessment: 'Risk assessment',
    data_governance: 'Data governance',
    dpia: 'Data protection impact assessment (DPIA)',
    supplier_information: 'Supplier information (Art 25(4))',
    other: 'Other',
};

export type HeadGrade = 'standalone' | 'witnessed' | 'plural' | 'self_witnessed';
/**
 * `skipped` is no receipt: the core records it, with why, when witness
 * registration is switched off (`BRUTOR_EVIDENCE_WITNESS_ENABLED=false`).
 * It never raises a grade; the head stays unwitnessed.
 */
export type ReceiptVerdict = 'witnessed' | 'timestamped' | 'unverified' | 'invalid' | 'skipped';
export type WitnessKind = 'scitt' | 'rfc3161';
export type WitnessScope = 'platform' | 'tenant';
export const WITNESS_KINDS: WitnessKind[] = ['scitt', 'rfc3161'];

/** Evidence statement basis. */
export type EvidenceBasis = 'recomputed' | 'judged' | 'indicator' | 'declared';

// ─── Chips ───────────────────────────────────────────────────────────────────

export const VERDICT_CHIP: Record<RecordVerdict, string> = {
    executed: 'bg-green-100 text-green-800',
    errored: 'bg-red-100 text-red-800',
    timeout: 'bg-red-100 text-red-800',
    blocked: 'bg-amber-100 text-amber-800',
    denied: 'bg-amber-100 text-amber-800',
    engine_failure: 'bg-red-100 text-red-800',
    awaiting_human: 'bg-blue-100 text-blue-800',
    resolved: 'bg-green-100 text-green-800',
    expired: 'bg-surface-raised text-secondary',
    epoch_boundary: 'bg-surface-raised text-secondary',
    assessment: 'bg-blue-100 text-blue-800',
    adjudication: 'bg-blue-100 text-blue-800',
    recorded: 'bg-surface-raised text-secondary',
    planned: 'bg-surface-raised text-secondary',
};

export const HEAD_GRADE_CHIP: Record<HeadGrade, string> = {
    standalone: 'bg-surface-raised text-secondary',
    witnessed: 'bg-green-100 text-green-800',
    plural: 'bg-green-100 text-green-800',
    self_witnessed: 'bg-amber-100 text-amber-800',
};

export const HEAD_GRADE_LABEL: Record<HeadGrade, string> = {
    standalone: 'standalone',
    witnessed: 'witnessed',
    plural: 'plural',
    self_witnessed: 'self-witnessed',
};

export const RECEIPT_VERDICT_CHIP: Record<ReceiptVerdict, string> = {
    witnessed: 'bg-green-100 text-green-800',
    timestamped: 'bg-green-100 text-green-800',
    unverified: 'bg-amber-100 text-amber-800',
    invalid: 'bg-red-100 text-red-800',
    skipped: 'bg-surface-raised text-secondary',
};

const RECEIPT_VERDICT_LABEL: Partial<Record<ReceiptVerdict, string>> = {
    skipped: 'unwitnessed (witness switched off)',
};

/** What a receipt's verdict is called on screen: its own name, but for a `skipped` row, which says why the head is unwitnessed. */
export function receiptVerdictLabel(value: string | null | undefined): string {
    return (RECEIPT_VERDICT_LABEL as Record<string, string | undefined>)[value ?? ''] ?? (value || '—');
}

export const BASIS_CHIP: Record<EvidenceBasis, string> = {
    recomputed: 'bg-green-100 text-green-800',
    judged: 'bg-blue-100 text-blue-800',
    indicator: 'bg-amber-100 text-amber-800',
    declared: 'bg-surface-raised text-secondary',
};

export const VERIFY_SEVERITY_CHIP: Record<EvidenceVerifyFinding['severity'], string> = {
    error: 'bg-red-100 text-red-800',
    warning: 'bg-amber-100 text-amber-800',
    info: 'bg-surface-raised text-secondary',
};

/** Chip class for a value the server may send outside the pinned union. */
export function chipFor<K extends string>(map: Record<K, string>, value: string | null | undefined): string {
    return (map as Record<string, string>)[value ?? ''] ?? 'bg-surface-raised text-secondary';
}

export function headGradeLabel(value: string | null | undefined): string {
    return (HEAD_GRADE_LABEL as Record<string, string>)[value ?? ''] ?? (value || '—');
}

/** First 12 hex characters — enough to recognise a root or key id in a table. */
export function shortHash(value: string | null | undefined, length = 12): string {
    if (!value) return '—';
    return value.length > length ? `${value.slice(0, length)}…` : value;
}

// ─── Framework registry + profiles ───────────────────────────────────────────

export type ComplianceFrameworkListResponse = ApiResponse<'/v1/admin/compliance/frameworks', 'get'>;
export type ComplianceFramework = ComplianceFrameworkListResponse['items'][number];
export type AISystemComplianceProfile = ApiResponse<'/v1/admin/compliance/ai-systems/{ai_system_id}/profile', 'get'>;

export type OperatorRole = 'builder' | 'operator' | 'vendor_of' | 'reseller';
export const OPERATOR_ROLES: OperatorRole[] = ['builder', 'operator', 'vendor_of', 'reseller'];
export const OPERATOR_ROLE_LABEL: Record<OperatorRole, string> = {
    builder: 'Builder',
    operator: 'Operator',
    vendor_of: 'Vendor of',
    reseller: 'Reseller',
};

export type CoreBooleanField =
    | 'interacts_with_natural_persons'
    | 'generates_synthetic_content'
    | 'automated_decisions_about_persons'
    | 'two_phase_actions'
    | 'inference_records'
    | 'salt_digests';

export const CORE_BOOLEAN_FIELDS: { key: CoreBooleanField; label: string; help: string }[] = [
    { key: 'interacts_with_natural_persons', label: 'Interacts with natural persons', help: 'People talk to it directly (chat, voice, forms).' },
    { key: 'generates_synthetic_content', label: 'Generates synthetic content', help: 'Produces text, images, audio or video presented as content.' },
    { key: 'automated_decisions_about_persons', label: 'Automated decisions about persons', help: 'Its output decides or materially shapes an outcome for a person.' },
    { key: 'two_phase_actions', label: 'Two-phase actions', help: 'Consequential actions are proposed, then executed after a gate.' },
    { key: 'inference_records', label: 'Inference records', help: 'Keeps a record per inference beyond the action record.' },
    { key: 'salt_digests', label: 'Salt digests', help: 'Content digests in records are salted so they cannot be brute-forced from short inputs.' },
];

export type SensitivityFlag = 'personal_data' | 'special_category' | 'phi' | 'financial' | 'minors';
export const SENSITIVITY_FLAGS: { key: SensitivityFlag; label: string }[] = [
    { key: 'personal_data', label: 'Personal data' },
    { key: 'special_category', label: 'Special-category data' },
    { key: 'phi', label: 'Protected health information' },
    { key: 'financial', label: 'Financial data' },
    { key: 'minors', label: 'Data about minors' },
];

export type ProfileDateField = 'developed' | 'placed_on_market' | 'put_into_service';
export const PROFILE_DATE_FIELDS: { key: ProfileDateField; label: string }[] = [
    { key: 'developed', label: 'Developed' },
    { key: 'placed_on_market', label: 'Placed on market' },
    { key: 'put_into_service', label: 'Put into service' },
];

export type ProfileEntityField = 'provider' | 'deployer' | 'representative';
export const PROFILE_ENTITY_FIELDS: { key: ProfileEntityField; label: string }[] = [
    { key: 'provider', label: 'Provider' },
    { key: 'deployer', label: 'Deployer' },
    { key: 'representative', label: 'Authorised representative' },
];

export interface ExternalRegistration {
    framework_id: string;
    registry: string;
    id: string;
}

/** The neutral core of a profile as the editor holds it. Every field is
 *  optional on the wire; the editor fills defaults for rendering only and
 *  strips empties before it saves, so an untouched profile stays `{}`. */
export interface ProfileCoreForm {
    operator_role: OperatorRole | '';
    jurisdictions: string[];
    entities: Partial<Record<ProfileEntityField, string>>;
    dates: Partial<Record<ProfileDateField, string>>;
    external_registrations: ExternalRegistration[];
    sensitivity: Partial<Record<SensitivityFlag, boolean>>;
    flags: Partial<Record<CoreBooleanField, boolean>>;
    /** Who exercises human oversight (declared; read by the `oversight.*` statements). */
    oversight_assignment: string;
    /** The content-marking approach, declared with its limit. */
    synthetic_marking: string;
    notes: string;
}

export const EU_RISK_TIERS = ['minimal', 'limited', 'high', 'unacceptable'] as const;
export const EU_ANNEXES = ['none', 'annex_i', 'annex_iii'] as const;
export const EU_ANNEX_III_AREAS = ['1', '2', '3', '4', '5', '6', '7', '8'] as const;
export const EU_ANNEX_III_AREA_LABEL: Record<(typeof EU_ANNEX_III_AREAS)[number], string> = {
    '1': '1 — Biometrics',
    '2': '2 — Critical infrastructure',
    '3': '3 — Education and vocational training',
    '4': '4 — Employment and workers management',
    '5': '5 — Access to essential services',
    '6': '6 — Law enforcement',
    '7': '7 — Migration, asylum and border control',
    '8': '8 — Administration of justice and democratic processes',
};
export const EU_CONFORMITY_ASSESSMENTS = ['internal', 'notified_body'] as const;

// ─── Boundary narrowing ──────────────────────────────────────────────────────

/** The verify endpoint relays the core's JSON as-is. Read it through the
 *  narrowing helpers so a malformed answer renders as findings, not as a
 *  crash — and never as ok. */
export function normalizeVerifyResult(raw: unknown): EvidenceVerifyResult {
    const findings: EvidenceVerifyFinding[] = [];
    for (const item of getArray(raw, 'findings') ?? []) {
        const severity = getString(item, 'severity');
        findings.push({
            check: getString(item, 'check') ?? 'unknown',
            code: getString(item, 'code') ?? 'unknown',
            severity: severity === 'error' || severity === 'warning' || severity === 'info' ? severity : 'error',
            detail: getString(item, 'detail') ?? '',
        });
    }
    const statementRaw = getRecord(raw, 'statement');
    const statement = statementRaw
        ? {
              verified: getBoolean(statementRaw, 'verified'),
              iss: getString(statementRaw, 'iss'),
              sub: getString(statementRaw, 'sub'),
              kid: getString(statementRaw, 'kid'),
              error: getString(statementRaw, 'error'),
          }
        : null;
    if (!isRecord(raw)) {
        findings.push({
            check: 'response',
            code: 'malformed_response',
            severity: 'error',
            detail: 'The verifier returned a non-object response.',
        });
    }
    return {
        ok: getBoolean(raw, 'ok') === true && !findings.some((f) => f.severity === 'error'),
        findings,
        record_id: getString(raw, 'record_id') ?? null,
        effect_mode: getString(raw, 'effect_mode') ?? null,
        statement,
    };
}

// ─── Compliance console wire types ─────────────
//
// Read out of the generated Control Plane types — a rename on the server
// fails the build. Free-form blocks the CP declares as `Dict[str, Any]`
// (`latest_report`, `facts`, `other_frameworks`, oversight `systems`) stay
// `unknown` and are read through `narrow.ts` where they are rendered.

export type ComplianceOverviewResponse = ApiResponse<'/v1/admin/compliance/overview', 'get'>;
export type ComplianceOverviewSystem = ComplianceOverviewResponse['systems'][number];
export type ComplianceFrameworkSummary = ComplianceOverviewResponse['framework'];
export type ComplianceObligationsResponse = ApiResponse<'/v1/admin/compliance/obligations', 'get'>;
export type ComplianceBoardObligation = ComplianceObligationsResponse['obligations'][number];
export type ComplianceMatrix = NonNullable<ComplianceObligationsResponse['matrix']>;
export type ComplianceMatrixSystem = ComplianceMatrix['systems'][number];
export type ComplianceMatrixCell = ComplianceMatrixSystem['cells'][number];
export type ObligationDetailResponse = ApiResponse<'/v1/admin/compliance/obligations/{key}', 'get'>;
export type ObligationDetailSystem = ObligationDetailResponse['systems'][number];
export type ObligationEvidenceDetail = ObligationDetailSystem['obligations'][number];
export type EvidenceStatement = NonNullable<ObligationEvidenceDetail['statements']>[number];
export type ComplianceSystemsResponse = ApiResponse<'/v1/admin/compliance/systems', 'get'>;
export type ComplianceSystemRow = ComplianceSystemsResponse['items'][number];
export type OversightSummary = ApiResponse<'/v1/admin/compliance/oversight', 'get'>;
export type TransparencySummary = ApiResponse<'/v1/admin/compliance/transparency', 'get'>;
export type TransparencyStatement = NonNullable<TransparencySummary['notice_evidence']>;
export type NoticeConfigListResponse = ApiResponse<'/v1/admin/compliance/notice-configs', 'get'>;
export type NoticeConfig = NoticeConfigListResponse['items'][number];
export type NoticeConfigPutRequest = ApiRequestBody<'/v1/admin/compliance/notice-configs', 'put'>;
export type NoticeConfigSaveResponse = ApiResponse<'/v1/admin/compliance/notice-configs', 'put'>;
export type NoticeConfigDeleteResponse = ApiResponse<'/v1/admin/compliance/notice-configs/{config_id}', 'delete'>;
export type EffectiveNoticeConfig = ApiResponse<'/v1/admin/compliance/notice-configs/effective', 'get'>;
export type NoticeExemption = NonNullable<NoticeConfig['exemption']>;
export type SealStatus = NoticeConfigSaveResponse['seal'];
export type ComplianceReportListResponse = ApiResponse<'/v1/admin/compliance/reports', 'get'>;
export type ComplianceReportSummary = ComplianceReportListResponse['items'][number];
export type ComplianceReport = ApiResponse<'/v1/admin/compliance/reports/{report_id}', 'get'>;
export type ReportGenerateRequest = ApiRequestBody<'/v1/admin/compliance/reports', 'post'>;
export type ReportGenerateResponse = ApiResponse<'/v1/admin/compliance/reports', 'post'>;
export type IncidentListResponse = ApiResponse<'/v1/admin/compliance/incidents', 'get'>;
export type ComplianceIncident = IncidentListResponse['items'][number];
export type IncidentDeadline = NonNullable<ComplianceIncident['deadline_status']>[number];
export type IncidentDetail = ApiResponse<'/v1/admin/compliance/incidents/{incident_id}', 'get'>;
export type IncidentEvent = NonNullable<IncidentDetail['events']>[number];
export type IncidentCreateRequest = ApiRequestBody<'/v1/admin/compliance/incidents', 'post'>;
export type IncidentUpdateRequest = ApiRequestBody<'/v1/admin/compliance/incidents/{incident_id}', 'patch'>;
export type IncidentReportRequest = ApiRequestBody<'/v1/admin/compliance/incidents/{incident_id}/report', 'post'>;
export type IncidentSubmissionRequest = ApiRequestBody<'/v1/admin/compliance/incidents/{incident_id}/submissions', 'post'>;
export type IncidentChangeResponse = ApiResponse<'/v1/admin/compliance/incidents', 'post'>;
export type DocumentationExport = ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/documentation', 'get'>;
export type DocumentationExportListResponse = ApiResponse<
    '/v1/admin/ai-systems/{ai_system_id}/documentation/exports',
    'get'
>;
export type DocumentationExportSummary = DocumentationExportListResponse['items'][number];

/** Statement statuses the resolvers return. `not_covered`
 *  is the report's row for an obligation with no statement at all. */
export const STATEMENT_STATUSES = [
    'met',
    'not_met',
    'not_evaluable',
    'undetermined',
    'indicator',
    'declared',
    'not_covered',
] as const;
export type StatementStatus = (typeof STATEMENT_STATUSES)[number];

export const EVIDENCE_BASES: EvidenceBasis[] = ['recomputed', 'judged', 'indicator', 'declared'];

/** Incident deadline statuses (`app/services/compliance/incidents.py`). */
export const DEADLINE_STATUSES = [
    'open',
    'due_soon',
    'overdue',
    'reported_on_time',
    'reported_late',
    'closed',
    // Staged regimes (DORA, NIS2): the clock starts at the previous stage's
    // submission; closing never satisfies an unsubmitted stage.
    'waiting',
    'closed_unsubmitted',
] as const;
export type DeadlineStatus = (typeof DEADLINE_STATUSES)[number];

export const INCIDENT_STATUSES = ['open', 'reported', 'closed'] as const;
export const INCIDENT_SEVERITIES = ['low', 'medium', 'high', 'critical'] as const;
/** Suggestions only — the classification is free text; a framework's
 *  deadline rule applies when the classification names it. */
export const INCIDENT_CLASSIFICATION_SUGGESTIONS = [
    'serious_incident',
    'death',
    'widespread',
    'major_incident',
    'breach',
    'security_incident',
];

export const REPORT_CADENCES = ['daily', 'weekly', 'monthly'] as const;
export type ReportCadence = (typeof REPORT_CADENCES)[number];

/** Notice surfaces (`compliance_notice_configs.surface`). */
export const NOTICE_SURFACES = ['banner', 'inline', 'both'] as const;
export type NoticeSurface = (typeof NOTICE_SURFACES)[number];

/** Exemption bases (`app/services/compliance/notices.py::EXEMPTION_BASES`). */
export const NOTICE_EXEMPTION_BASES = ['obvious_from_context', 'authorised_by_law', 'no_natural_person', 'other'] as const;
export const NOTICE_EXEMPTION_BASIS_LABEL: Record<(typeof NOTICE_EXEMPTION_BASES)[number], string> = {
    obvious_from_context: 'Obvious from the context',
    authorised_by_law: 'Authorised by law',
    no_natural_person: 'No natural person interacts with it',
    other: 'Other',
};

/** The eight locales the Enterprise User Portal is translated into. */
export const PORTAL_LOCALES = [
    { code: 'en', label: 'English' },
    { code: 'de', label: 'Deutsch' },
    { code: 'fr', label: 'Français' },
    { code: 'it', label: 'Italiano' },
    { code: 'es', label: 'Español' },
    { code: 'pt', label: 'Português' },
    { code: 'zh', label: '中文' },
    { code: 'ja', label: '日本語' },
] as const;

/**
 * Where a framework keeps the system's role / classification inside the
 * profile's `frameworks.<id>` block. Mirrors the catalogs' `role_field` /
 * `classification_field` (`app/services/compliance/catalogs/*.py`); the
 * registry API does not expose them, so the defaults and the two
 * overrides are pinned here and the CP still validates every save.
 */
export const FRAMEWORK_ROLE_FIELD: Record<string, string> = {
    dora: 'financial_entity_type',
    nis2: 'entity_type',
};
export function roleFieldFor(frameworkId: string): string {
    return FRAMEWORK_ROLE_FIELD[frameworkId] ?? 'role';
}
export function classificationFieldFor(_frameworkId: string): string {
    return 'risk_tier';
}

/** True when a verify answer says the verifier itself could not run — a
 *  state of the tool, never a result about the evidence. */
export function isVerifierUnavailable(result: Pick<EvidenceVerifyResult, 'findings'>): boolean {
    return result.findings.some((f) => f.code === 'verifier_unavailable');
}
