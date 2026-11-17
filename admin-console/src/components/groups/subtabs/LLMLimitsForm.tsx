/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Shared LLM usage-limits form. Used by:
 *   - GroupLLMLimitsTab — group-wide LLM limits, no "global ceiling"
 *     reference (this IS the ceiling).
 *   - LLMModelLimitsDialog — per-model overrides, with `globalLimits`
 *     surfaced so operators see Global / Effective per field.
 *
 * Five sections, matching the enterprise-governance grid in the Rust
 * enforcement layer (`usage.rs::enforce_llm_rate_limits` +
 * `book_llm_post_response`):
 *
 *   1. Throughput   — RPM / ITPM / OTPM / $-per-min + burst + cooldown.
 *   2. Concurrency  — max in-flight.
 *   3. Budget       — daily/monthly USD + warnings + hard stop.
 *   4. Tokens       — daily/monthly combined + input/output split.
 *   5. Requests     — daily/monthly request count + warnings.
 *
 * Each section header carries an inline Info popover (matching the
 * MCPToolLimitsDialog pattern) with title /
 * algorithm / when-to-use / example sourced from the Rust enforcement
 * semantics so the copy and the code stay in lockstep.
 */

import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { DollarSign, Hash, Info, TrendingUp, Users, Zap } from 'lucide-react';
import { LLMGlobalLimits, LimitCapacityInfo } from '../../../lib/groupTypes';
import { InheritedLimitField } from '../limits/InheritedLimitField';
import { fieldCapacity } from '../../../lib/limitCapacity';

/** What scope this form is editing — affects copy and effective-value display. */
type Mode = 'group' | 'model';

interface Props {
    limits: LLMGlobalLimits;
    /** Set on the per-model variant: the effective group-level limits, used
     *  to display `Global: X / Effective: min(global, per-model)`. */
    globalLimits?: LLMGlobalLimits;
    /** Ceiling merged from every ancestor. Shown per field, and enforced
     *  by the API on PATCH — a value above it is refused with a 400. */
    inheritedLimits?: LLMGlobalLimits;
    /** This group's limits after the restrictive merge with its ancestors. */
    effectiveLimits?: LLMGlobalLimits;
    /** What the shared budget still has unclaimed, per field. */
    capacity?: LimitCapacityInfo | null;
    /** Root groups inherit nothing; keeps the badges off. */
    hasParent?: boolean;
    onChange: (next: LLMGlobalLimits) => void;
    mode: Mode;
    disabled?: boolean;
}

// ============================================================================
// Per-section help copy — keyed to the Rust enforcement layer.
// ============================================================================

interface FieldHelp {
    title: string;
    algorithm: string;
    whenToUse: string;
    example: string;
}

const SECTION_HELP: Record<string, FieldHelp> = {
    throughput: {
        title: 'Throughput — short-window rate ceilings',
        algorithm:
            'Token-bucket enforcement on requests-per-minute, input-tokens-per-minute, output-tokens-per-minute, and USD-per-minute. ITPM/OTPM/$ are charged POST-response from the upstream\'s actual billed token counts and cost — no estimation error. All four buckets are checked on every call; the strictest applicable bucket trips first. cooldown_seconds is a penalty lockout armed after any bucket exhaustion.',
        whenToUse:
            'Always set RPM as the floor of protection against runaway agents. ITPM binds for RAG / long-context workloads (one big prompt can blow the day\'s budget). OTPM binds for long-form generation and agent loops. $/min is the absolute cost ceiling; the daily $ budget is the cumulative one.',
        example:
            'RPM 60 + Burst 10 → 10 quick calls then 1/sec. ITPM 100k + tokens_burst 200k → can spike one 200k-token call then refills at ~1.6k tokens/sec. Cooldown 60 → after any trip, the next 60s reject all calls.',
    },
    concurrency: {
        title: 'Concurrency — simultaneous in-flight cap',
        algorithm:
            'Semaphore-style counter, distinct from rate. A slot is taken before the request and always released when it ends — including upstream errors, guardrail blocks and client disconnects. Exact across gateway replicas via Redis; counted per replica while Redis is down.',
        whenToUse:
            'One slow upstream call can pin shared resources without ever tripping RPM. Cap concurrency separately so a per-model fault (e.g. Anthropic latency spike) cannot exhaust group-wide pools. Most operators set the model cap stricter than the group cap.',
        example:
            'max_concurrent 8 → the 9th simultaneous request gets 429 immediately, even if RPM is far below the limit. Slot is released the instant the response completes (or errors out).',
    },
    budget: {
        title: 'Budget — cumulative USD spend',
        algorithm:
            'Fixed-window quotas on cost (charged from billed tokens × price). Daily window resets at midnight UTC; monthly at the 1st. Warning % fires an alert (no enforcement). A configured budget always blocks once it is reached.',
        whenToUse:
            'Predictable cost ceilings for finance reviews. Pair with $/min in Throughput: $/day is the absolute cap, $/min is the burn-rate cap. Leave the budget unset where blocking is worse than overspend, and watch the warning alerts instead.',
        example:
            '$200/day with 80% daily warning → alert at $160 spend, hard block at $200. Combined with $/min 0.50 → no single hour can drain more than $30, smoothing the spend curve.',
    },
    tokens: {
        title: 'Tokens — cumulative token quotas',
        algorithm:
            'Daily/monthly fixed-window quotas. Total = prompt + completion. Input-only and output-only siblings let operators cap the two halves independently — useful when one half is the binding constraint. The gateway enforces every one that is set: the two totals and the four input and output quotas (daily and monthly). A call is refused once any of them is reached.',
        whenToUse:
            'Total caps are the simplest cost-proxy. Input-only is the right cap for RAG / long-context workloads where the prompt dominates cost. Output-only is the right cap for long-form generation. Set both when you want fine-grained budgets per workload pattern.',
        example:
            'daily_limit 10M + daily_input_limit 8M + daily_output_limit 2M → a workload that submits 8M prompt tokens still has 2M output budget; switching to output-heavy generation maxes at 2M.',
    },
    requests: {
        title: 'Requests — cumulative request-count quotas',
        algorithm:
            'Daily/monthly fixed-window quotas on raw request count. Each accepted call increments by 1. Independent of token / cost quotas — useful for "fair share" caps that don\'t care about request size.',
        whenToUse:
            'Useful as a coarse fair-use ceiling per resource group, or as a circuit-breaker for noisy clients. Less useful than token / USD quotas for cost control because LLM calls are 1000× more heterogeneous in cost than REST calls. Set in addition to, not instead of, the token + USD caps.',
        example:
            '5000 requests/day + 80% warning → alert at 4000 requests, hard block at 5000.',
    },
};

// ============================================================================
// Helpers
// ============================================================================

function effectiveValue(
    global: number | undefined,
    perItem: number | undefined,
): string {
    if (global !== undefined && perItem !== undefined) return String(Math.min(global, perItem));
    if (perItem !== undefined) return String(perItem);
    if (global !== undefined) return String(global);
    return 'No limit';
}

function getNum(section: any, field: string): number | undefined {
    if (!section) return undefined;
    const v = section[field];
    return typeof v === 'number' ? v : undefined;
}

// ============================================================================
// Inline help popover — matches the MCPToolLimitsDialog pattern
// ============================================================================

interface HeaderHelpProps {
    sectionKey: keyof typeof SECTION_HELP;
    openKey: string | null;
    onToggle: (k: string | null) => void;
    Icon: React.ComponentType<{ className?: string }>;
    label: string;
}

const HeaderHelp: React.FC<HeaderHelpProps> = ({
    sectionKey,
    openKey,
    onToggle,
    Icon,
    label,
}) => {
    const open = openKey === sectionKey;
    const help = SECTION_HELP[sectionKey];
    const popoverRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!open) return;
        const onDocClick = (e: MouseEvent) => {
            if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
                onToggle(null);
            }
        };
        const onEsc = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onToggle(null);
        };
        document.addEventListener('mousedown', onDocClick);
        document.addEventListener('keydown', onEsc);
        return () => {
            document.removeEventListener('mousedown', onDocClick);
            document.removeEventListener('keydown', onEsc);
        };
    }, [open, onToggle]);

    return (
        <div className="relative inline-flex items-center gap-2">
            <Icon className="w-4 h-4 text-muted" />
            <h3 className="font-medium text-default">{label}</h3>
            <button
                type="button"
                onClick={() => onToggle(open ? null : sectionKey)}
                className={`text-muted hover:text-primary transition-colors ${
                    open ? 'text-primary' : ''
                }`}
                aria-expanded={open}
                aria-controls={`help-${sectionKey}`}
                title={`What does ${label} mean?`}
            >
                <Info className="w-4 h-4" />
            </button>
            {open && (
                <div
                    id={`help-${sectionKey}`}
                    ref={popoverRef}
                    role="dialog"
                    aria-label={help.title}
                    className="absolute z-20 left-0 top-full mt-1 w-96 p-3 bg-surface border border-default rounded-lg shadow-lg normal-case"
                >
                    <div className="text-sm font-semibold text-default mb-2">{help.title}</div>
                    <div className="text-xs text-secondary mb-2">
                        <span className="font-medium text-default">Algorithm: </span>
                        {help.algorithm}
                    </div>
                    <div className="text-xs text-secondary mb-2">
                        <span className="font-medium text-default">When to use: </span>
                        {help.whenToUse}
                    </div>
                    <div className="text-xs text-secondary">
                        <span className="font-medium text-default">Example: </span>
                        {help.example}
                    </div>
                </div>
            )}
        </div>
    );
};

// ============================================================================
// Field — one labelled number input + effective-value display
// ============================================================================

/**
 * Everything a field needs that does not vary field to field. Passed by
 * context so the 25 call sites below read as the grid they render — a
 * label and the limit it edits, nothing more.
 */
interface LimitsCtxValue {
    limits: LLMGlobalLimits;
    globalLimits?: LLMGlobalLimits;
    inheritedLimits?: LLMGlobalLimits;
    effectiveLimits?: LLMGlobalLimits;
    capacity?: LimitCapacityInfo | null;
    hasParent: boolean;
    mode: Mode;
    disabled?: boolean;
    update: (section: keyof LLMGlobalLimits, field: string, value: number | undefined) => void;
    openPopover: string | null;
    setOpenPopover: (key: string | null) => void;
}

const LimitsCtx = createContext<LimitsCtxValue | null>(null);

interface FieldProps {
    label: string;
    section: keyof LLMGlobalLimits;
    field: string;
    suffix?: string;
    step?: string;
}

/**
 * One limit. On the group form it carries the inherited ceiling and the
 * unclaimed capacity (shared with the MCP and Skill forms); on the
 * per-model dialog it carries the group-wide value it is narrowing.
 */
const Field: React.FC<FieldProps> = ({ label, section, field, suffix, step }) => {
    const ctx = useContext(LimitsCtx);
    if (!ctx) return null;
    const {
        limits, globalLimits, inheritedLimits, effectiveLimits, capacity,
        hasParent, mode, disabled, update, openPopover, setOpenPopover,
    } = ctx;

    const value = getNum(limits[section], field);
    const fieldKey = `${section}.${field}`;

    if (mode === 'model') {
        const globalValue = getNum(globalLimits?.[section], field);
        return (
            <div>
                <label className="block text-xs text-muted mb-1">
                    {label}
                    {suffix ? <span className="text-muted ml-1">({suffix})</span> : null}
                </label>
                <input
                    type="number"
                    step={step ?? '1'}
                    min={0}
                    value={value ?? ''}
                    onChange={(e) => {
                        const raw = e.target.value;
                        update(section, field, raw === '' ? undefined : Number(raw));
                    }}
                    disabled={disabled}
                    placeholder={globalValue !== undefined ? `Global: ${globalValue}` : 'No limit'}
                    data-testid={`limit-input-${fieldKey}`}
                    className="w-full px-2 py-1.5 text-sm border border-strong rounded bg-surface text-default disabled:opacity-50"
                />
                <span className="text-xs text-muted mt-0.5 block">
                    Effective: {effectiveValue(globalValue, value)}
                    {suffix ?? ''}
                </span>
            </div>
        );
    }

    return (
        <InheritedLimitField
            label={label}
            suffixNote={suffix}
            value={value}
            onChange={(v) => update(section, field, v)}
            inherited={getNum(inheritedLimits?.[section], field)}
            effective={getNum(effectiveLimits?.[section], field)}
            capacity={fieldCapacity(capacity, section, field)}
            hasParent={hasParent}
            step={step}
            disabled={disabled}
            fieldKey={fieldKey}
            popoverOpen={openPopover === fieldKey}
            onTogglePopover={setOpenPopover}
            dense
        />
    );
};

// ============================================================================
// Main form
// ============================================================================

const LLMLimitsForm: React.FC<Props> = ({
    limits,
    globalLimits,
    inheritedLimits,
    effectiveLimits,
    capacity,
    hasParent = true,
    onChange,
    mode,
    disabled,
}) => {
    const [openHelp, setOpenHelp] = useState<string | null>(null);
    const [openPopover, setOpenPopover] = useState<string | null>(null);

    /** Functional update helper that preserves section structure. */
    const update = <K extends keyof LLMGlobalLimits>(
        section: K,
        field: string,
        value: number | boolean | undefined,
    ) => {
        const next: LLMGlobalLimits = { ...limits };
        const currentSection: any = { ...(next[section] ?? {}) };
        if (value === undefined || (typeof value === 'number' && Number.isNaN(value))) {
            delete currentSection[field];
        } else {
            currentSection[field] = value;
        }
        if (Object.keys(currentSection).length === 0) {
            delete (next as any)[section];
        } else {
            (next as any)[section] = currentSection;
        }
        onChange(next);
    };

    const ctx: LimitsCtxValue = {
        limits, globalLimits, inheritedLimits, effectiveLimits, capacity,
        hasParent, mode, disabled, update, openPopover, setOpenPopover,
    };

    return (
        <LimitsCtx.Provider value={ctx}>
        <div className="space-y-4">
            {/* SECTION 1 — Throughput */}
            <section className="bg-surface border border-default rounded-lg p-6">
                <div className="mb-3">
                    <HeaderHelp
                        sectionKey="throughput"
                        openKey={openHelp}
                        onToggle={setOpenHelp}
                        Icon={Zap}
                        label="Throughput"
                    />
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <Field label="Requests / min" section="throughput" field="max_requests_per_minute" />
                    <Field label="Input tokens / min" section="throughput" field="max_input_tokens_per_minute" />
                    <Field label="Output tokens / min" section="throughput" field="max_output_tokens_per_minute" />
                    <Field label="USD / min" section="throughput" field="max_usd_per_minute" step="0.01" />
                    <Field label="Request burst" section="throughput" field="request_burst" />
                    <Field label="Token burst" section="throughput" field="tokens_burst" />
                    <Field label="USD burst" section="throughput" field="usd_burst" step="0.01" />
                    <Field label="Cooldown s" section="throughput" field="cooldown_seconds" />
                </div>
            </section>

            {/* SECTION 2 — Concurrency */}
            <section className="bg-surface border border-default rounded-lg p-6">
                <div className="mb-3">
                    <HeaderHelp
                        sectionKey="concurrency"
                        openKey={openHelp}
                        onToggle={setOpenHelp}
                        Icon={Users}
                        label="Concurrency"
                    />
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <Field label="Max concurrent" section="concurrency" field="max_concurrent" />
                </div>
            </section>

            {/* SECTION 3 — Budget */}
            <section className="bg-surface border border-default rounded-lg p-6">
                <div className="mb-3">
                    <HeaderHelp
                        sectionKey="budget"
                        openKey={openHelp}
                        onToggle={setOpenHelp}
                        Icon={DollarSign}
                        label="Budget"
                    />
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <Field label="Daily Limit" section="budget" field="daily_limit_usd" suffix="USD" step="0.01" />
                    <Field label="Monthly Limit" section="budget" field="monthly_limit_usd" suffix="USD" step="0.01" />
                    <Field label="Daily Warning" section="budget" field="daily_warning_percent" suffix="%" />
                    <Field label="Monthly Warning" section="budget" field="monthly_warning_percent" suffix="%" />
                </div>
                <p className="mt-3 text-xs text-muted">
                    A configured dollar budget always blocks at 100% — the same
                    as the token and request caps. The warning percentages above
                    control when advance-warning alerts fire before that.
                </p>
            </section>

            {/* SECTION 4 — Tokens */}
            <section className="bg-surface border border-default rounded-lg p-6">
                <div className="mb-3">
                    <HeaderHelp
                        sectionKey="tokens"
                        openKey={openHelp}
                        onToggle={setOpenHelp}
                        Icon={Hash}
                        label="Tokens"
                    />
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <Field label="Daily total" section="tokens" field="daily_limit" />
                    <Field label="Monthly total" section="tokens" field="monthly_limit" />
                    <Field label="Daily Warning" section="tokens" field="daily_warning_percent" suffix="%" />
                    <Field label="Monthly Warning" section="tokens" field="monthly_warning_percent" suffix="%" />
                    <Field label="Daily input" section="tokens" field="daily_input_limit" />
                    <Field label="Monthly input" section="tokens" field="monthly_input_limit" />
                    <Field label="Daily output" section="tokens" field="daily_output_limit" />
                    <Field label="Monthly output" section="tokens" field="monthly_output_limit" />
                </div>
                <p className="text-xs text-muted mt-3" data-testid="token-quota-help">
                    The gateway enforces the four input and output quotas (daily and monthly) as it enforces the totals:
                    a call is refused once any quota that is set is reached.
                </p>
            </section>

            {/* SECTION 5 — Requests */}
            <section className="bg-surface border border-default rounded-lg p-6">
                <div className="mb-3">
                    <HeaderHelp
                        sectionKey="requests"
                        openKey={openHelp}
                        onToggle={setOpenHelp}
                        Icon={TrendingUp}
                        label="Requests"
                    />
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <Field label="Daily Limit" section="requests" field="daily_limit" />
                    <Field label="Monthly Limit" section="requests" field="monthly_limit" />
                    <Field label="Daily Warning" section="requests" field="daily_warning_percent" suffix="%" />
                    <Field label="Monthly Warning" section="requests" field="monthly_warning_percent" suffix="%" />
                </div>
            </section>
        </div>
        </LimitsCtx.Provider>
    );
};

export default LLMLimitsForm;
