/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tier → model mapping for the `complexity-based-routing` strategy.
 *
 * Shown only when that strategy is selected. The panel deliberately leads
 * with an explanation of what the strategy *does*, because "complexity
 * routing" means nothing on its own and an operator mapping models to tiers
 * needs to know what decides a tier and what happens when it guesses wrong.
 */

import React from 'react';
import { Gauge, Info, ShieldCheck } from 'lucide-react';

import {
    COMPLEXITY_TIERS,
    ComplexityTier,
    RoutingGroupTierAssignment,
} from '../../../lib/llmTypes';

/** What each tier means, in the operator's terms rather than the scorer's. */
const TIER_COPY: Record<
    ComplexityTier,
    { label: string; blurb: string; tone: string }
> = {
    simple: {
        label: 'Simple',
        blurb: 'Greetings, definitions, short factual questions.',
        tone: 'bg-emerald-50 border-emerald-200 text-emerald-900',
    },
    medium: {
        label: 'Medium',
        blurb: 'Ordinary questions with a bit of context behind them.',
        tone: 'bg-sky-50 border-sky-200 text-sky-900',
    },
    complex: {
        label: 'Complex',
        blurb: 'Code, technical terms, multi-part or long requests.',
        tone: 'bg-amber-50 border-amber-200 text-amber-900',
    },
    reasoning: {
        label: 'Reasoning',
        blurb: 'Explicit "think it through", "explain why", "compare".',
        tone: 'bg-purple-50 border-purple-200 text-purple-900',
    },
};

interface ModelOption {
    id: string;
    name: string;
}

interface ComplexityTierEditorProps {
    /** Models that are members of this routing group — the only legal targets. */
    groupModels: ModelOption[];
    /** Current mapping; one entry per tier at most in this simple editor. */
    value: RoutingGroupTierAssignment[];
    onChange: (next: RoutingGroupTierAssignment[]) => void;
    /** Governance floor: never route below this tier. */
    floorTier: ComplexityTier | null;
    onFloorTierChange: (tier: ComplexityTier | null) => void;
    disabled?: boolean;
}

export const ComplexityTierEditor: React.FC<ComplexityTierEditorProps> = ({
    groupModels,
    value,
    onChange,
    floorTier,
    onFloorTierChange,
    disabled = false,
}) => {
    const modelForTier = (tier: ComplexityTier): string =>
        value.find((v) => v.tier === tier)?.llm_model_id ?? '';

    const setModelForTier = (tier: ComplexityTier, modelId: string) => {
        const rest = value.filter((v) => v.tier !== tier);
        onChange(
            modelId ? [...rest, { tier, llm_model_id: modelId, position: 0 }] : rest
        );
    };

    const mappedCount = value.length;

    return (
        <div className="space-y-4" data-testid="complexity-tier-editor">
            {/* ── What this strategy does ─────────────────────────────── */}
            <div className="flex items-start gap-2.5 p-3 bg-surface-alt border border-default rounded-lg">
                <Gauge className="w-4 h-4 text-primary-600 shrink-0 mt-0.5" />
                <div className="text-sm text-secondary space-y-1.5">
                    <p>
                        <strong className="text-default">How it works.</strong> Every request
                        is scored locally against the prompt — its length, whether it contains
                        code or technical terms, whether it asks for reasoning, how many tools
                        it offers. The score picks a tier, and the tier picks the model you
                        map below. There is <strong>no extra model call</strong>: scoring takes
                        microseconds and costs nothing.
                    </p>
                    <p>
                        <strong className="text-default">It is deterministic.</strong> The same
                        prompt always produces the same tier, and the request log records which
                        signals fired — so a routing decision can be explained after the fact
                        rather than guessed at.
                    </p>
                    <p className="text-muted">
                        The scorer reads the latest user message (plus the system prompt for
                        code and technical signals only). Keyword lists are English; treat this
                        as a cost optimization, never as a security or policy control.
                    </p>
                </div>
            </div>

            {/* ── Floor tier: the quality control ─────────────────────── */}
            <div className="p-3 border border-default rounded-lg">
                <div className="flex items-start gap-2.5">
                    <ShieldCheck className="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <label
                            htmlFor="complexity-floor-tier"
                            className="block text-sm font-medium text-default"
                        >
                            Floor tier
                        </label>
                        <p className="text-xs text-secondary mt-0.5 mb-2">
                            Never route below this tier, whatever a prompt scores. Use it when
                            a weak answer costs more than a big model does — the guarantee is
                            that traffic in this group can only ever move <em>up</em> from here.
                        </p>
                        <select
                            id="complexity-floor-tier"
                            data-testid="complexity-floor-tier"
                            disabled={disabled}
                            value={floorTier ?? ''}
                            onChange={(e) =>
                                onFloorTierChange(
                                    e.target.value ? (e.target.value as ComplexityTier) : null
                                )
                            }
                            className="w-full max-w-xs px-3 py-2 text-sm border border-default rounded-lg bg-surface disabled:opacity-50"
                        >
                            <option value="">No floor — allow the cheapest tier</option>
                            {COMPLEXITY_TIERS.map((t) => (
                                <option key={t} value={t}>
                                    {TIER_COPY[t].label} and above
                                </option>
                            ))}
                        </select>
                    </div>
                </div>
            </div>

            {/* ── The mapping ─────────────────────────────────────────── */}
            <div>
                <h4 className="text-sm font-semibold text-default mb-2">
                    Tier → model
                </h4>
                {groupModels.length === 0 ? (
                    <div className="flex items-start gap-2 p-3 text-sm bg-amber-50 border border-amber-200 rounded-lg text-amber-900">
                        <Info className="w-4 h-4 shrink-0 mt-0.5" />
                        <span>
                            Add models to this routing group first — a tier can only point at a
                            model the group already contains.
                        </span>
                    </div>
                ) : (
                    <div className="space-y-2">
                        {COMPLEXITY_TIERS.map((tier) => (
                            <div
                                key={tier}
                                className={`flex flex-wrap items-center gap-3 p-2.5 border rounded-lg ${TIER_COPY[tier].tone}`}
                            >
                                <div className="min-w-[9rem]">
                                    <div className="text-sm font-semibold">
                                        {TIER_COPY[tier].label}
                                    </div>
                                    <div className="text-[11px] opacity-80">
                                        {TIER_COPY[tier].blurb}
                                    </div>
                                </div>
                                <select
                                    aria-label={`Model for ${TIER_COPY[tier].label} tier`}
                                    data-testid={`tier-model-${tier}`}
                                    disabled={disabled}
                                    value={modelForTier(tier)}
                                    onChange={(e) => setModelForTier(tier, e.target.value)}
                                    className="flex-1 min-w-[12rem] px-3 py-1.5 text-sm border border-default rounded-lg bg-surface text-default disabled:opacity-50 min-h-0 min-w-0"
                                >
                                    <option value="">Not mapped</option>
                                    {groupModels.map((m) => (
                                        <option key={m.id} value={m.id}>
                                            {m.name}
                                        </option>
                                    ))}
                                </select>
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* ── What happens when it isn't fully configured ─────────── */}
            <div
                className="flex items-start gap-2 p-3 text-sm bg-surface-alt border border-default rounded-lg text-secondary"
                data-testid="complexity-fallback-note"
            >
                <Info className="w-4 h-4 shrink-0 mt-0.5 text-muted" />
                <span>
                    {mappedCount === 0 ? (
                        <>
                            <strong className="text-default">No tiers mapped yet.</strong> Until
                            at least one tier points at a model, this group keeps behaving
                            exactly as it does today — requests fall back to ordinary usage-based
                            balancing. Nothing changes until you map a tier.
                        </>
                    ) : (
                        <>
                            If a request scores into a tier with no model mapped — or the mapped
                            model is unhealthy, rate-limited or not permitted for the caller —
                            the group falls back to ordinary usage-based balancing rather than
                            failing the request.
                        </>
                    )}
                </span>
            </div>
        </div>
    );
};

export default ComplexityTierEditor;
