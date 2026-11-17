/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Regulatory obligation types. Mirrors
 * `app/api/v1/endpoints/admin/ai_obligations.py`.
 *
 * The invariant the shapes encode: applicability is DATED
 * (`applies_from` + `in_force`), never a compliance boolean, and an
 * undeclared fact yields `undetermined` rather than a guess.
 */

export type ObligationStatus = 'applies' | 'not_applicable' | 'undetermined';

export interface ObligationFrameworkInfo {
    id: string;
    title: string;
    jurisdiction: string;
    reference: string;
    obligation_count: number;
}

/**
 * An evidence statement an obligation is shown against.
 * Declared only in this phase — the statement names what would be
 * recomputed, judged, read as an indicator or taken as declared; no
 * status travels with it yet.
 */
export interface ObligationEvidenceSpec {
    key: string;
    statement: string;
    basis: string; // recomputed | judged | indicator | declared
    resolver: string;
    params?: Record<string, unknown>;
    limit?: string | null;
}

export interface ObligationResult {
    key: string;
    title: string;
    reference: string;
    summary: string;
    applies_from: string;
    in_force: boolean;
    status: ObligationStatus;
    reason: string;
    needs_review: boolean;
    review_note?: string | null;
    /** Optional on the wire: a board without evidence statements carries none. */
    evidence?: ObligationEvidenceSpec[];
}

export interface BoardSystemRef {
    id: string;
    name: string;
    /** The DECLARED tier. */
    eu_ai_act_risk_tier?: string | null;
    eu_ai_act_role?: string | null;
    /** The tier the registry evaluated the system at: its
     *  effective tier under a composite-classification framework (a
     *  contributing caller lifted it), else the declared tier. */
    risk_tier_evaluated?: string | null;
    status: ObligationStatus;
    reason: string;
}

export interface BoardObligation extends ObligationResult {
    systems: BoardSystemRef[];
    undetermined_count: number;
}

export interface ObligationsBoardResponse {
    framework: ObligationFrameworkInfo;
    as_of: string;
    system_count: number;
    obligations: BoardObligation[];
}

export interface SystemObligationsResponse {
    framework: ObligationFrameworkInfo;
    system_id: string;
    system_name: string;
    /** The DECLARED tier. */
    eu_ai_act_risk_tier?: string | null;
    eu_ai_act_role?: string | null;
    /** The tier the obligations below were evaluated at. */
    risk_tier_evaluated?: string | null;
    /** The systems whose contributing dependency lifted the tier. */
    effective_tier_sources?: { system_id: string; system_name?: string | null; tier?: string | null }[];
    as_of: string;
    obligations: ObligationResult[];
}

/** Display labels for the regulated roles. */
export const EU_AI_ACT_ROLE_LABEL: Record<string, string> = {
    provider: 'Provider',
    deployer: 'Deployer',
    provider_and_deployer: 'Provider & Deployer',
    importer: 'Importer',
    distributor: 'Distributor',
    product_manufacturer: 'Product manufacturer',
    authorised_representative: 'Authorised representative',
};

/**
 * The dated split every obligation surface renders: in force today first,
 * then upcoming ordered by the date each starts to apply. Shared so the
 * Overview timeline, the Obligations matrix and the AI System panel cannot
 * disagree about which side of the line an obligation sits on.
 */
export function splitObligations<T extends Pick<ObligationResult, 'in_force' | 'applies_from'>>(
    obligations: T[],
): { inForce: T[]; upcoming: T[] } {
    const inForce = obligations.filter((o) => o.in_force);
    const upcoming = obligations
        .filter((o) => !o.in_force)
        .sort((a, b) => a.applies_from.localeCompare(b.applies_from));
    return { inForce, upcoming };
}
