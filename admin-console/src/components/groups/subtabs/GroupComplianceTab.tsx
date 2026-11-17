/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GroupComplianceTab — per-resource-group compliance posture.
 *
 * Two fields:
 *   - `eu_ai_act_risk_tier` — minimal / limited / high / unacceptable.
 *     Drives `eu_ai_act.<tier>` audit tagging when the tenant has
 *     `eu_ai_act` declared in its compliance profile.
 *   - `phi_resource` — operator-declared HIPAA PHI scope. True tags
 *     every call through this RG as `hipaa.rg_marked` regardless of
 *     what the PII detector saw.
 *
 * The component fetches the tenant's compliance profile on mount so
 * it can show which regimes the declared posture will actually
 * contribute tags to. Without this context an operator could set
 * `eu_ai_act_risk_tier: high` on a tenant that hasn't declared EU AI
 * Act — the field would persist but the tagging engine would never
 * emit a tag for it. We surface that gap explicitly rather than let
 * the operator wonder why their tier choice "did nothing."
 */

import React, { useEffect, useState } from 'react';
import { ShieldCheck, Save, Loader, Info, AlertTriangle } from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import { ResourceGroup, EU_AI_ACT_RISK_TIERS } from '../../../lib/groupTypes';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { HelpTip } from '../../ui/HelpTip';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const TIER_LABELS: Record<string, { label: string; blurb: string; severity: 'green' | 'blue' | 'orange' | 'red' }> = {
    minimal: {
        label: 'Minimal',
        blurb: 'No specific obligations under the AI Act (e.g. AI-enabled spam filters).',
        severity: 'green',
    },
    limited: {
        label: 'Limited',
        blurb: 'Transparency obligations only (e.g. chatbots that must disclose they are AI).',
        severity: 'blue',
    },
    high: {
        label: 'High',
        blurb: 'Strict obligations: risk management, data governance, human oversight, conformity assessment.',
        severity: 'orange',
    },
    unacceptable: {
        label: 'Unacceptable',
        blurb: 'Prohibited under the AI Act (social scoring, real-time biometric ID, etc.). Setting this should usually be paired with disabling the group.',
        severity: 'red',
    },
};

// Severity callout boxes — light + dark mode pairs so the tier blurb
// reads on either theme. The dark variants follow the project-wide
// pattern (see TenantSettingsTab.tsx): -900 border, -950/40 surface,
// -200 text. Without these the box rendered as white-on-near-white in
// dark mode because the bare `-50` tokens have no dark variants.
const SEVERITY_CLASSES: Record<string, string> = {
    green:
        'bg-emerald-50 border-emerald-200 text-emerald-800 ' +
        'dark:bg-emerald-950/40 dark:border-emerald-900 dark:text-emerald-200',
    blue:
        'bg-blue-50 border-blue-200 text-blue-800 ' +
        'dark:bg-blue-950/40 dark:border-blue-900 dark:text-blue-200',
    orange:
        'bg-orange-50 border-orange-200 text-orange-800 ' +
        'dark:bg-orange-950/40 dark:border-orange-900 dark:text-orange-200',
    red:
        'bg-red-50 border-red-200 text-red-800 ' +
        'dark:bg-red-950/40 dark:border-red-900 dark:text-red-200',
};

const GroupComplianceTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { updateResourceGroup, gatewayClient } = useGateway();
    const toast = useToast();

    // Editable state. Initial value mirrors the loaded group; the
    // dirty check below compares back against this snapshot, so a
    // round-trip Save+reload reseeds it cleanly via the `group` prop
    // dependency on the effect.
    const [tier, setTier] = useState<string>(group.eu_ai_act_risk_tier ?? '');
    const [phi, setPhi] = useState<boolean>(group.phi_resource ?? false);
    const [sensitive, setSensitive] = useState<boolean>(group.sensitive_data ?? false);
    const [saving, setSaving] = useState(false);

    // Tenant compliance frameworks — fetched once on mount. Drives
    // the "this tier will / will not produce tags" hint.
    const [frameworks, setFrameworks] = useState<string[] | null>(null);
    const [frameworksError, setFrameworksError] = useState<string | null>(null);

    useEffect(() => {
        setTier(group.eu_ai_act_risk_tier ?? '');
        setPhi(group.phi_resource ?? false);
        setSensitive(group.sensitive_data ?? false);
    }, [group.id, group.eu_ai_act_risk_tier, group.phi_resource, group.sensitive_data]);

    useEffect(() => {
        let cancelled = false;
        runBackground((async () => {
            try {
                const resp = await gatewayClient.getComplianceProfile();
                if (!cancelled) {
                    setFrameworks(resp?.profile?.frameworks ?? []);
                    setFrameworksError(null);
                }
            } catch (err) {
                if (!cancelled) {
                    setFrameworksError(errorMessage(err, 'Failed to load tenant compliance profile'));
                    setFrameworks([]);
                }
            }
        })(), 'async work');
        return () => {
            cancelled = true;
        };
    }, [gatewayClient]);

    const initialTier = group.eu_ai_act_risk_tier ?? '';
    const initialPhi = group.phi_resource ?? false;
    const initialSensitive = group.sensitive_data ?? false;
    const isDirty = tier !== initialTier || phi !== initialPhi || sensitive !== initialSensitive;

    // Whether the tenant has the regimes these fields drive
    // declared. Used for inline "no-op" hints.
    const euAiActDeclared = frameworks?.includes('eu_ai_act') ?? false;
    const hipaaDeclared = frameworks?.includes('hipaa') ?? false;

    const handleSave = async () => {
        setSaving(true);
        try {
            // Explicit null clears the field (the backend's
            // `model_fields_set` branch in update_resource_group
            // preserves the intent over `exclude_none=True`).
            await updateResourceGroup(group.id, {
                eu_ai_act_risk_tier:
                    tier === '' ? null : (tier as 'minimal' | 'limited' | 'high' | 'unacceptable'),
                phi_resource: phi,
                // Sent only when changed, so a save on a CP without the
                // field is not refused for an untouched toggle.
                ...(sensitive !== initialSensitive ? { sensitive_data: sensitive } : {}),
            });
            toast.success(
                'Compliance posture saved',
                'New audit rows for this group will reflect the updated classification.',
            );
            onUpdate();
        } catch (err) {
            toast.error('Save failed', errorMessage(err, 'Unknown error'));
        } finally {
            setSaving(false);
        }
    };

    const handleReset = () => {
        setTier(initialTier);
        setPhi(initialPhi);
        setSensitive(initialSensitive);
    };

    return (
        <div className="p-4 space-y-6 max-w-3xl">
            {/* Header */}
            <div className="flex items-center gap-2">
                <ShieldCheck className="w-5 h-5 text-primary-500" />
                <h3 className="text-base font-semibold text-default">Compliance posture</h3>
            </div>

            {/* Context: which tenant frameworks these fields feed */}
            <div className="rounded-md border border-default bg-surface-alt p-3 text-xs">
                <div className="flex items-start gap-2">
                    <Info className="w-4 h-4 mt-0.5 text-secondary flex-shrink-0" />
                    <div className="space-y-1.5">
                        <p className="text-secondary">
                            These fields are read by the audit-tagging engine to drive{' '}
                            <code className="px-1 bg-surface rounded">eu_ai_act.&lt;tier&gt;</code> and{' '}
                            <code className="px-1 bg-surface rounded">hipaa.rg_marked</code>{' '}
                            tags on every call through this group. They take effect only
                            when the matching framework is declared on the tenant's
                            compliance profile.
                        </p>
                        <p className="text-secondary">
                            <strong>Per-resource-group only — not inherited.</strong> Each
                            resource group attests its own posture; a child group does
                            NOT pick up its parent's tier or PHI flag automatically. If
                            you classify <em>this</em> group as EU AI Act <code>high</code>{' '}
                            or PHI-bearing, only calls routed through this exact group
                            get tagged — sibling and descendant groups remain untagged
                            until you set their posture explicitly. This is deliberate:
                            compliance tags should reflect operator attestation per
                            system, not implicit cascade, so an auditor reads the
                            evidence trail as the operator meant it.
                        </p>
                        <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-muted">Tenant frameworks:</span>
                            {frameworks === null ? (
                                <span className="text-muted italic">loading…</span>
                            ) : frameworks.length === 0 ? (
                                <span className="text-muted italic">none declared</span>
                            ) : (
                                frameworks.map((f) => (
                                    <span
                                        key={f}
                                        className="px-1.5 py-0.5 rounded bg-primary-50 text-primary-700 border border-primary-200 dark:bg-primary-950/40 dark:text-primary-200 dark:border-primary-900 text-[11px] font-mono"
                                    >
                                        {f}
                                    </span>
                                ))
                            )}
                        </div>
                        {frameworksError && (
                            <div className="text-red-600 text-[11px]">{frameworksError}</div>
                        )}
                    </div>
                </div>
            </div>

            {/* EU AI Act risk tier */}
            <section className="bg-surface border border-default rounded-md p-4 space-y-3">
                <div className="flex items-baseline justify-between">
                    <h4 className="text-sm font-semibold text-default">EU AI Act risk tier</h4>
                    {!euAiActDeclared && frameworks !== null && (
                        <span className="text-[11px] text-muted italic flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3" />
                            Tenant has not declared EU AI Act — tier is recorded but no tags will fire
                        </span>
                    )}
                </div>
                <p className="text-xs text-secondary">
                    Classify what this resource group hosts under the EU AI Act's risk hierarchy.
                    The tier becomes the tag suffix (e.g. <code className="px-1 bg-surface-alt rounded">eu_ai_act.high</code>)
                    on every audit row from this group when the tenant declares the regime.
                </p>
                <select
                    value={tier}
                    onChange={(e) => setTier(e.target.value)}
                    className="w-full px-3 py-2 text-sm border border-strong rounded bg-surface text-default focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                >
                    <option value="">— Unclassified —</option>
                    {EU_AI_ACT_RISK_TIERS.map((t) => (
                        <option key={t} value={t}>
                            {TIER_LABELS[t].label}
                        </option>
                    ))}
                </select>
                {tier && TIER_LABELS[tier] && (
                    <div
                        className={`text-xs p-2 rounded border ${SEVERITY_CLASSES[TIER_LABELS[tier].severity]}`}
                    >
                        <span className="font-medium">{TIER_LABELS[tier].label}:</span>{' '}
                        {TIER_LABELS[tier].blurb}
                    </div>
                )}
            </section>

            {/* HIPAA PHI flag */}
            <section className="bg-surface border border-default rounded-md p-4 space-y-3">
                <div className="flex items-baseline justify-between">
                    <h4 className="text-sm font-semibold text-default">HIPAA PHI scope</h4>
                    {!hipaaDeclared && frameworks !== null && (
                        <span className="text-[11px] text-muted italic flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3" />
                            Tenant has not declared HIPAA — flag is recorded but no tags will fire
                        </span>
                    )}
                </div>
                <label className="flex items-start gap-3 cursor-pointer">
                    <input
                        type="checkbox"
                        checked={phi}
                        onChange={(e) => setPhi(e.target.checked)}
                        className="mt-0.5 h-4 w-4 text-primary-600 focus:ring-primary-500 border-strong rounded"
                    />
                    <div className="text-xs">
                        <div className="font-medium text-default">
                            This group's resources may contain PHI
                        </div>
                        <p className="text-secondary mt-0.5">
                            Stamps <code className="px-1 bg-surface-alt rounded">hipaa.rg_marked</code> on every audit row from this group,
                            independent of what the PII detector saw on the wire.
                            Use this when the resources hosted in this group are
                            known to handle protected health information — operator
                            attestation, not detector inference.
                        </p>
                    </div>
                </label>
            </section>

            {/* Sensitive data (privileged-read rule) */}
            <section className="bg-surface border border-default rounded-md p-4 space-y-3" data-testid="rg-sensitive-data">
                <div className="flex items-center gap-1.5">
                    <h4 className="text-sm font-semibold text-default">Sensitive data</h4>
                    <HelpTip text="Evidence records are sealed for commands, not queries: an MCP tool annotated readOnlyHint is normally not sealed. On a group tagged sensitive — this flag or the PHI flag above — read-only tool calls are sealed as evidence records too (the privileged-read rule HIPAA and PCI share), so who read what sensitive resource, and under which policy, is on the record. Refusals are always sealed." />
                </div>
                <label className="flex items-start gap-3 cursor-pointer">
                    <input
                        type="checkbox"
                        checked={sensitive}
                        onChange={(e) => setSensitive(e.target.checked)}
                        aria-label="This group's resources hold sensitive data"
                        className="mt-0.5 h-4 w-4 text-primary-600 focus:ring-primary-500 border-strong rounded"
                        data-testid="rg-sensitive-data-toggle"
                    />
                    <div className="text-xs">
                        <div className="font-medium text-default">This group's resources hold sensitive data</div>
                        <p className="text-secondary mt-0.5">
                            Seals read-only tool calls through this group as evidence, alongside every consequential
                            action. Use it for resources that hold personal, financial or otherwise regulated data
                            that is not PHI.
                        </p>
                    </div>
                </label>
            </section>

            {/* Save bar */}
            <div className="flex items-center justify-end gap-3 pt-2 border-t border-default">
                {isDirty && (
                    <button
                        type="button"
                        onClick={handleReset}
                        disabled={saving}
                        className="px-3 py-1.5 text-xs text-secondary hover:text-default"
                    >
                        Discard changes
                    </button>
                )}
                <button
                    type="button"
                    onClick={asyncHandler(handleSave, 'click')}
                    disabled={!isDirty || saving}
                    className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium bg-primary-500 text-white rounded-md hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    {saving ? <Loader className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    Save compliance posture
                </button>
            </div>
        </div>
    );
};

export default GroupComplianceTab;
