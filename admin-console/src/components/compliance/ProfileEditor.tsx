/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The compliance profile editor for one AI System.
 *
 * A profile is a framework-neutral `core` — who operates the system, in
 * which jurisdictions, what it touches and how it behaves — plus a
 * `frameworks` block keyed by registry id with the facts that framework
 * needs (the EU AI Act's role, risk tier, annex and conformity route; a
 * key/value block for any framework without a dedicated form yet).
 *
 * The editor edits ONE framework block at a time (the switcher's) and
 * carries the others through untouched on save, so switching frameworks
 * never loses a declaration. Every save is a declaration: when the core
 * has sealed it, `declaration_record_id` names the record.
 */

import { useCallback, useMemo, useState } from 'react';
import { Loader2, Plus, Save, Trash2 } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { useToast } from '../common/Toast';
import { errorMessage, getArray, getBoolean, getRecord, getString, isRecord } from '../../lib/narrow';
import {
    CORE_BOOLEAN_FIELDS,
    EU_ANNEX_III_AREA_LABEL,
    EU_ANNEX_III_AREAS,
    EU_ANNEXES,
    EU_CONFORMITY_ASSESSMENTS,
    EU_RISK_TIERS,
    OPERATOR_ROLE_LABEL,
    OPERATOR_ROLES,
    PROFILE_DATE_FIELDS,
    PROFILE_ENTITY_FIELDS,
    SENSITIVITY_FLAGS,
    type AISystemComplianceProfile,
    type ComplianceFramework,
    type ExternalRegistration,
    type OperatorRole,
    type ProfileCoreForm,
} from '../../lib/complianceEvidenceTypes';
import { EU_AI_ACT_ROLE_LABEL } from '../../lib/obligationTypes';
import { classificationFieldFor, roleFieldFor } from '../../lib/complianceEvidenceTypes';
import { RecordLink } from './ComplianceUi';
import { useFrameworkCatalog } from './useFrameworkCatalog';

interface ProfileEditorProps {
    aiSystemId: string;
    /** The framework whose facts block is being edited. */
    framework: string;
    frameworks: ComplianceFramework[];
    onSaved?: (profile: AISystemComplianceProfile) => void;
    /** Open a sealed record (the declaration) in the record dialog. */
    onOpenRecord?: (recordId: string) => void;
}

const EU_AI_ACT = 'eu-ai-act';

// ─── Wire ↔ form ─────────────────────────────────────────────────────────────

function coreFromWire(core: unknown): ProfileCoreForm {
    const entities: ProfileCoreForm['entities'] = {};
    for (const { key } of PROFILE_ENTITY_FIELDS) {
        const v = getString(getRecord(core, 'entities'), key);
        if (v) entities[key] = v;
    }
    const dates: ProfileCoreForm['dates'] = {};
    for (const { key } of PROFILE_DATE_FIELDS) {
        const v = getString(getRecord(core, 'dates'), key);
        if (v) dates[key] = v;
    }
    const sensitivity: ProfileCoreForm['sensitivity'] = {};
    for (const { key } of SENSITIVITY_FLAGS) {
        const v = getBoolean(getRecord(core, 'sensitivity'), key);
        if (v !== undefined) sensitivity[key] = v;
    }
    const flags: ProfileCoreForm['flags'] = {};
    for (const { key } of CORE_BOOLEAN_FIELDS) {
        const v = getBoolean(core, key);
        if (v !== undefined) flags[key] = v;
    }
    const registrations: ExternalRegistration[] = [];
    for (const r of getArray(core, 'external_registrations') ?? []) {
        registrations.push({
            framework_id: getString(r, 'framework_id') ?? '',
            registry: getString(r, 'registry') ?? '',
            id: getString(r, 'id') ?? '',
        });
    }
    const role = getString(core, 'operator_role');
    return {
        operator_role: OPERATOR_ROLES.includes(role as OperatorRole) ? (role as OperatorRole) : '',
        jurisdictions: (getArray(core, 'jurisdictions') ?? []).filter((j): j is string => typeof j === 'string'),
        entities,
        dates,
        external_registrations: registrations,
        sensitivity,
        flags,
        oversight_assignment: getString(core, 'oversight_assignment') ?? '',
        synthetic_marking: getString(core, 'synthetic_marking') ?? '',
        notes: getString(core, 'notes') ?? '',
    };
}

/** Strip empties so an untouched profile round-trips as `{}`. */
function coreToWire(form: ProfileCoreForm): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    if (form.operator_role) out.operator_role = form.operator_role;
    const jurisdictions = form.jurisdictions.map((j) => j.trim()).filter(Boolean);
    if (jurisdictions.length) out.jurisdictions = jurisdictions;
    const entities = Object.fromEntries(Object.entries(form.entities).filter(([, v]) => v && v.trim()));
    if (Object.keys(entities).length) out.entities = entities;
    const dates = Object.fromEntries(Object.entries(form.dates).filter(([, v]) => v));
    if (Object.keys(dates).length) out.dates = dates;
    const registrations = form.external_registrations.filter((r) => r.framework_id || r.registry || r.id);
    if (registrations.length) out.external_registrations = registrations;
    const sensitivity = Object.fromEntries(Object.entries(form.sensitivity).filter(([, v]) => v !== undefined));
    if (Object.keys(sensitivity).length) out.sensitivity = sensitivity;
    for (const [k, v] of Object.entries(form.flags)) {
        if (v !== undefined) out[k] = v;
    }
    if (form.oversight_assignment.trim()) out.oversight_assignment = form.oversight_assignment.trim();
    if (form.synthetic_marking.trim()) out.synthetic_marking = form.synthetic_marking.trim();
    if (form.notes.trim()) out.notes = form.notes.trim();
    return out;
}

type FrameworkBlock = Record<string, unknown>;

interface Draft {
    core: ProfileCoreForm;
    block: FrameworkBlock;
    jurisdictionsText: string;
}

function blockFromWire(frameworks: unknown, id: string): FrameworkBlock {
    const block = getRecord(frameworks, id);
    return block ? { ...block } : {};
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function ProfileEditor({ aiSystemId, framework, frameworks, onSaved, onOpenRecord }: ProfileEditorProps) {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const [saving, setSaving] = useState(false);

    const registryEntry = frameworks.find((f) => f.id === framework);
    const { data: catalog } = useFrameworkCatalog(framework, (registryEntry?.obligation_count ?? 0) > 0);
    const roleField = roleFieldFor(framework);
    const classificationField = classificationFieldFor(framework);
    const roles = catalog?.roles ?? [];
    const classifications = catalog?.classifications ?? [];

    const loader = useCallback(() => gatewayClient.getAISystemComplianceProfile(aiSystemId), [gatewayClient, aiSystemId]);
    const { data: profile, error, loading, setData } = useKeyedLoad(aiSystemId, loader, 'Failed to load the compliance profile');

    // The form is an OVERLAY on the loaded profile: `base` is what the
    // server holds, `draft` is what the operator has changed. Nothing is
    // copied into state when the profile arrives — the first edit forks the
    // base — so a re-fetch never has to be reconciled with a half-edited
    // form, and "dirty" is simply "there is a draft".
    const base = useMemo<Draft | null>(() => {
        if (!profile) return null;
        const core = coreFromWire(profile.core);
        return { core, block: blockFromWire(profile.frameworks, framework), jurisdictionsText: core.jurisdictions.join(', ') };
    }, [profile, framework]);
    const [draft, setDraft] = useState<Draft | null>(null);
    const current = draft ?? base;
    const dirty = draft !== null;

    const edit = (fn: (prev: Draft) => Draft) => {
        setDraft((prev) => {
            const from = prev ?? base;
            return from ? fn(from) : prev;
        });
    };
    const updateCore = (patch: Partial<ProfileCoreForm>) => edit((d) => ({ ...d, core: { ...d.core, ...patch } }));
    const updateBlock = (key: string, value: unknown) =>
        edit((d) => {
            const block = { ...d.block };
            if (value === '' || value === undefined || value === null) delete block[key];
            else block[key] = value;
            return { ...d, block };
        });
    /** Replace the free key/value facts, keeping the managed keys (role,
     *  classification, in_scope) and every untouched value's own type — a
     *  boolean stays a boolean, not the string "true". */
    const setBlockExtras = (entries: Array<[string, string]>, managed: string[]) =>
        edit((d) => {
            const block: FrameworkBlock = {};
            for (const k of managed) if (k in d.block) block[k] = d.block[k];
            for (const [k, v] of entries) {
                if (!k.trim()) continue;
                const prev = d.block[k];
                block[k] = prev !== undefined && typeof prev !== 'string' && JSON.stringify(prev) === v ? prev : v;
            }
            return { ...d, block };
        });
    const setJurisdictionsText = (text: string) => edit((d) => ({ ...d, jurisdictionsText: text }));

    const save = async () => {
        if (!profile || !current) return;
        setSaving(true);
        try {
            const others = isRecord(profile.frameworks) ? { ...profile.frameworks } : {};
            const cleanedBlock = Object.fromEntries(
                Object.entries(current.block).filter(([, v]) => v !== '' && v !== undefined && v !== null),
            );
            const saved = await gatewayClient.putAISystemComplianceProfile(aiSystemId, {
                core: coreToWire({ ...current.core, jurisdictions: current.jurisdictionsText.split(',') }),
                frameworks: { ...others, [framework]: cleanedBlock },
            });
            setData(() => saved);
            setDraft(null);
            if (saved.sealed) toast.success('Compliance profile declared and sealed');
            else toast.error(`Profile saved but not sealed${saved.seal_error ? ` (${saved.seal_error})` : ''}`);
            onSaved?.(saved);
        } catch (e: unknown) {
            toast.error(`Save failed: ${errorMessage(e)}`);
        } finally {
            setSaving(false);
        }
    };

    const blockKeyValues = useMemo(
        () =>
            Object.entries(current?.block ?? {}).map(
                ([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)] as [string, string],
            ),
        [current],
    );

    if (loading && !profile) {
        return (
            <div className="flex items-center gap-2 py-8 justify-center text-muted text-sm" data-testid="profile-loading">
                <Loader2 className="w-4 h-4 animate-spin" /> Loading profile…
            </div>
        );
    }
    if (error) {
        return (
            <p className="p-3 rounded-lg border border-red-300 bg-red-50 text-sm text-red-800" data-testid="profile-error">
                {error}
            </p>
        );
    }
    if (!profile || !current) return null;
    const { core, block, jurisdictionsText } = current;

    return (
        <div className="space-y-4 text-sm" data-testid="profile-editor">
            <div className="flex items-start justify-between gap-3 flex-wrap">
                <div>
                    <h3 className="text-sm font-semibold text-default">Compliance profile</h3>
                    <p className="text-xs text-muted">
                        {profile.exists ? (
                            <>
                                Declared{profile.updated_at ? ` ${new Date(profile.updated_at).toLocaleString()}` : ''}
                                {profile.updated_by ? ` by ${profile.updated_by}` : ''}.
                            </>
                        ) : (
                            'Nothing declared yet — the EU AI Act block is prefilled from the group.'
                        )}
                    </p>
                    {profile.declaration_record_id ? (
                        <p className="text-xs text-secondary mt-1" data-testid="profile-declaration-record">
                            Sealed declaration{' '}
                            {onOpenRecord ? (
                                <RecordLink recordId={profile.declaration_record_id} onOpen={onOpenRecord} />
                            ) : (
                                <code className="font-mono">{profile.declaration_record_id}</code>
                            )}
                        </p>
                    ) : (
                        profile.exists && (
                            <p className="text-xs text-amber-800 mt-1" data-testid="profile-not-sealed">
                                Declared but not sealed{profile.seal_error ? ` (${profile.seal_error})` : ''} — a DECLARED
                                statement has no sealed declaration to cite until the next save seals.
                            </p>
                        )
                    )}
                </div>
                <button
                    type="button"
                    onClick={() => void save()}
                    disabled={!dirty || saving}
                    className="inline-flex items-center gap-2 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                    data-testid="profile-save"
                >
                    {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    Declare
                </button>
            </div>

            {/* Core */}
            <fieldset className="rounded-lg border border-default p-3 space-y-3" data-testid="profile-core">
                <legend className="px-1 text-xs font-semibold text-muted uppercase tracking-wide">Core</legend>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="block">
                        <span className="text-xs text-muted">Operator role</span>
                        <select
                            value={core.operator_role}
                            onChange={(e) => updateCore({ operator_role: e.target.value as OperatorRole | '' })}
                            className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                            data-testid="profile-operator-role"
                        >
                            <option value="">— not declared —</option>
                            {OPERATOR_ROLES.map((r) => (
                                <option key={r} value={r}>
                                    {OPERATOR_ROLE_LABEL[r]}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="block">
                        <span className="text-xs text-muted">Jurisdictions (comma-separated)</span>
                        <input
                            type="text"
                            value={jurisdictionsText}
                            onChange={(e) => setJurisdictionsText(e.target.value)}
                            placeholder="EU, UK, US-CA"
                            className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                            data-testid="profile-jurisdictions"
                        />
                    </label>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    {PROFILE_ENTITY_FIELDS.map(({ key, label }) => (
                        <label key={key} className="block">
                            <span className="text-xs text-muted">{label}</span>
                            <input
                                type="text"
                                value={core.entities[key] ?? ''}
                                onChange={(e) => updateCore({ entities: { ...core.entities, [key]: e.target.value } })}
                                className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                                data-testid={`profile-entity-${key}`}
                            />
                        </label>
                    ))}
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    {PROFILE_DATE_FIELDS.map(({ key, label }) => (
                        <label key={key} className="block">
                            <span className="text-xs text-muted">{label}</span>
                            <input
                                type="date"
                                value={core.dates[key] ?? ''}
                                onChange={(e) => updateCore({ dates: { ...core.dates, [key]: e.target.value } })}
                                className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                                data-testid={`profile-date-${key}`}
                            />
                        </label>
                    ))}
                </div>

                <div>
                    <span className="text-xs text-muted">Sensitivity</span>
                    <div className="mt-1 flex flex-wrap gap-3">
                        {SENSITIVITY_FLAGS.map(({ key, label }) => (
                            <label key={key} className="inline-flex items-center gap-1.5 text-xs text-default">
                                <input
                                    type="checkbox"
                                    checked={core.sensitivity[key] === true}
                                    onChange={(e) =>
                                        updateCore({ sensitivity: { ...core.sensitivity, [key]: e.target.checked } })
                                    }
                                    className="h-4 w-4 rounded border-gray-300"
                                    data-testid={`profile-sensitivity-${key}`}
                                />
                                {label}
                            </label>
                        ))}
                    </div>
                </div>

                <div>
                    <span className="text-xs text-muted">Behaviour</span>
                    <div className="mt-1 grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                        {CORE_BOOLEAN_FIELDS.map(({ key, label, help }) => (
                            <label key={key} className="inline-flex items-start gap-1.5 text-xs text-default" title={help}>
                                <input
                                    type="checkbox"
                                    checked={core.flags[key] === true}
                                    onChange={(e) => updateCore({ flags: { ...core.flags, [key]: e.target.checked } })}
                                    className="mt-0.5 h-4 w-4 rounded border-gray-300"
                                    data-testid={`profile-flag-${key}`}
                                />
                                {label}
                            </label>
                        ))}
                    </div>
                </div>

                <div>
                    <div className="flex items-center justify-between">
                        <span className="text-xs text-muted">External registrations</span>
                        <button
                            type="button"
                            className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline"
                            onClick={() =>
                                updateCore({
                                    external_registrations: [
                                        ...core.external_registrations,
                                        { framework_id: framework, registry: '', id: '' },
                                    ],
                                })
                            }
                            data-testid="profile-add-registration"
                        >
                            <Plus className="w-3 h-3" /> Add
                        </button>
                    </div>
                    {core.external_registrations.map((r, i) => (
                        <div key={i} className="mt-1 grid grid-cols-[1fr_1fr_1fr_auto] gap-2" data-testid={`profile-registration-${i}`}>
                            <input
                                type="text"
                                value={r.framework_id}
                                placeholder="framework id"
                                onChange={(e) => {
                                    const next = [...core.external_registrations];
                                    next[i] = { ...r, framework_id: e.target.value };
                                    updateCore({ external_registrations: next });
                                }}
                                className="rounded-md border border-default bg-surface px-2 py-1 text-xs"
                            />
                            <input
                                type="text"
                                value={r.registry}
                                placeholder="registry"
                                onChange={(e) => {
                                    const next = [...core.external_registrations];
                                    next[i] = { ...r, registry: e.target.value };
                                    updateCore({ external_registrations: next });
                                }}
                                className="rounded-md border border-default bg-surface px-2 py-1 text-xs"
                            />
                            <input
                                type="text"
                                value={r.id}
                                placeholder="registration id"
                                onChange={(e) => {
                                    const next = [...core.external_registrations];
                                    next[i] = { ...r, id: e.target.value };
                                    updateCore({ external_registrations: next });
                                }}
                                className="rounded-md border border-default bg-surface px-2 py-1 text-xs"
                            />
                            <button
                                type="button"
                                aria-label="Remove registration"
                                className="p-1 text-muted hover:text-red-600"
                                onClick={() =>
                                    updateCore({ external_registrations: core.external_registrations.filter((_, j) => j !== i) })
                                }
                            >
                                <Trash2 className="w-3.5 h-3.5" />
                            </button>
                        </div>
                    ))}
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="block">
                        <span className="text-xs text-muted">Oversight assignment</span>
                        <input
                            type="text"
                            value={core.oversight_assignment}
                            onChange={(e) => updateCore({ oversight_assignment: e.target.value })}
                            placeholder="Role or team that can intervene"
                            className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                            data-testid="profile-oversight-assignment"
                        />
                    </label>
                    <label className="block">
                        <span className="text-xs text-muted">Synthetic-content marking approach</span>
                        <input
                            type="text"
                            value={core.synthetic_marking}
                            onChange={(e) => updateCore({ synthetic_marking: e.target.value })}
                            placeholder="e.g. UI label + X-Brutor-AI-Generated header"
                            className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                            data-testid="profile-synthetic-marking"
                        />
                    </label>
                </div>

                <label className="block">
                    <span className="text-xs text-muted">Notes</span>
                    <textarea
                        value={core.notes}
                        onChange={(e) => updateCore({ notes: e.target.value })}
                        rows={2}
                        className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                        data-testid="profile-notes"
                    />
                </label>
            </fieldset>

            {/* Per-framework facts */}
            <fieldset className="rounded-lg border border-default p-3 space-y-3" data-testid="profile-framework-block">
                <legend className="px-1 text-xs font-semibold text-muted uppercase tracking-wide">
                    {registryEntry?.title ?? framework} facts
                </legend>
                {framework === EU_AI_ACT ? (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <Select
                            label="Regulated role"
                            value={getString(block, 'role') ?? ''}
                            onChange={(v) => updateBlock('role', v)}
                            options={Object.entries(EU_AI_ACT_ROLE_LABEL)}
                            testId="profile-eu-role"
                        />
                        <Select
                            label="Risk tier"
                            value={getString(block, 'risk_tier') ?? ''}
                            onChange={(v) => updateBlock('risk_tier', v)}
                            options={EU_RISK_TIERS.map((t) => [t, t])}
                            testId="profile-eu-risk-tier"
                        />
                        <Select
                            label="Annex"
                            value={getString(block, 'annex') ?? ''}
                            onChange={(v) => updateBlock('annex', v)}
                            options={EU_ANNEXES.map((a) => [a, a.replace('_', ' ')])}
                            testId="profile-eu-annex"
                        />
                        <Select
                            label="Annex III area"
                            value={getString(block, 'annex_iii_area') ?? ''}
                            onChange={(v) => updateBlock('annex_iii_area', v)}
                            options={EU_ANNEX_III_AREAS.map((a) => [a, EU_ANNEX_III_AREA_LABEL[a]])}
                            testId="profile-eu-annex-iii-area"
                        />
                        <Select
                            label="Conformity assessment"
                            value={getString(block, 'conformity_assessment') ?? ''}
                            onChange={(v) => updateBlock('conformity_assessment', v)}
                            options={EU_CONFORMITY_ASSESSMENTS.map((c) => [c, c.replace('_', ' ')])}
                            testId="profile-eu-conformity"
                        />
                    </div>
                ) : (
                    <div className="space-y-3">
                        {(roles.length > 0 || classifications.length > 0) && (
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                {roles.length > 0 && (
                                    <Select
                                        label={`Role (${roleField})`}
                                        value={getString(block, roleField) ?? ''}
                                        onChange={(v) => updateBlock(roleField, v)}
                                        options={roles.map((r) => [r, r.replace(/_/g, ' ')])}
                                        testId="profile-fw-role"
                                    />
                                )}
                                {classifications.length > 0 && (
                                    <Select
                                        label={`Classification (${classificationField})`}
                                        value={getString(block, classificationField) ?? ''}
                                        onChange={(v) => updateBlock(classificationField, v)}
                                        options={classifications.map((c) => [c, c.replace(/_/g, ' ')])}
                                        testId="profile-fw-classification"
                                    />
                                )}
                            </div>
                        )}
                        <label className="inline-flex items-center gap-1.5 text-xs text-default">
                            <input
                                type="checkbox"
                                checked={getBoolean(block, 'in_scope') === true}
                                onChange={(e) => updateBlock('in_scope', e.target.checked ? true : undefined)}
                                className="h-4 w-4 rounded border-default"
                                data-testid="profile-fw-in-scope"
                            />
                            In scope of {registryEntry?.title ?? framework}
                        </label>
                        <KeyValueEditor
                            entries={blockKeyValues.filter(([k]) => k !== roleField && k !== classificationField && k !== 'in_scope')}
                            onChange={(entries) => setBlockExtras(entries, [roleField, classificationField, 'in_scope'])}
                        />
                    </div>
                )}
            </fieldset>
        </div>
    );
}

function Select({
    label,
    value,
    onChange,
    options,
    testId,
}: {
    label: string;
    value: string;
    onChange: (v: string) => void;
    options: Array<[string, string]>;
    testId: string;
}) {
    return (
        <label className="block">
            <span className="text-xs text-muted">{label}</span>
            <select
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm"
                data-testid={testId}
            >
                <option value="">— not declared —</option>
                {options.map(([v, l]) => (
                    <option key={v} value={v}>
                        {l}
                    </option>
                ))}
            </select>
        </label>
    );
}

function KeyValueEditor({
    entries,
    onChange,
}: {
    entries: Array<[string, string]>;
    onChange: (entries: Array<[string, string]>) => void;
}) {
    return (
        <div className="space-y-1.5" data-testid="profile-kv-editor">
            <p className="text-xs text-muted">
                Further facts this framework&apos;s declared statements cite, as key/value pairs (identifier keys,
                scalar values).
            </p>
            {entries.map(([k, v], i) => (
                <div key={i} className="grid grid-cols-[1fr_2fr_auto] gap-2">
                    <input
                        type="text"
                        value={k}
                        placeholder="key"
                        onChange={(e) => {
                            const next = entries.map((e2) => [...e2] as [string, string]);
                            next[i][0] = e.target.value;
                            onChange(next);
                        }}
                        className="rounded-md border border-default bg-surface px-2 py-1 text-xs font-mono"
                    />
                    <input
                        type="text"
                        value={v}
                        placeholder="value"
                        onChange={(e) => {
                            const next = entries.map((e2) => [...e2] as [string, string]);
                            next[i][1] = e.target.value;
                            onChange(next);
                        }}
                        className="rounded-md border border-default bg-surface px-2 py-1 text-xs"
                    />
                    <button
                        type="button"
                        aria-label="Remove fact"
                        className="p-1 text-muted hover:text-red-600"
                        onClick={() => onChange(entries.filter((_, j) => j !== i))}
                    >
                        <Trash2 className="w-3.5 h-3.5" />
                    </button>
                </div>
            ))}
            <button
                type="button"
                className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline"
                onClick={() => onChange([...entries, ['', '']])}
                data-testid="profile-kv-add"
            >
                <Plus className="w-3 h-3" /> Add fact
            </button>
        </div>
    );
}
