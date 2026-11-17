/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control card dashboard types.
 *
 * Mirrors `mission_control_cards.py` response models. Two rules travel
 * with the data and the UI must honour them:
 *
 *  - windows scale MEASUREMENTS (spend/runs/cpt/trend); health, verdict
 *    and Big-T class are pinned judgments and never move with the
 *    window selector;
 *  - group cards carry worst-of judgments over their subtree, flagged
 *    by `t_class_contains` so the chip renders "contains T(…)".
 */

export type CardWindow = '24h' | '7d' | '30d' | '90d' | 'all';
export type CardSort = 'attention' | 'spend' | 'cpt' | 'name';

/** One row of the Unattributed card's to-do list: the group its runs started in. */
export interface UnattributedGroup {
    /** null = the group the runs started in is not recorded. */
    group_id?: string | null;
    group_name?: string | null;
    runs: number;
    spend: number;
}

export interface MissionControlCard {
    id: string;
    name: string;
    group_type: string;
    /** Platform-provisioned group (Default, Unbound): labelled "system", not its group_type. */
    is_system?: boolean;
    /** One-line purpose of a system group, when it has one. */
    purpose?: string | null;
    system_kind?: string | null;
    lifecycle_stage?: string | null;
    eu_ai_act_risk_tier?: string | null;
    is_group: boolean;
    systems: number;
    parent_group_id?: string | null;

    health_state?: string | null;
    verdict_status?: string | null;
    t_class?: string | null;
    /** True on group cards: the class is the worst in the subtree. */
    t_class_contains: boolean;
    k_median?: number | null;
    a_max?: number | null;

    spend: number;
    /**
     * The part of `spend` that was imported rather than routed: costed in
     * the same ledger, but nothing was enforced on it. Equal to `spend`
     * when nothing on this card was governed.
     */
    observed_spend?: number;
    runs: number;
    completed_runs: number;
    cost_per_task?: number | null;
    cost_per_task_blended: boolean;
    cpt_bound?: number | null;
    cpt_breached: boolean;

    trend: number[];
    /** Native tokenomics unit — total tokens over the window. */
    tokens: number;
    /** Run-ledger payload mass (input+output bytes) over the window. */
    data_bytes: number;
    trend_tokens: number[];
    trend_bytes: number[];
    live: boolean;
    findings: number;
    governance_gaps: number;

    /** 'unattributed' for the one pseudo-card; absent on real groups. */
    pseudo?: 'unattributed' | null;
    note?: string | null;
    /** Unattributed pseudo-card only: its runs by starting group, runs desc. */
    unattributed_by_group?: UnattributedGroup[] | null;
    /** Attested exclusion from the worst-of rollup (the card still shows its own judgement). */
    excluded?: boolean;
    exclusion_reason?: string | null;
    exclusion_expires_at?: string | null;
    /** Group cards: systems in the subtree left out of the rollup. */
    excluded_systems?: number;
}

export interface MissionControlCardsResponse {
    cards: MissionControlCard[];
    total: number;
    window: string;
    parent_id?: string | null;
    /** Breadcrumb: ancestors of parent_id, root-first. */
    path: { id: string; name: string }[];
    judgments_pinned_note: string;
}

export interface MissionControlCardsParams {
    parent_id?: string;
    window?: CardWindow;
    q?: string;
    group_type?: string;
    sort?: CardSort;
    limit?: number;
    offset?: number;
}

/** A sub-section of an explainer: optional heading, paragraph, bullets. */
export interface CardExplainerSection {
    title?: string;
    body?: string;
    points?: string[];
}

/** One operator-facing concept explainer (served from the tokenomics
 *  module — one definition, every surface). The T-class explainer uses
 *  the richer shape (bullet points + sections); the rest are title+body. */
export interface CardExplainer {
    title: string;
    body: string;
    points?: string[];
    sections?: CardExplainerSection[];
}

/** The full Big-T guide, opened from the dashboard's "Workload classes"
 *  button. Info boxes stay terse and assume the operator has read it. */
export interface WorkloadGuide {
    title: string;
    sections: CardExplainerSection[];
}

export interface MissionControlExplainersResponse {
    explainers: Record<string, CardExplainer>;
    guide?: WorkloadGuide;
}
