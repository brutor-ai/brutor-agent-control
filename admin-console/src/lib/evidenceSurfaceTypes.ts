/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Wire types and display vocabulary for the evidence
 * surfaces that live outside the Compliance console: run details, Settings →
 * Evidence, System → Audit Chains, the Assurance Inbox kinds and the blind
 * review queue.
 *
 * Response shapes come from the generated Control Plane types; the few
 * endpoints the CP relays from the core without a response model (bundle
 * build / verify) are narrowed here through `narrow.ts`, so a malformed
 * answer renders as a finding, never as a clean result.
 *
 * Words on screen: sealed / witnessed / timestamped / met / not met /
 * unverified — never the terms the vocabulary test bans.
 */

import type { ApiResponse } from './api/controlPlaneTypes';
import { normalizeVerifyResult, type EvidenceVerifyFinding } from './complianceEvidenceTypes';
import { getArray, getBoolean, getNumber, getRecord, getString, isRecord } from './narrow';

// ─── Wire types ──────────────────────────────────────────────────────────────

export type EvidenceSettings = ApiResponse<'/v1/admin/evidence/settings', 'get'>;
export type SigningKeyList = ApiResponse<'/v1/admin/evidence/keys', 'get'>;
export type SigningKey = SigningKeyList['items'][number];
export type KeyRotation = ApiResponse<'/v1/admin/evidence/keys/rotate', 'post'>;

export type CheckReviewList = ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews', 'get'>;
export type CheckReview = CheckReviewList['items'][number];
export type CheckAgreement = ApiResponse<'/v1/admin/assurance-checks/{check_id}/agreement', 'get'>;
export type CheckReviewGrade = ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews/{review_id}', 'post'>;
export type CheckReviewSample = ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews/sample', 'post'>;

export type ComplianceReportList = ApiResponse<'/v1/admin/compliance/reports', 'get'>;
export type ComplianceReportSummary = ComplianceReportList['items'][number];
export type EffectiveNoticeConfig = ApiResponse<'/v1/admin/compliance/notice-configs/effective', 'get'>;
export type DocumentationExportList = ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/documentation/exports', 'get'>;
export type DocumentationExportSummary = DocumentationExportList['items'][number];

export type HumanResult = 'met' | 'not_met' | 'n/a';
export const HUMAN_RESULTS: { value: HumanResult; label: string }[] = [
    { value: 'met', label: 'Met' },
    { value: 'not_met', label: 'Not met' },
    { value: 'n/a', label: 'Not applicable' },
];

// ─── Record verdicts (all 14) ──────────────────────────────────

export const ALL_RECORD_VERDICTS = [
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
export type AnyRecordVerdict = (typeof ALL_RECORD_VERDICTS)[number];

export const STEP_VERDICT_CHIP: Record<AnyRecordVerdict, string> = {
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
    assessment: 'bg-purple-100 text-purple-800',
    adjudication: 'bg-purple-100 text-purple-800',
    recorded: 'bg-surface-raised text-secondary',
    planned: 'bg-blue-100 text-blue-800',
};

export const VERDICT_LABEL: Record<AnyRecordVerdict, string> = {
    executed: 'executed',
    errored: 'errored',
    timeout: 'timeout',
    blocked: 'blocked',
    denied: 'denied',
    engine_failure: 'engine failure',
    awaiting_human: 'awaiting human',
    resolved: 'resolved',
    expired: 'expired',
    epoch_boundary: 'epoch boundary',
    assessment: 'assessment',
    adjudication: 'adjudication',
    recorded: 'recorded',
    planned: 'planned',
};

export function verdictLabel(v: string | null | undefined): string {
    return (VERDICT_LABEL as Record<string, string>)[v ?? ''] ?? (v || '—');
}

/** Derived effect mode. `null` = the body was pruned. */
export const EFFECT_MODE_LABEL: Record<string, string> = {
    not_applicable: 'not applicable',
    confirmed: 'confirmed',
    dispatched_unconfirmed: 'dispatched, unconfirmed',
};

export function effectModeLabel(mode: string | null | undefined): string {
    if (!mode) return 'unknown (body pruned)';
    return EFFECT_MODE_LABEL[mode] ?? mode;
}

// ─── Chain links ─────────────────────────────────────────────────────────────

export interface ChainLink {
    parentRecordId: string;
    relation: string;
}

export function readChain(chain: unknown): ChainLink | null {
    const parent = getString(chain, 'parent_record_id');
    const relation = getString(chain, 'relation');
    if (!parent || !relation) return null;
    return { parentRecordId: parent, relation };
}

/** How a step's chain link reads, from the child's side. */
export function describeChainLink(verdict: string, relation: string): string {
    switch (relation) {
        case 'confirms':
            return 'confirms the planned record';
        case 'supersedes':
            if (verdict === 'expired') return 'expires the approval request';
            if (verdict === 'resolved') return 'resolves the approval request';
            return 'supersedes the approval request';
        case 'epoch_opens':
            return 'opens an epoch after';
        case 'assesses':
            return 'assesses';
        case 'adjudicates':
            return 'adjudicates';
        default:
            return relation;
    }
}

/** A `planned` or `awaiting_human` record no later record in the run chains
 *  to — the visible may/did gap. */
export function openHalves(steps: { record_id: string; verdict: string; chain?: unknown }[]): Set<string> {
    const parents = new Set<string>();
    for (const s of steps) {
        const link = readChain(s.chain);
        if (link) parents.add(link.parentRecordId);
    }
    return new Set(
        steps.filter((s) => (s.verdict === 'planned' || s.verdict === 'awaiting_human') && !parents.has(s.record_id)).map((s) => s.record_id),
    );
}

// ─── Bundles (core-relayed, untyped on the CP) ───────────────────────────────

export interface BundleBuildResult {
    bundle_id: string | null;
    bundle_digest: string | null;
    record_id: string | null;
    missing: string[];
    record_count: number;
    bundle: Record<string, unknown> | null;
}

export function normalizeBundleBuild(raw: unknown): BundleBuildResult {
    const bundle = getRecord(raw, 'bundle') ?? null;
    const manifest = getRecord(bundle, 'manifest');
    const completeness = getRecord(manifest, 'completeness');
    const missing = (getArray(completeness, 'missing') ?? getArray(raw, 'missing') ?? []).filter(
        (m): m is string => typeof m === 'string',
    );
    return {
        bundle_id: getString(raw, 'bundle_id') ?? null,
        bundle_digest: getString(raw, 'bundle_digest') ?? null,
        record_id: getString(raw, 'record_id') ?? null,
        missing,
        record_count: (getArray(bundle, 'records') ?? []).length,
        bundle,
    };
}

export interface BundleVerifyResult {
    ok: boolean;
    findings: EvidenceVerifyFinding[];
    bundle_digest: string | null;
    digest_matches: boolean | null;
    records_verified: number | null;
    proofs_verified: number | null;
    heads_verified: number | null;
    missing: string[];
}

export function normalizeBundleVerify(raw: unknown): BundleVerifyResult {
    const base = normalizeVerifyResult(raw);
    const digestMatches = getBoolean(raw, 'digest_matches');
    return {
        // A digest that does not match is never ok, whatever the core said.
        ok: base.ok && digestMatches !== false && isRecord(raw),
        findings: base.findings,
        bundle_digest: getString(raw, 'bundle_digest') ?? null,
        digest_matches: digestMatches ?? null,
        records_verified: getNumber(raw, 'records_verified') ?? null,
        proofs_verified: getNumber(raw, 'proofs_verified') ?? null,
        heads_verified: getNumber(raw, 'heads_verified') ?? null,
        missing: (getArray(raw, 'missing') ?? []).filter((m): m is string => typeof m === 'string'),
    };
}

// ─── Witness pins ────────────────────────────────────────────────────────────

const HEX64 = /^[0-9a-fA-F]{64}$/;
const PEM_CERT = /^-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*$/;
/** Matches the CP bound (`witnesses.pinned_key` is TEXT). */
export const PINNED_KEY_MAX_LEN = 16384;

/** `null` when valid. An empty pin is allowed (receipts then grade
 *  `unverified`) — the form says so rather than refusing. */
export function validateWitnessPin(kind: string, pin: string): string | null {
    const value = pin.trim();
    if (!value) return null;
    if (value.length > PINNED_KEY_MAX_LEN) return `The pin is longer than ${PINNED_KEY_MAX_LEN} characters.`;
    if (kind === 'scitt') {
        return HEX64.test(value) ? null : 'A scitt pin is the witness’s Ed25519 public key: 64 hex characters (32 bytes).';
    }
    if (kind === 'rfc3161') {
        if (HEX64.test(value) || PEM_CERT.test(value)) return null;
        return 'An rfc3161 pin is the SHA-256 of the TSA certificate (DER), as 64 hex characters, or the certificate as PEM.';
    }
    return 'Unknown witness kind.';
}

/** Hex pins are stored lowercase; a PEM is kept verbatim. */
export function normalizeWitnessPin(pin: string): string | null {
    const value = pin.trim();
    if (!value) return null;
    return HEX64.test(value) ? value.toLowerCase() : value;
}

export function validateWitnessUrl(url: string): string | null {
    const value = url.trim();
    if (!value) return 'URL is required.';
    return /^https?:\/\/\S+$/i.test(value) ? null : 'The URL must start with http:// or https://.';
}

// ─── Head grades ─────────────────────────────────────────────────────────────

/** Heads unwitnessed longer than this are a finding. */
export const UNWITNESSED_FINDING_MS = 60 * 60 * 1000;

/** A head graded standalone / self-witnessed for over an hour. A finding to
 *  look at, not a verdict about the records under it. */
export function isUnwitnessedFinding(grade: string | null | undefined, issuedAt: string | null | undefined, nowMs = Date.now()): boolean {
    if (grade !== 'standalone' && grade !== 'self_witnessed') return false;
    if (!issuedAt) return false;
    const t = Date.parse(issuedAt);
    return !Number.isNaN(t) && nowMs - t > UNWITNESSED_FINDING_MS;
}

// ─── Error shapes ────────────────────────────────────────────────────────────

/** The `{detail: {error: code}}` code a CP pass-through error carries. */
export function errorCode(e: unknown): string | undefined {
    const details = getRecord(e, 'details');
    return getString(getRecord(details, 'detail'), 'error') ?? getString(details, 'error');
}
