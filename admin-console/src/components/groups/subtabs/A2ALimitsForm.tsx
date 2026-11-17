/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational A2A-limits form. Renders the "Invocations" and
 * "Delegation chain" cards over a plain `A2ALimits` dict and reports edits
 * back via `onChange`. It owns no fetch/save state — the parent (e.g.
 * `GroupA2ALimitsTab`) holds the limits dict and runs load/save/dirty-
 * tracking, so the same form can be reused by any caller that needs to edit
 * A2A limits.
 *
 * Sibling to `SkillLimitsForm` / `MCPLimitsForm`. Inherited-value hints and
 * the effective-value hint are surfaced from the optional `inherited` /
 * `effective` props (informational only). The info banner and save/revert
 * footer stay with the owning tab.
 */

 

import React from 'react';
import { ShieldAlert, Workflow } from 'lucide-react';
import UnavailableSettings from '../../common/UnavailableSettings';
import { withoutUnavailable, type UnavailableSetting } from '../../../lib/unavailableSettings';

// A2A absolute depth cap — mirrors Rust's
// `brutor-gateway-core/src/services/a2a_delegation.rs::ABSOLUTE_MAX_DEPTH`.
// The proxy refuses any chain above this value regardless of the tenant
// or group rule, so the UI should surface it as a hard ceiling.
export const ABSOLUTE_MAX_DEPTH = 32;

export interface InvocationLimits {
    daily_limit?: number | null;
    monthly_limit?: number | null;
    daily_warning_percent?: number | null;
    monthly_warning_percent?: number | null;
}

export interface ChainLimits {
    max_delegation_depth?: number | null;
}

export interface A2ALimits {
    invocations?: InvocationLimits;
    chain?: ChainLimits;
}

/** The A2A limits the gateway does not act on in this release: no alert is raised before the invocation cap. */
const A2A_LIMITS_NOT_AVAILABLE: readonly UnavailableSetting[] = [
    { path: ['invocations', 'daily_warning_percent'], label: 'Daily warning (%)' },
    { path: ['invocations', 'monthly_warning_percent'], label: 'Monthly warning (%)' },
];

interface A2ALimitsFormProps {
    value: A2ALimits | Record<string, any>;
    onChange: (next: A2ALimits | Record<string, any>) => void;
    inherited?: A2ALimits | Record<string, any> | null;
    effective?: A2ALimits | Record<string, any> | null;
    disabled?: boolean;
}

function getNestedNum(src: A2ALimits | Record<string, any> | null | undefined, section: keyof A2ALimits, field: string) {
    if (!src) return undefined;
    const sec = (src as any)[section] as Record<string, unknown> | undefined;
    const v = sec?.[field];
    if (typeof v === 'number') return v;
    return undefined;
}

/**
 * Pure presentational renderer for the A2A limit fields.
 *
 * Holds no fetch/save state — it renders `value`, surfaces inherited /
 * effective hints, and emits the next `A2ALimits` object through `onChange`.
 */
const A2ALimitsForm: React.FC<A2ALimitsFormProps> = ({
    value,
    onChange,
    inherited = null,
    effective = null,
    disabled = false,
}) => {
    const limits = value as A2ALimits;

    // Update a nested number field and emit the next limits object.
    // Ignores non-numeric input, which also means no spurious
    // onChange/dirty on garbage.
    function setNum(section: keyof A2ALimits, field: string, raw: string) {
        const parsed = raw === '' ? null : Number(raw);
        if (raw !== '' && Number.isNaN(parsed)) return;
        onChange(
            withoutUnavailable(
                {
                    ...value,
                    [section]: {
                        ...((value as any)[section] ?? {}),
                        [field]: parsed,
                    },
                },
                A2A_LIMITS_NOT_AVAILABLE,
            ),
        );
    }


    return (
        <>
            <UnavailableSettings doc={value} settings={A2A_LIMITS_NOT_AVAILABLE} />
            <Section
                icon={<Workflow className="w-4 h-4 text-primary-600" />}
                title="Invocations"
                description="Daily and monthly caps on the number of A2A calls this group can initiate."
            >
                <LimitRow
                    label="Daily limit"
                    value={limits.invocations?.daily_limit ?? null}
                    inheritedValue={getNestedNum(inherited, 'invocations', 'daily_limit')}
                    effectiveValue={getNestedNum(effective, 'invocations', 'daily_limit')}
                    onChange={v => setNum('invocations', 'daily_limit', v)}
                    min={0}
                    disabled={disabled}
                />
                <LimitRow
                    label="Monthly limit"
                    value={limits.invocations?.monthly_limit ?? null}
                    inheritedValue={getNestedNum(inherited, 'invocations', 'monthly_limit')}
                    effectiveValue={getNestedNum(effective, 'invocations', 'monthly_limit')}
                    onChange={v => setNum('invocations', 'monthly_limit', v)}
                    min={0}
                    disabled={disabled}
                />
                <p className="text-xs text-muted">
                    Invocation caps always block once the daily or monthly limit
                    is reached — the same as the frequency limits below.
                </p>
            </Section>

            <Section
                icon={<ShieldAlert className="w-4 h-4 text-primary-600" />}
                title="Delegation chain"
                description={`Cap how many delegations deep this group's calls can fan out. Absolute proxy cap: ${ABSOLUTE_MAX_DEPTH}.`}
            >
                <LimitRow
                    label="max_delegation_depth"
                    value={limits.chain?.max_delegation_depth ?? null}
                    inheritedValue={getNestedNum(inherited, 'chain', 'max_delegation_depth')}
                    effectiveValue={getNestedNum(effective, 'chain', 'max_delegation_depth')}
                    onChange={v => setNum('chain', 'max_delegation_depth', v)}
                    min={0}
                    max={ABSOLUTE_MAX_DEPTH}
                    disabled={disabled}
                />
            </Section>
        </>
    );
};

export default A2ALimitsForm;

// ─── sub-components ────────────────────────────────────────────────────

const Section: React.FC<{
    icon: React.ReactNode;
    title: string;
    description: string;
    children: React.ReactNode;
}> = ({ icon, title, description, children }) => (
    <section className="bg-surface border border-default rounded-lg p-4 shadow-sm space-y-3">
        <header className="flex items-center space-x-2">
            {icon}
            <h4 className="text-sm font-semibold text-default">{title}</h4>
        </header>
        <p className="text-xs text-muted">{description}</p>
        <div className="space-y-2">{children}</div>
    </section>
);

const LimitRow: React.FC<{
    label: string;
    value: number | null;
    inheritedValue?: number;
    effectiveValue?: number;
    onChange: (raw: string) => void;
    min?: number;
    max?: number;
    isPercent?: boolean;
    disabled?: boolean;
}> = ({ label, value, inheritedValue, effectiveValue, onChange, min, max, isPercent, disabled }) => (
    <div className="grid grid-cols-1 md:grid-cols-[1fr,160px,auto] gap-3 items-center">
        <div>
            <label className="text-sm font-medium text-default">{label}</label>
            {inheritedValue !== undefined && (
                <p className="text-xs text-muted">
                    Inherited: {inheritedValue}
                    {isPercent ? '%' : ''}
                </p>
            )}
        </div>
        <input
            type="number"
            value={value === null || value === undefined ? '' : value}
            onChange={e => onChange(e.target.value)}
            min={min}
            max={max}
            placeholder="—"
            disabled={disabled}
            className="px-3 py-1.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface text-default"
        />
        <span className="text-xs text-muted">
            {effectiveValue !== undefined ? `Effective: ${effectiveValue}${isPercent ? '%' : ''}` : ''}
        </span>
    </div>
);
