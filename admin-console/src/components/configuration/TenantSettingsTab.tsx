/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { ownTenantOf, type CurrentTenant } from '../../lib/gatewayApi/tenant';
import { mergeTabs, useSlot, type SlotIcon, type TenantModelField } from '../../app/slots';
import { errorMessage, errorStatus, getString } from '../../lib/narrow';
import { toModelOptions, type TenantModelOption } from '../../lib/tenantModelOptions';
import { gateFloorsOf } from '../../lib/lifecycleGates';
import React, { useEffect, useMemo, useState } from 'react';
import {
    Building2,
    Brain,
    Shield,
    ShieldCheck,
    Save,
    AlertTriangle,
    Lock,
    Loader,
    Globe,
    Database,
    Plus,
    Trash2,
    Info,
    FileSignature,
    Fingerprint,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import TenantEvidencePanel from './TenantEvidencePanel';
import TenantIdentityDefaultPanel from './TenantIdentityDefaultPanel';
import { useToast } from '../common/Toast';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import type { ApiSendableBody } from '../../lib/api/controlPlaneTypes';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

type SubTabId = 'identity' | 'profile' | 'security' | 'embedding' | 'residency' | 'retention' | 'assurance' | 'workload-identity' | 'evidence';

/** A sub-tab of the strip: one of this page's own or a contributed one. */
/** The two-factor policies the Control Plane accepts for a tenant. */
type MfaPolicy = NonNullable<ApiSendableBody<'/v1/admin/tenants/current', 'patch'>['mfa_policy']>;
const MFA_POLICIES: readonly MfaPolicy[] = ['optional', 'required_admins', 'required_all'];

/** `value` as a two-factor policy; a tenant with none stored has `optional`. */
function mfaPolicy(value: string | null | undefined): MfaPolicy {
    return MFA_POLICIES.find((policy) => policy === value) ?? 'optional';
}

interface SubTab {
    id: string;
    label: string;
    icon: SlotIcon;
    /** Editable sub-tabs show the save bar; the others the read-only mark, unless they save themselves. */
    editable: boolean;
    /** A contributed sub-tab with a save of its own: neither the save bar nor the read-only mark. */
    savesItself?: boolean;
}

// Per-tenant residency profile. Shape matches
// the Rust `services::residency::ResidencyProfile` and the Python
// `DataResidencyProfile`. NULL profile means enforcement is off.
interface DataResidencyProfile {
    allowed_regions: string[];
    block_global: boolean;
}

// Per-tenant retention policy. Mirrors the Python
// `DataRetentionPolicy`; null means store and keep everything.
interface DataRetentionPolicy {
    store_bodies: 'full' | 'redacted' | 'none';
    body_days?: number | null;
    log_days?: number | null;
    run_days?: number | null;
}

/** The tenant's record as the Control Plane answers it to its administrator. */
type TenantRecord = CurrentTenant;

interface EditableFormState {
    display_name: string;
    organization_name: string;
    domain: string;
    primary_contact_email: string;
    primary_contact_name: string;
    mfa_policy: MfaPolicy;
    default_judge_model_id: string;
}

// The tenant's two-factor policy, least to most restrictive (the order is
// what "stricter" means below). Enforced by the control plane at admin
// sign-in and by the core proxy at Portal sign-in.
const MFA_OPTIONS = [
    { value: 'optional', label: 'Optional' },
    { value: 'required_admins', label: 'Required for administrators' },
    { value: 'required_all', label: 'Required for everyone (administrators and Portal users)' },
];
const mfaRank = (value: string | null | undefined) => MFA_OPTIONS.findIndex((opt) => opt.value === (value || 'optional'));
const mfaLabel = (value: string | null | undefined) =>
    MFA_OPTIONS.find((opt) => opt.value === (value || 'optional'))?.label ?? String(value);

interface TenantSettingsTabProps {
    /** Deep link (`config:tenant:<sub>`), e.g. the inbox's evidence_unwitnessed → `evidence`. */
    initialSubTab?: string;
}

// This page's own sub-tabs — ordered so editable tabs come first, then the
// read-only/governance ones. The ones the platform operator sets (Quotas,
// Features, Billing) are contributed to the `tenant-settings.tabs` slot.
const SUB_TABS: (SubTab & { id: SubTabId })[] = [
    { id: 'identity', label: 'Identity', icon: Building2, editable: false },
    { id: 'profile', label: 'Profile', icon: Building2, editable: true },
    { id: 'security', label: 'Security', icon: Shield, editable: true },
    { id: 'embedding', label: 'Default Models', icon: Brain, editable: true },
    { id: 'residency', label: 'Data Residency', icon: Globe, editable: true },
    { id: 'retention', label: 'Data Retention', icon: Database, editable: true },
    { id: 'assurance', label: 'AI System Assurance', icon: ShieldCheck, editable: true },
    // The tenant identity default. It saves itself (it needs its own
    // permission), so no save bar.
    { id: 'workload-identity', label: 'Workload Identity', icon: Fingerprint, editable: false },
    // Witnesses and the tenant log's key. The panel has
    // its own add / edit / delete actions, so no save bar.
    { id: 'evidence', label: 'Evidence', icon: FileSignature, editable: false },
];

const TenantSettingsTab: React.FC<TenantSettingsTabProps> = ({ initialSubTab }) => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    const [tenant, setTenant] = useState<TenantRecord | null>(null);
    const [judgeModels, setJudgeModels] = useState<TenantModelOption[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [form, setForm] = useState<EditableFormState>({
        display_name: '',
        organization_name: '',
        domain: '',
        primary_contact_email: '',
        primary_contact_name: '',
        mfa_policy: 'optional',
        default_judge_model_id: '',
    });
    // The two-factor policy is sent only when it was changed here: a save
    // from a stale form must not put back a policy another administrator
    // has since tightened. `mfaError` is the control plane's refusal of the
    // last change (409: enable 2FA on your own account first).
    const [mfaTouched, setMfaTouched] = useState(false);
    const [mfaError, setMfaError] = useState<string | null>(null);
    // Model sections contributed to the Default Models sub-tab (slot
    // `tenant-settings.model-sections`). Each picks the model held in one
    // field of the tenant. The page loads the field and the models to pick
    // from, keeps what is picked, and saves it with everything else; a change
    // to another model needs Save pressed twice (`confirmModelSwitch`).
    // All three are keyed by the section's field.
    const modelSections = useSlot('tenant-settings.model-sections');
    const [sectionModels, setSectionModels] = useState<Record<string, TenantModelOption[]>>({});
    const [sectionValues, setSectionValues] = useState<Record<string, string>>({});
    const [sectionStored, setSectionStored] = useState<Record<string, string>>({});
    const [confirmModelSwitch, setConfirmModelSwitch] = useState(false);
    // Contributed sub-tabs have no part in the save: read-only, or saving themselves.
    const slotTabs = useSlot('tenant-settings.tabs');
    const { subTabs, contributed } = useMemo(() => {
        const merged = mergeTabs(SUB_TABS, slotTabs);
        return {
            subTabs: merged.tabs.map((t): SubTab => ('editable' in t ? t : { ...t, editable: false })),
            contributed: merged.contributed,
        };
    }, [slotTabs]);
    const [activeSubTab, setActiveSubTab] = useState<string>(
        subTabs.find((t) => t.id === initialSubTab)?.id ?? 'identity',
    );

    // Residency profile is kept in its own state slot rather than the
    // flat `form` because it's a nested JSONB column — null means
    // enforcement off, an object means on. PATCH payload omits the
    // field unless the user touched this tab, so other tabs' saves
    // don't accidentally re-stamp the residency JSONB.
    const [residency, setResidency] = useState<DataResidencyProfile | null>(null);
    const [residencyTouched, setResidencyTouched] = useState(false);

    // Retention policy. Same touched-gate as residency.
    const [retention, setRetention] = useState<DataRetentionPolicy | null>(null);
    const [retentionTouched, setRetentionTouched] = useState(false);

    // Automatic assurance capture. Same touched-gate as
    // residency: a save from another sub-tab must not round-trip
    // this value. Empty input means null on the wire, which the scheduler
    // reads as "do not capture on a schedule" — the default.
    const [captureHours, setCaptureHours] = useState<string>('');
    const [captureTouched, setCaptureTouched] = useState(false);

    // The tenant's default approval window, in hours as typed. Same
    // touched-gate; empty = null = the platform default (5 minutes).
    const [approvalHours, setApprovalHours] = useState<string>('');
    const [approvalTouched, setApprovalTouched] = useState(false);

    // Per-tenant lifecycle gate policy. Edited as JSON:
    // the shape is a small map of transition -> requirement keys, and a
    // bespoke matrix editor would have to duplicate the gate's vocabulary in
    // the client, where it would drift. The vocabulary endpoint below is
    // fetched instead, so what the operator sees is what the gate enforces.
    const [gatePolicyText, setGatePolicyText] = useState<string>('');
    const [gatePolicyTouched, setGatePolicyTouched] = useState(false);
    const [gateVocab, setGateVocab] = useState<any>(null);
    const [readFailure, reads] = useReadFailures<'vocabulary'>();
    const [gateJsonError, setGateJsonError] = useState<string | null>(null);

    const loadData = async () => {
        setLoading(true);
        try {
            // Gate vocabulary comes from the gate's own tables, so the editor
            // can never offer a transition or requirement the gate doesn't
            // know. Without it the field still works; the reference list
            // says it could not be loaded.
            gatewayClient
                .getGatePolicyVocabulary()
                .then((v: any) => {
                    setGateVocab(v);
                    reads.loaded('vocabulary');
                })
                .catch((err: unknown) => {
                    setGateVocab(null);
                    reads.lost('vocabulary', 'Failed to load the transitions and requirements the gate knows', err);
                });

            // A section's models are part of the load: the page does not
            // open without them.
            const [tenantAnswer, judgeResp, sectionLists] = await Promise.all([
                gatewayClient.getCurrentTenant(),
                gatewayClient.listLLMs({ mode: 'chat', enabled: true }) as Promise<unknown>,
                Promise.all(modelSections.map((section) => section.loadModels(gatewayClient))),
            ]);
            // A system administrator belongs to no tenant: this page is a tenant's.
            const tenantResp = ownTenantOf(tenantAnswer);
            if (!tenantResp) throw new Error('These are the settings of a tenant, and this session belongs to no tenant.');
            setTenant(tenantResp);
            const stored = Object.fromEntries(
                modelSections.map((section) => [section.field, getString(tenantResp, section.field) ?? '']),
            );
            setSectionValues(stored);
            setSectionStored(stored);
            setSectionModels(Object.fromEntries(modelSections.map((section, i) => [section.field, sectionLists[i]])));
            setForm({
                display_name: tenantResp.display_name || '',
                organization_name: tenantResp.organization_name || '',
                domain: tenantResp.domain || '',
                primary_contact_email: tenantResp.primary_contact_email || '',
                primary_contact_name: tenantResp.primary_contact_name || '',
                mfa_policy: mfaPolicy(tenantResp.mfa_policy),
                default_judge_model_id: tenantResp.default_judge_model_id || '',
            });
            setMfaTouched(false);
            setMfaError(null);
            setResidency(tenantResp.data_residency ?? null);
            setResidencyTouched(false);
            setRetention(tenantResp.data_retention ?? null);
            setRetentionTouched(false);
            setCaptureHours(
                tenantResp.assurance_capture_interval_hours != null
                    ? String(tenantResp.assurance_capture_interval_hours)
                    : '',
            );
            setCaptureTouched(false);
            setApprovalHours(
                tenantResp.default_approval_window_seconds != null
                    ? String(tenantResp.default_approval_window_seconds / 3600)
                    : '',
            );
            setApprovalTouched(false);
            setGatePolicyText(
                tenantResp.ai_system_gate_policy
                    ? JSON.stringify(tenantResp.ai_system_gate_policy, null, 2)
                    : '',
            );
            setGatePolicyTouched(false);
            setGateJsonError(null);
            // Judge candidates = enabled chat models.
            setJudgeModels(toModelOptions(judgeResp));
        } catch (err: any) {
            toast.error('Failed to load tenant settings', err?.message || 'Unknown error');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        runBackground(loadData(), 'load data');
    }, []);

    // A section's model is changed when another model is picked. Picking
    // none is not: it is saved with one press.
    const sectionChanged = (field: string) => !!sectionValues[field] && sectionValues[field] !== sectionStored[field];
    const modelChanged = modelSections.some((section) => sectionChanged(section.field));

    const handleSave = async () => {
        if (!tenant) return;

        // If a section's model has changed, require an extra confirmation
        // step — the section says what the switch costs.
        if (modelChanged && !confirmModelSwitch) {
            setConfirmModelSwitch(true);
            return;
        }

        setSaving(true);
        try {
            // What the contributed sections add: named fields of the update,
            // so the body keeps its type (a free dictionary spread into a
            // body is not seen by the compiler).
            const sectionFields: Partial<Record<TenantModelField, string | null>> = {};
            for (const section of modelSections) sectionFields[section.field] = sectionValues[section.field] || null;
            const payload: ApiSendableBody<'/v1/admin/tenants/current', 'patch'> = {
                display_name: form.display_name,
                organization_name: form.organization_name || null,
                domain: form.domain || null,
                primary_contact_email: form.primary_contact_email,
                primary_contact_name: form.primary_contact_name || null,
                ...sectionFields,
                default_judge_model_id: form.default_judge_model_id || null,
            };

            // Only include data_residency when the operator actually
            // touched the residency tab — otherwise we'd round-trip
            // the loaded value back on every save, masking concurrent
            // edits from another admin and stamping the audit log
            // with a no-op change.
            if (residencyTouched) {
                payload.data_residency = residency;
            }
            // The two-factor policy, with the same touched gate.
            if (mfaTouched) {
                payload.mfa_policy = form.mfa_policy;
            }
            if (retentionTouched) {
                payload.data_retention = retention;
            }

            // Assurance capture interval. Blank clears it, which switches
            // scheduled capture off rather than leaving it unchanged — the
            // backend re-adds explicit nulls for exactly this reason.
            if (captureTouched) {
                const raw = captureHours.trim();
                payload.assurance_capture_interval_hours = raw ? Number(raw) : null;
            }

            // Default approval window. Blank = the platform default.
            if (approvalTouched) {
                const raw = approvalHours.trim();
                payload.default_approval_window_seconds = raw ? Math.round(Number(raw) * 3600) : null;
            }

            // Gate policy. Blank reverts to the built-in requirements. Parse
            // failures are caught here rather than sent: a 422 from the server
            // is correct but a JSON error is the operator's typo, not a
            // governance decision, and should be shown next to the field.
            if (gatePolicyTouched) {
                const raw = gatePolicyText.trim();
                if (!raw) {
                    payload.ai_system_gate_policy = null;
                } else {
                    try {
                        payload.ai_system_gate_policy = JSON.parse(raw);
                    } catch (err) {
                        setGateJsonError(
                            err instanceof Error ? err.message : 'Invalid JSON',
                        );
                        setSaving(false);
                        return;
                    }
                }
            }

            const updated = await gatewayClient.updateCurrentTenantSettings(payload);

            toast.success('Tenant settings saved', 'Your changes are live.');
            setTenant(updated as TenantRecord);
            setSectionStored(sectionValues);
            setConfirmModelSwitch(false);
            // Re-seed from the round-trip so the editor reflects
            // server-side validation (e.g. region tokens lowercased).
            setResidency((updated as TenantRecord).data_residency ?? null);
            setResidencyTouched(false);
            setRetention((updated as TenantRecord).data_retention ?? null);
            setRetentionTouched(false);
            // The policy the server now enforces is what the selector shows.
            setForm((current) => ({ ...current, mfa_policy: mfaPolicy((updated as TenantRecord).mfa_policy) }));
            setMfaTouched(false);
            setMfaError(null);
        } catch (err: any) {
            toast.error('Save failed', err?.message || 'Unknown error');
            // A refused two-factor policy is also said where it was chosen:
            // the toast goes away, the reason should not.
            if (mfaTouched && errorStatus(err) === 409) {
                setMfaError(errorMessage(err, 'The two-factor policy was not changed.'));
            }
            setConfirmModelSwitch(false);
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center py-16">
                <Loader className="w-6 h-6 animate-spin text-primary-500" />
            </div>
        );
    }

    if (!tenant) {
        return (
            <div className="p-6 text-center text-secondary">
                Unable to load tenant settings.
            </div>
        );
    }

    const activeTabMeta = subTabs.find((t) => t.id === activeSubTab) ?? subTabs[0];

    // ── Section renderers ─────────────────────────────────────────────────

    const renderIdentity = () => (
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm">
            <ReadOnlyField label="Tenant ID" value={tenant.id} mono />
            <ReadOnlyField label="Slug" value={tenant.name} mono />
            <ReadOnlyField
                label="Status"
                value={
                    tenant.suspended
                        ? `Suspended${tenant.suspension_reason ? ` — ${tenant.suspension_reason}` : ''}`
                        : tenant.is_active
                          ? 'Active'
                          : 'Inactive'
                }
            />
            <ReadOnlyField
                label="Trial"
                value={
                    tenant.is_trial
                        ? `Yes — ends ${tenant.trial_ends_at ? new Date(tenant.trial_ends_at).toLocaleDateString() : 'unknown'}`
                        : 'No'
                }
            />
            <ReadOnlyField
                label="Created"
                value={tenant.created_at ? new Date(tenant.created_at).toLocaleString() : '—'}
            />
            <ReadOnlyField
                label="Last active"
                value={
                    tenant.last_active_at
                        ? new Date(tenant.last_active_at).toLocaleString()
                        : '—'
                }
            />
        </dl>
    );

    const renderProfile = () => (
        <div className="space-y-4">
            <Field
                label="Display name"
                required
                value={form.display_name}
                onChange={(v) => setForm({ ...form, display_name: v })}
            />
            <Field
                label="Organization name"
                value={form.organization_name}
                onChange={(v) => setForm({ ...form, organization_name: v })}
            />
            <Field
                label="Domain"
                value={form.domain}
                placeholder="example.com"
                onChange={(v) => setForm({ ...form, domain: v })}
            />
            <Field
                label="Primary contact name"
                value={form.primary_contact_name}
                onChange={(v) => setForm({ ...form, primary_contact_name: v })}
            />
            <Field
                label="Primary contact email"
                type="email"
                required
                value={form.primary_contact_email}
                onChange={(v) => setForm({ ...form, primary_contact_email: v })}
            />
        </div>
    );

    const renderSecurity = () => {
        const tightening = mfaRank(form.mfa_policy) > mfaRank(tenant.mfa_policy);
        return (
            <div>
                <label htmlFor="mfa-policy" className="block text-sm font-medium text-default mb-1">
                    Two-factor authentication (2FA) policy
                </label>
                <select
                    id="mfa-policy"
                    data-testid="mfa-policy-select"
                    value={form.mfa_policy}
                    onChange={(e) => {
                        setForm({ ...form, mfa_policy: mfaPolicy(e.target.value) });
                        setMfaTouched(e.target.value !== (tenant.mfa_policy || 'optional'));
                        setMfaError(null);
                    }}
                    className="w-full px-3 py-2 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                >
                    {MFA_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                            {opt.label}
                        </option>
                    ))}
                </select>
                <p className="text-xs text-muted mt-1" data-testid="mfa-policy-stored">
                    In force now: <strong>{mfaLabel(tenant.mfa_policy)}</strong>
                </p>

                {mfaError && (
                    <div
                        role="alert"
                        data-testid="mfa-policy-error"
                        className="mt-3 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md text-sm text-red-700 dark:text-red-400"
                    >
                        {mfaError}
                    </div>
                )}

                {tightening && !mfaError && (
                    <div
                        data-testid="mfa-policy-tightening"
                        className="mt-3 p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-md text-sm text-amber-800 dark:text-amber-300"
                    >
                        Saving this makes the policy stricter. Everyone it newly covers who has not
                        set up 2FA is signed out and must set it up at their next sign-in.
                    </div>
                )}

                <div className="text-xs text-muted mt-3 space-y-2" data-testid="mfa-policy-help">
                    <ul className="list-disc pl-4 space-y-1">
                        <li data-testid="mfa-policy-help-optional">
                            <strong>Optional</strong>: nobody is required to use 2FA. Anyone can still
                            turn it on for their own account.
                        </li>
                        <li data-testid="mfa-policy-help-required_admins">
                            <strong>Required for administrators</strong>: every administrator of this
                            tenant must have 2FA to use the Admin Console.
                        </li>
                        <li data-testid="mfa-policy-help-required_all">
                            <strong>Required for everyone</strong>: administrators as above, and Portal
                            users must have 2FA to use the Portal.
                        </li>
                    </ul>
                    <p>
                        When you make the policy stricter, the people it newly covers who have no 2FA
                        are signed out (within about half a minute). At their next sign-in they
                        enter their password and must then set up 2FA, with an authenticator app,
                        before they get a session.
                    </p>
                    <p>
                        You can choose a required policy only after 2FA is on for your own account,
                        so that saving it does not sign you out. While the policy requires it, 2FA
                        cannot be disabled by an administrator.
                    </p>
                    <p>
                        Administrators always sign in to the Admin Console with a password; there
                        is no sign-in through an external identity provider for them, so none is
                        exempt. Portal users who sign in through an external identity provider
                        (single sign-on) are not asked to set up 2FA here: that sign-in relies on
                        the identity provider&apos;s own second factor.
                    </p>
                    <p>
                        System administrators belong to no tenant and are not covered by this policy.
                    </p>
                </div>
            </div>
        );
    };

    const renderJudge = () => (
        <div>
            <p className="text-xs text-muted mb-4">
                Tenant-wide model used by <strong>Semantic Governance Policies</strong>{' '}
                when a policy doesn&apos;t pin its own judge. An LLM judge evaluates
                each governed request against the policy&apos;s natural-language
                constraint. Leave unset to fall back to the tenant&apos;s first
                enabled chat model.
            </p>

            <label className="block text-sm font-medium text-default mb-1">
                Default judge model
            </label>
            <select
                value={form.default_judge_model_id}
                onChange={(e) =>
                    setForm({ ...form, default_judge_model_id: e.target.value })
                }
                className="w-full px-3 py-2 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary"
            >
                <option value="">— None (first enabled chat model) —</option>
                {judgeModels.map((m) => (
                    <option key={m.id} value={m.id}>
                        {m.model_name} ({m.provider})
                    </option>
                ))}
            </select>

            {form.default_judge_model_id &&
                !judgeModels.some((m) => m.id === form.default_judge_model_id) && (
                    <p className="mt-2 text-xs text-amber-700">
                        The selected judge is no longer an enabled chat model — it
                        will fall back to the first enabled chat model until changed.
                    </p>
                )}

            <p className="mt-3 text-xs text-muted">
                Tip: pin a fast, inexpensive chat model (e.g. a Haiku or mini) — the
                judge runs on every governed request a semantic policy applies to.
            </p>
        </div>
    );

    // The contributed model sections are drawn above this, outside the
    // sub-tab switch (`renderModelSections`); the judge model is divided
    // from them only when there are any.
    const renderDefaultModels = () => (
        <div className={modelSections.length > 0 ? 'pt-6 border-t border-default' : undefined}>
            <h3 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                <Brain className="w-4 h-4 text-primary" /> Judge model
            </h3>
            {renderJudge()}
        </div>
    );

    // Mounted for as long as the page is, and hidden off the Default Models
    // sub-tab: what a section has loaded, and what it has in flight, is still
    // there when the operator comes back to it.
    const renderModelSections = () =>
        modelSections.length > 0 && (
            <div hidden={activeSubTab !== 'embedding'} className="space-y-8 mb-8">
                {modelSections.map((section) => (
                    <div key={section.id}>
                        <h3 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                            <section.icon className="w-4 h-4 text-primary" /> {section.title}
                        </h3>
                        <section.Section
                            tenantId={tenant.id}
                            stored={getString(tenant, section.field) ?? ''}
                            models={sectionModels[section.field] ?? []}
                            value={sectionValues[section.field] ?? ''}
                            onChange={(modelId) => setSectionValues((values) => ({ ...values, [section.field]: modelId }))}
                            changed={sectionChanged(section.field)}
                            confirming={confirmModelSwitch}
                        />
                    </div>
                ))}
            </div>
        );

    // Per-tenant residency profile. Toggle the
    // JSONB on/off, edit `allowed_regions` (literal-compared by the
    // Rust runtime against each provider's `region` field), and the
    // `block_global` flag that refuses providers with region=NULL or
    // region="global" even when allowed_regions is empty.
    const renderRetention = () => {
        const enabled = retention !== null;
        const setPolicy = (next: DataRetentionPolicy | null) => {
            setRetention(next);
            setRetentionTouched(true);
        };
        const dayField = (
            key: 'body_days' | 'log_days' | 'run_days',
            label: string,
            help: string,
        ) => (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">{label}</label>
                <div className="flex items-center gap-2">
                    <input
                        type="number"
                        min={1}
                        max={3650}
                        value={retention?.[key] ?? ''}
                        onChange={(e) => {
                            if (!retention) return;
                            const v = e.target.value === '' ? null : Number(e.target.value);
                            setPolicy({ ...retention, [key]: v });
                        }}
                        placeholder="keep"
                        data-testid={`retention-${key}`}
                        className="w-32 px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    <span className="text-xs text-muted">days · blank = keep</span>
                </div>
                <p className="text-xs text-secondary mt-1">{help}</p>
            </div>
        );
        const bodyExceedsLog =
            !!retention?.body_days && !!retention?.log_days && retention.body_days > retention.log_days;

        return (
            <div className="space-y-6" data-testid="retention-tab">
                <div className="flex items-start gap-2 bg-indigo-50 border border-indigo-200 rounded-md p-3">
                    <Info className="w-4 h-4 mt-0.5 text-indigo-600 flex-shrink-0" />
                    <div className="text-sm text-indigo-900">
                        <p className="font-medium">What the ledger stores, and for how long</p>
                        <p className="text-xs mt-1 text-indigo-800">
                            Every governed call writes a log row with two layers: the{' '}
                            <em>record</em> (who called what, what was decided — covered by
                            the audit hash chain) and the <em>content</em> (prompts, tool
                            arguments, model output — not hashed, so it can be redacted,
                            removed or erased without breaking verification). This policy
                            governs the content and the windows. Deletions are declared to
                            the chain verifier, which reports them as{' '}
                            <code className="bg-white px-1 rounded">pruned</code> rather than{' '}
                            <code className="bg-white px-1 rounded">gap</code>.
                        </p>
                    </div>
                </div>

                <div>
                    <label className="flex items-center gap-3 cursor-pointer">
                        <input
                            type="checkbox"
                            checked={enabled}
                            onChange={(e) =>
                                setPolicy(
                                    e.target.checked
                                        ? { store_bodies: 'full', body_days: null, log_days: null, run_days: null }
                                        : null,
                                )
                            }
                            data-testid="retention-enabled"
                            className="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-strong rounded"
                        />
                        <div>
                            <span className="font-medium text-default">Set a retention policy</span>
                            <p className="text-xs text-secondary">
                                When off, bodies are stored in full and nothing is ever removed.
                            </p>
                        </div>
                    </label>
                </div>

                {enabled && retention && (
                    <div className="space-y-5 pl-7 border-l-2 border-indigo-100">
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                What the proxy stores for bodies
                            </label>
                            <select
                                value={retention.store_bodies}
                                onChange={(e) =>
                                    setPolicy({
                                        ...retention,
                                        store_bodies: e.target.value as DataRetentionPolicy['store_bodies'],
                                    })
                                }
                                data-testid="retention-store-bodies"
                                className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                            >
                                <option value="full">Full — as received, capped at 64 KiB</option>
                                <option value="redacted">
                                    Redacted — local PII redaction at write time, independent of guardrails
                                </option>
                                <option value="none">None — no body or header text stored</option>
                            </select>
                            <p className="text-xs text-secondary mt-1">
                                Applied in the proxy's audit writer for every surface. Takes effect
                                within a minute.
                            </p>
                        </div>

                        {dayField(
                            'body_days',
                            'Remove bodies after',
                            'Bodies and headers are removed from older rows; the rows, hashes and counters stay.',
                        )}
                        {dayField(
                            'log_days',
                            'Delete log rows after',
                            'Whole rows are deleted and the deletion is declared to the audit-chain verifier.',
                        )}
                        {dayField(
                            'run_days',
                            'Delete run-ledger rows after',
                            'Runs and their feedback are deleted. Baselines already learned are unaffected.',
                        )}
                        {bodyExceedsLog && (
                            <div className="flex items-start gap-2 text-sm text-red-700" data-testid="retention-window-error">
                                <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                <span>Bodies cannot be kept longer than the rows that hold them — body days must not exceed log days.</span>
                            </div>
                        )}
                    </div>
                )}

                <div className="text-xs text-secondary border-t border-default pt-4">
                    <p className="font-medium text-default">Erasing a data subject</p>
                    <p className="mt-1">
                        <code className="bg-surface-alt px-1 rounded">POST /v1/admin/data-erasure</code>{' '}
                        (permission <code className="bg-surface-alt px-1 rounded">data:erasure</code>) removes
                        stored bodies, headers and direct identifiers for one person and deletes their user
                        record, keeping the hashed record layer. Every request is logged as an erasure event.
                        See <em>https://docs.brutor.ai/governance/data-handling/</em>.
                    </p>
                </div>
            </div>
        );
    };

    const renderResidency = () => {
        const enabled = residency !== null;
        const setProfile = (next: DataResidencyProfile | null) => {
            setResidency(next);
            setResidencyTouched(true);
        };

        return (
            <div className="space-y-6">
                <div className="flex items-start gap-2 bg-indigo-50 border border-indigo-200 rounded-md p-3">
                    <Info className="w-4 h-4 mt-0.5 text-indigo-600 flex-shrink-0" />
                    <div className="text-sm text-indigo-900">
                        <p className="font-medium">How residency enforcement works</p>
                        <p className="text-xs mt-1 text-indigo-800">
                            Every LLM and MCP request for this tenant is
                            checked against the regions listed below. If
                            the resolved provider / server's region isn't
                            in the allow-list, the proxy returns{' '}
                            <code className="bg-white px-1 rounded">403 region_not_allowed</code>{' '}
                            before reaching the upstream — the audit row
                            stamps{' '}
                            <code className="bg-white px-1 rounded">residency_class=blocked</code>.
                            With enforcement disabled the audit row stamps{' '}
                            <code className="bg-white px-1 rounded">not_evaluated</code>.
                        </p>
                    </div>
                </div>

                {/* Enforcement on/off */}
                <div>
                    <label className="flex items-center gap-3 cursor-pointer">
                        <input
                            type="checkbox"
                            checked={enabled}
                            onChange={(e) =>
                                setProfile(
                                    e.target.checked
                                        ? { allowed_regions: [], block_global: false }
                                        : null,
                                )
                            }
                            className="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-strong rounded"
                        />
                        <div>
                            <span className="font-medium text-default">
                                Enable data residency enforcement
                            </span>
                            <p className="text-xs text-secondary">
                                When off, providers and servers can serve
                                this tenant from any region.
                            </p>
                        </div>
                    </label>
                </div>

                {enabled && residency && (
                    <div className="space-y-4 pl-7 border-l-2 border-indigo-100">
                        {/* Allowed regions list */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-2">
                                Allowed regions
                            </label>
                            <div className="space-y-2">
                                {residency.allowed_regions.map((r, idx) => (
                                    <div key={idx} className="flex items-center gap-2">
                                        <input
                                            type="text"
                                            value={r}
                                            onChange={(e) => {
                                                const next = [...residency.allowed_regions];
                                                next[idx] = e.target.value;
                                                setProfile({ ...residency, allowed_regions: next });
                                            }}
                                            placeholder="eu-west-1"
                                            className="flex-1 px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono min-h-0 min-w-0"
                                        />
                                        <button
                                            type="button"
                                            onClick={() => {
                                                const next = residency.allowed_regions.filter(
                                                    (_, i) => i !== idx,
                                                );
                                                setProfile({ ...residency, allowed_regions: next });
                                            }}
                                            className="p-2 text-muted hover:text-red-600 hover:bg-red-50 rounded-md transition-colors"
                                            aria-label="Remove region"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                ))}
                                <button
                                    type="button"
                                    onClick={() =>
                                        setProfile({
                                            ...residency,
                                            allowed_regions: [...residency.allowed_regions, ''],
                                        })
                                    }
                                    className="flex items-center gap-2 text-sm text-indigo-600 hover:text-indigo-700"
                                >
                                    <Plus className="w-4 h-4" />
                                    Add region
                                </button>
                            </div>
                            <p className="text-xs text-muted mt-2">
                                Region strings must match what's stamped on each LLM
                                provider and MCP server (lowercase alnum plus hyphen /
                                underscore — e.g.{' '}
                                <code className="bg-surface-alt px-1 rounded">eu-west-1</code>).
                                Empty list with <span className="font-medium">Block global / unspecified</span> off
                                = no constraint (functionally same as disabling enforcement).
                            </p>
                        </div>

                        {/* block_global toggle */}
                        <div>
                            <label className="flex items-start gap-3 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={residency.block_global}
                                    onChange={(e) =>
                                        setProfile({
                                            ...residency,
                                            block_global: e.target.checked,
                                        })
                                    }
                                    className="mt-0.5 h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-strong rounded"
                                />
                                <div>
                                    <span className="font-medium text-default">
                                        Block global / unspecified region
                                    </span>
                                    <p className="text-xs text-secondary mt-0.5">
                                        Refuse providers whose region is{' '}
                                        <code className="bg-surface-alt px-1 rounded">global</code>{' '}
                                        or unset, even if the allow-list is empty.
                                        Use for tenants under strict regimes (e.g.
                                        GDPR with no cross-border transfer) where
                                        no-region-set is considered an unsafe default.
                                    </p>
                                </div>
                            </label>
                        </div>
                    </div>
                )}
            </div>
        );
    };

    const renderAssurance = () => {
        const parsed = captureHours.trim() ? Number(captureHours) : null;
        const invalid = parsed !== null && (!Number.isInteger(parsed) || parsed < 1 || parsed > 8760);
        const active = tenant.assurance_capture_interval_hours != null && !captureTouched;
        const approvalParsed = approvalHours.trim() ? Math.round(Number(approvalHours) * 3600) : null;
        const approvalInvalid =
            approvalParsed !== null &&
            (!Number.isFinite(approvalParsed) || approvalParsed < 60 || approvalParsed > 604800);

        return (
            <div className="space-y-4 max-w-2xl">
                <div className="space-y-2">
                    <label className="block text-sm font-medium text-secondary">
                        Automatic assurance capture
                    </label>
                    <div className="flex items-center gap-2">
                        <input
                            type="number"
                            min={1}
                            max={8760}
                            value={captureHours}
                            placeholder="Off"
                            onChange={(e) => {
                                setCaptureHours(e.target.value);
                                setCaptureTouched(true);
                            }}
                            className="w-32 px-3 py-2 text-sm border border-strong rounded focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                        />
                        <span className="text-sm text-secondary">hours between captures</span>
                    </div>
                    {invalid && (
                        <p className="text-xs text-red-600">
                            Must be a whole number of hours between 1 and 8760 (one year).
                        </p>
                    )}
                    <p className="text-xs text-secondary">
                        An assurance report is an immutable snapshot of one AI
                        System&rsquo;s evidence — runs, drift, liveness, contract and
                        conformance — captured as it stood. Leave this empty and
                        reports are only ever produced when somebody opens the
                        console and asks for one. A snapshot taken on a schedule
                        by a machine is worth more at audit time than a thorough
                        one taken twice a year by a person.
                    </p>
                    <p className="text-xs text-secondary">
                        Health is refreshed on every scheduler tick regardless of
                        this setting; the interval controls only how often a
                        report is <em>captured</em>. Applies to every active AI
                        System in this tenant, so expect roughly one stored
                        report per system per interval.
                    </p>
                </div>

                {active && (
                    <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
                        <ShieldCheck className="inline-block h-3.5 w-3.5 mr-1" />
                        Scheduled capture is on — every{' '}
                        <strong>{tenant.assurance_capture_interval_hours}h</strong>{' '}
                        per active AI System.
                    </div>
                )}
                {!active && !captureTouched && (
                    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                        Scheduled capture is <strong>off</strong>. Assurance
                        reports exist only when captured by hand.
                    </div>
                )}

                <div className="pt-4 border-t border-default space-y-2">
                    <label htmlFor="tenant-approval-window" className="block text-sm font-medium text-secondary">
                        Default approval window
                    </label>
                    <div className="flex items-center gap-2">
                        <input
                            id="tenant-approval-window"
                            type="number"
                            min={0.02}
                            max={168}
                            step="any"
                            value={approvalHours}
                            placeholder="0.083 (5 min)"
                            onChange={(e) => {
                                setApprovalHours(e.target.value);
                                setApprovalTouched(true);
                            }}
                            className="w-32 px-3 py-2 text-sm border border-strong rounded focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                        />
                        <span className="text-sm text-secondary">hours a human has to decide a hold</span>
                    </div>
                    {approvalInvalid && (
                        <p className="text-xs text-red-600">Must be between 1 minute and 168 hours (7 days).</p>
                    )}
                    <p className="text-xs text-secondary">
                        Applies to holds whose policy names no window of its own and whose Resource
                        Group chain sets none (Governance &rarr; Approvals on a group). Leave empty for
                        the platform default of 5 minutes &mdash; too short for anyone who approves
                        overnight.
                    </p>
                </div>

                <div className="pt-4 border-t border-default space-y-2">
                    <label className="block text-sm font-medium text-secondary">
                        Lifecycle gate policy
                    </label>
                    <p className="text-xs text-secondary">
                        Which evidence each lifecycle transition demands before
                        an AI System may move stage. Leave empty to use the
                        built-in requirements. A tenant may tighten or relax any
                        transition, except for the requirements the gate always re-adds, below.
                    </p>
                    <textarea
                        value={gatePolicyText}
                        placeholder={'{\n  "proposed->approved": ["contract_minted", "owner_set"]\n}'}
                        rows={7}
                        spellCheck={false}
                        onChange={(e) => {
                            setGatePolicyText(e.target.value);
                            setGatePolicyTouched(true);
                            setGateJsonError(null);
                        }}
                        className="w-full px-3 py-2 text-xs font-mono border border-strong rounded focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                    />
                    {gateJsonError && (
                        <p className="text-xs text-red-600">
                            Invalid JSON — {gateJsonError}
                        </p>
                    )}

                    <ReadFailureNotice compact failure={readFailure('vocabulary')} onRetry={asyncHandler(loadData, 'tenant settings', 'background')} />
                    {gateVocab && (
                        <div className="rounded-md border border-default bg-surface-subtle p-3 space-y-2">
                            <div className="text-[11px] font-medium text-secondary">
                                Transitions and their defaults
                            </div>
                            <ul className="space-y-0.5">
                                {gateVocab.transitions?.map((t: any) => (
                                    <li key={t.key} className="text-[11px] font-mono text-muted">
                                        {t.key}
                                        {t.default_requirements?.length
                                            ? ` — ${t.default_requirements.join(', ')}`
                                            : ' — no requirements'}
                                    </li>
                                ))}
                            </ul>
                            <div className="text-[11px] font-medium text-secondary pt-1">
                                Requirement keys
                            </div>
                            <ul className="space-y-0.5">
                                {gateVocab.requirements?.map((r: any) => (
                                    <li key={r.key} className="text-[11px] text-muted">
                                        <span className="font-mono">{r.key}</span> — {r.label}
                                    </li>
                                ))}
                            </ul>
                            {gateFloorsOf(gateVocab).map((floor) => (
                                <p key={floor.when} className="text-[11px] text-amber-700 dark:text-amber-400 pt-1" data-testid="gate-immutable-floor">
                                    <AlertTriangle className="inline-block h-3 w-3 mr-1" />
                                    {floor.when},{' '}
                                    <span className="font-mono">{floor.alwaysRequired.join(', ')}</span>{' '}
                                    is re-added by the gate even if your policy omits it. You can relax the rest; you cannot relax that.
                                </p>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        );
    };

    const renderActiveSubTab = () => {
        switch (activeSubTab) {
            case 'identity':
                return renderIdentity();
            case 'profile':
                return renderProfile();
            case 'security':
                return renderSecurity();
            case 'embedding':
                return renderDefaultModels();
            case 'residency':
                return renderResidency();
            case 'retention':
                return renderRetention();
            case 'assurance':
                return renderAssurance();
            case 'evidence':
                return <TenantEvidencePanel />;
            case 'workload-identity':
                return <TenantIdentityDefaultPanel tenant={tenant} onSaved={(value) => setTenant({ ...tenant, identity_policy_default: value } as TenantRecord)} />;
            default:
                return contributed.get(activeSubTab)?.render({ tenant });
        }
    };

    // Only editable sub-tabs show the save bar — read-only sections have
    // nothing to save, so hiding it avoids a confusing always-enabled button.
    const showSaveBar = activeTabMeta.editable;

    return (
        <div className="app-column h-full min-h-0">
            {/* Sub-tab strip */}
            <div className="flex shrink-0 border-b border-default bg-surface-alt -mx-6 px-6 overflow-x-auto">
                {subTabs.map(({ id, label, icon: Icon }) => {
                    const active = activeSubTab === id;
                    return (
                        <button
                            key={id}
                            onClick={() => {
                                setActiveSubTab(id);
                                setConfirmModelSwitch(false);
                            }}
                            className={`flex items-center gap-2 px-4 py-2.5 border-b-2 text-sm font-medium transition-colors whitespace-nowrap ${
                                active
                                    ? 'border-primary-500 text-primary-600'
                                    : 'border-transparent text-secondary hover:text-default hover:border-strong'
                            }`}
                        >
                            <Icon className="w-4 h-4" />
                            <span>{label}</span>
                        </button>
                    );
                })}
            </div>

            {/* Active sub-tab body — the sub-tab strip and save bar stay put while
                this scrolls, so long sub-tabs (Retention, Quotas, Features) are
                reachable. Required: the app shell clips at `.h-full.flex` and
                gives every `.flex-1` `min-height: 0`, so without a scroll
                container here the overflow is silently cut off. */}
            <div className="scroll-region min-h-0 -mx-6 px-6 py-6">
                <section className="bg-surface border border-default rounded-lg p-5 max-w-3xl">
                    <div className="flex items-center gap-2 mb-4">
                        <activeTabMeta.icon className="w-5 h-5 text-primary-500" />
                        <h3 className="text-base font-semibold text-default">
                            {activeTabMeta.label}
                        </h3>
                        {!activeTabMeta.editable && !activeTabMeta.savesItself && (
                            <span
                                className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium text-muted bg-surface-alt rounded"
                                title="Read-only — set by the platform operator"
                            >
                                <Lock className="w-2.5 h-2.5" />
                                read-only
                            </span>
                        )}
                    </div>
                    {renderModelSections()}
                    {renderActiveSubTab()}
                </section>
            </div>

            {/* Save bar — pinned below the scrolling body, only on editable sub-tabs */}
            {showSaveBar && (
                <div className="shrink-0 bg-surface border-t border-default py-3 -mx-6 px-6 flex items-center justify-end gap-3">
                    <button
                        onClick={asyncHandler(handleSave, 'save')}
                        disabled={saving}
                        className="inline-flex items-center gap-2 px-4 py-2 bg-primary-500 text-white text-sm font-medium rounded-md hover:bg-primary-600 disabled:opacity-50"
                    >
                        {saving ? (
                            <Loader className="w-4 h-4 animate-spin" />
                        ) : (
                            <Save className="w-4 h-4" />
                        )}
                        {modelChanged && !confirmModelSwitch ? 'Review changes…' : 'Save changes'}
                    </button>
                </div>
            )}
        </div>
    );
};

// ────────────────────────────────────────────────────────────────────────────
// Small presentational helpers
// ────────────────────────────────────────────────────────────────────────────

const Field: React.FC<{
    label: string;
    value: string;
    onChange: (v: string) => void;
    required?: boolean;
    placeholder?: string;
    type?: string;
}> = ({ label, value, onChange, required, placeholder, type = 'text' }) => (
    <div>
        <label className="block text-sm font-medium text-default mb-1">
            {label}
            {required && <span className="text-red-500 ml-0.5">*</span>}
        </label>
        <input
            type={type}
            value={value}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
            className="w-full px-3 py-2 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary"
        />
    </div>
);

const ReadOnlyField: React.FC<{ label: string; value: string; mono?: boolean }> = ({
    label,
    value,
    mono,
}) => (
    <div>
        <dt className="text-xs text-muted uppercase tracking-wide">{label}</dt>
        <dd className={`text-sm text-default mt-0.5 ${mono ? 'font-mono' : ''}`}>
            {value || '—'}
        </dd>
    </div>
);

/**
 * Body content for a read-only sub-tab. The sub-tab header already provides
 * the icon + title + lock badge, so this helper only renders the
 * description blurb and the key/value grid.
 */
export default TenantSettingsTab;
