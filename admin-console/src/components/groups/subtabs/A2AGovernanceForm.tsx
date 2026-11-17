/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational A2A-governance form. Renders the "Capability access",
 * "Deny classifications", and "Allowed remote URL globs" cards over a plain
 * `A2AGovernance` dict and reports edits back via `onChange`. It owns no
 * fetch/save state — the parent (e.g. `GroupA2AGovernanceTab`) holds the
 * governance dict and runs load/save/dirty-tracking, so the same form can be
 * reused by any caller that needs to edit A2A governance.
 *
 * Sibling to `SkillGovernanceForm` / `MCPGovernanceForm`. Inherited-value
 * hints and the effective-value hint are surfaced from the optional
 * `inherited` / `effective` props (informational only). Allow-lists are
 * the exception: the gateway judges the list of each level on its own, so
 * the lists of the levels above come one by one in `allowListsAbove`. The info banner and
 * save/revert footer stay with the owning tab; the chip-input buffers are
 * local UI state (no I/O), matching the presentational-form pattern.
 */

import React, { useState } from 'react';
import { Plus, ShieldCheck, X } from 'lucide-react';

import { APPROVAL_TIMEOUT_MAX_SECONDS, APPROVAL_TIMEOUT_MIN_SECONDS, approvalTimeoutProblem } from '../../../lib/approvalTimeout';

export type { A2AGovernance } from '../../../lib/groupTypes';
import type { A2AGovernance } from '../../../lib/groupTypes';

export type AccessLevel = NonNullable<NonNullable<A2AGovernance['capability']>['default_access']>;

interface A2AGovernanceFormProps {
    value: A2AGovernance;
    onChange: (next: A2AGovernance) => void;
    inherited?: A2AGovernance | null;
    effective?: A2AGovernance | null;
    /**
     * The allow-list of each level above that states one, by its name, from
     * the top down. The gateway judges each list on its own, so they are
     * shown one by one and never merged.
     */
    allowListsAbove?: readonly AllowListLevel[];
    /** How the allow-lists of the levels apply, in the Control Plane's words. */
    allowListsRule?: string;
    disabled?: boolean;
}

/** One level above and the allow-list it states. */
export interface AllowListLevel {
    level: string;
    globs: string[];
}

/**
 * Pure presentational renderer for the A2A governance field set.
 *
 * Holds no fetch/save state — it renders `value`, surfaces inherited /
 * effective hints, and emits the next `A2AGovernance` object through
 * `onChange`. The chip-input buffers are the only local state (no I/O).
 */
const A2AGovernanceForm: React.FC<A2AGovernanceFormProps> = ({
    value,
    onChange,
    inherited = null,
    effective = null,
    allowListsAbove = [],
    allowListsRule,
    disabled = false,
}) => {
    const gov = value as A2AGovernance;
    const inheritedGov = inherited as A2AGovernance | null;
    const effectiveGov = effective as A2AGovernance | null;

    // Chip input buffers (local UI state; no I/O)
    const [tagInput, setTagInput] = useState('');
    const [globInput, setGlobInput] = useState('');

    function setAccess(access: AccessLevel | '') {
        onChange({
            ...value,
            capability: {
                ...(gov.capability ?? {}),
                default_access: access || undefined,
            },
        });
    }

    const timeoutProblem = approvalTimeoutProblem(gov.capability?.approval_timeout_seconds);

    function setApprovalTimeout(raw: string) {
        const parsed = raw === '' ? null : Number(raw);
        if (raw !== '' && Number.isNaN(parsed)) return;
        onChange({
            ...value,
            capability: {
                ...(gov.capability ?? {}),
                approval_timeout_seconds: parsed,
            },
        });
    }

    function addTag() {
        const v = tagInput.trim();
        if (!v) return;
        const existing = gov.data?.deny_classifications ?? [];
        if (!existing.includes(v)) {
            onChange({
                ...value,
                data: { ...(gov.data ?? {}), deny_classifications: [...existing, v] },
            });
        }
        setTagInput('');
    }

    function removeTag(idx: number) {
        const list = gov.data?.deny_classifications ?? [];
        onChange({
            ...value,
            data: {
                ...(gov.data ?? {}),
                deny_classifications: list.filter((_, i) => i !== idx),
            },
        });
    }

    function addGlob() {
        const v = globInput.trim();
        if (!v) return;
        const existing = gov.remote?.allowed_globs ?? [];
        if (!existing.includes(v)) {
            onChange({
                ...value,
                remote: { ...(gov.remote ?? {}), allowed_globs: [...existing, v] },
            });
        }
        setGlobInput('');
    }

    function removeGlob(idx: number) {
        const list = gov.remote?.allowed_globs ?? [];
        onChange({
            ...value,
            remote: {
                ...(gov.remote ?? {}),
                allowed_globs: list.filter((_, i) => i !== idx),
            },
        });
    }

    const inheritedAccess = inheritedGov?.capability?.default_access;
    const effectiveAccess = effectiveGov?.capability?.default_access;

    return (
        <>
            {/* Capability access */}
            <Section
                icon={<ShieldCheck className="w-4 h-4 text-primary-600" />}
                title="Capability access"
                description="What the gateway does when a member calls a capability of a bound card."
            >
                <p className="text-xs text-muted" data-testid="a2a-levels-help">
                    Each level that sets a policy is judged on its own: the caller&apos;s group, every group above it,
                    and the agent card. A refusal at any level refuses the call; otherwise a hold at any level holds it.
                </p>
                <div className="grid grid-cols-1 md:grid-cols-[1fr,220px,auto] gap-3 items-center">
                    <div>
                        <label className="text-sm font-medium text-default">
                            default_access
                        </label>
                        {inheritedAccess && (
                            <p className="text-xs text-muted">
                                Inherited: {inheritedAccess}
                            </p>
                        )}
                    </div>
                    <select
                        value={gov.capability?.default_access ?? ''}
                        onChange={e => setAccess(e.target.value as AccessLevel | '')}
                        disabled={disabled}
                        className="px-3 py-1.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface text-default"
                    >
                        <option value="">(not set — enabled by default)</option>
                        <option value="enabled">enabled</option>
                        <option value="approval_required">approval_required</option>
                        <option value="disabled">disabled</option>
                    </select>
                    <span className="text-xs text-muted">
                        {effectiveAccess ? `Effective: ${effectiveAccess}` : ''}
                    </span>
                </div>
                <p className="text-xs text-muted" data-testid="a2a-access-help">
                    <code>enabled</code> lets the call through. <code>approval_required</code> holds a call that does
                    work (a message, an invocation) until a person approves or rejects it in Mission Control →
                    Approvals; a call that only reads is not held. <code>disabled</code> refuses every A2A call.
                </p>
                <div className="grid grid-cols-1 md:grid-cols-[1fr,220px,auto] gap-3 items-center">
                    <div>
                        <label className="text-sm font-medium text-default">
                            approval_timeout_seconds
                        </label>
                        {inheritedGov?.capability?.approval_timeout_seconds !== undefined && (
                            <p className="text-xs text-muted">
                                Inherited: {inheritedGov.capability.approval_timeout_seconds}
                            </p>
                        )}
                    </div>
                    <input
                        type="number"
                        min={APPROVAL_TIMEOUT_MIN_SECONDS}
                        max={APPROVAL_TIMEOUT_MAX_SECONDS}
                        value={
                            gov.capability?.approval_timeout_seconds ?? ''
                        }
                        onChange={e => setApprovalTimeout(e.target.value)}
                        placeholder="—"
                        disabled={disabled}
                        className="px-3 py-1.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface text-default"
                    />
                    <span className="text-xs text-muted">
                        {effectiveGov?.capability?.approval_timeout_seconds !== undefined
                            ? `Effective: ${effectiveGov.capability.approval_timeout_seconds}`
                            : ''}
                    </span>
                </div>
                {timeoutProblem && (
                    <p role="alert" className="text-xs text-red-600" data-testid="approval-timeout-problem">{timeoutProblem}</p>
                )}
                <p className="text-xs text-muted" data-testid="a2a-approval-window-help">
                    How long a held call waits for a decision, in seconds. When the time runs out, the call is not
                    made. The shortest time any level states applies. Empty: the group&apos;s default approval window,
                    then the tenant&apos;s, then 5 minutes. At most 7 days.
                </p>
            </Section>

            {/* Deny classifications */}
            <Section
                icon={<ShieldCheck className="w-4 h-4 text-primary-600" />}
                title="Deny classifications"
                description="Block any capability whose declared classification tags intersect this list (PII, PHI, ...)."
            >
                <ChipList
                    values={gov.data?.deny_classifications ?? []}
                    inherited={inheritedGov?.data?.deny_classifications ?? undefined}
                    onAdd={addTag}
                    onRemove={removeTag}
                    input={tagInput}
                    onInputChange={setTagInput}
                    placeholder="PII"
                    variant="deny"
                    disabled={disabled}
                />
            </Section>

            {/* Allow globs */}
            <Section
                icon={<ShieldCheck className="w-4 h-4 text-primary-600" />}
                title="Allowed remote URL globs"
                description="The addresses an outbound call may leave for."
            >
                <p className="text-xs text-muted" data-testid="a2a-allow-list-help">
                    Judges outbound calls only: the address a call leaves for must match at least one pattern. An
                    inbound call has no remote address, so the list does not judge it. Every level that states a list
                    is judged on its own, so the address must match the list of each of them. An empty list restricts
                    nothing at this level.
                </p>
                <ChipList
                    values={gov.remote?.allowed_globs ?? []}
                    onAdd={addGlob}
                    onRemove={removeGlob}
                    input={globInput}
                    onInputChange={setGlobInput}
                    placeholder="https://*.partner.com/*"
                    variant="allow"
                    disabled={disabled}
                />
                {allowListsRule && (
                    <p className="text-xs text-muted" data-testid="a2a-allow-lists-rule">
                        {allowListsRule}
                    </p>
                )}
                {allowListsAbove.length > 0 && (
                    <div className="text-xs text-muted" data-testid="a2a-allow-lists-above">
                        <p>The address must also match the allow-list of each of these groups:</p>
                        <ul className="list-disc pl-5 mt-1 space-y-0.5">
                            {allowListsAbove.map(({ level, globs }) => (
                                <li key={level}>
                                    {level}: {globs.join(', ')}
                                </li>
                            ))}
                        </ul>
                    </div>
                )}
            </Section>
        </>
    );
};

export default A2AGovernanceForm;

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

const ChipList: React.FC<{
    values: string[];
    inherited?: string[];
    onAdd: () => void;
    onRemove: (idx: number) => void;
    input: string;
    onInputChange: (v: string) => void;
    placeholder: string;
    variant: 'deny' | 'allow';
    disabled?: boolean;
}> = ({ values, inherited, onAdd, onRemove, input, onInputChange, placeholder, variant, disabled }) => {
    const chipClass =
        variant === 'deny'
            ? 'bg-surface-alt border-default text-red-700'
            : 'bg-primary-50 border-primary-200 text-primary-700';
    return (
        <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
                {values.map((v, i) => (
                    <span
                        key={`${v}-${i}`}
                        className={`inline-flex items-center space-x-1 px-2 py-0.5 text-xs rounded-full border ${chipClass}`}
                    >
                        <code>{v}</code>
                        <button
                            type="button"
                            onClick={() => onRemove(i)}
                            disabled={disabled}
                            aria-label={`Remove ${v}`}
                            className="hover:opacity-70 disabled:opacity-50"
                        >
                            <X className="w-3 h-3" />
                        </button>
                    </span>
                ))}
                <input
                    value={input}
                    onChange={e => onInputChange(e.target.value)}
                    onKeyDown={e => {
                        if (e.key === 'Enter') {
                            e.preventDefault();
                            onAdd();
                        }
                    }}
                    placeholder={placeholder}
                    disabled={disabled}
                    className="w-60 px-3 py-1.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface text-default"
                />
                <button
                    type="button"
                    onClick={onAdd}
                    disabled={disabled}
                    className="flex items-center space-x-1 px-2.5 py-1.5 text-xs font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                >
                    <Plus className="w-3 h-3" />
                    <span>Add</span>
                </button>
            </div>
            {inherited && inherited.length > 0 && (
                <p className="text-xs text-muted">
                    Inherited: {inherited.join(', ')}
                </p>
            )}
        </div>
    );
};
