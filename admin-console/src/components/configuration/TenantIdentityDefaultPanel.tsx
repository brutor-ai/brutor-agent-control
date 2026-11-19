/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Tenant → Workload Identity: the tenant identity default.
 *
 * Every group's effective identity policy is resolved from the development
 * default, this default, the explicit policies down its hierarchy and the
 * production and high-risk floors; the stricter value of each field wins. So
 * this default can tighten what applies anywhere in the tenant, and never
 * lowers a floor. It also holds the personal-key ceiling.
 *
 * Writing it needs `identity_policy:manage` on top of the permission the
 * tenant update needs. It is saved alone, as its own field of the tenant
 * update, so a save here sends nothing else and a save elsewhere never
 * sends it.
 */

import React, { useMemo, useState } from 'react';
import { AlertCircle, Loader2, Save, Trash2 } from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useOidcIssuers } from '../../hooks/useOidcIssuers';
import { asyncHandler } from '../../lib/asyncSafe';
import { errorMessage } from '../../lib/narrow';
import {
    DEFAULT_USER_KEY_TTL_DAYS,
    DEVELOPMENT_DEFAULT,
    FIELD_LABELS,
    IDENTITY_POLICY_MANAGE,
    KEY_TTL_RANGE,
    POLICY_FIELDS,
    fieldValueText,
    identityPolicyDefaultOf,
    policyRefusal,
    validatePolicy,
    type IdentityPolicyDefault,
    type PolicyField,
} from '../../lib/identityPolicy';
import IdentityPolicyFields, { PolicyFieldRow } from '../identity/IdentityPolicyFields';
import { TABLE_TBODY } from '../ui/tableStyles';
import { fromDraft, toDraft, type PolicyDraft } from '../identity/policyDraft';

interface Props {
    /** The tenant record as the Control Plane answered it. */
    tenant: unknown;
    /** The default the tenant now holds, after a save or a removal. */
    onSaved?: (value: IdentityPolicyDefault | null) => void;
}

type Stored = { value: IdentityPolicyDefault | null; error: null } | { value: null; error: string };

function readStored(tenant: unknown): Stored {
    try {
        return { value: identityPolicyDefaultOf(tenant), error: null };
    } catch (err) {
        return { value: null, error: errorMessage(err, 'The tenant identity default could not be read.') };
    }
}

const ROWS: readonly (PolicyField | 'max_user_key_ttl_days')[] = [...POLICY_FIELDS, 'max_user_key_ttl_days'];

const TenantIdentityDefaultPanel: React.FC<Props> = ({ tenant, onSaved }) => {
    const { gatewayClient, hasPermission } = useAuth();
    const canManage = hasPermission(IDENTITY_POLICY_MANAGE);

    const initial = useMemo(() => readStored(tenant), [tenant]);
    const [stored, setStored] = useState<IdentityPolicyDefault | null>(initial.value);
    const [readError, setReadError] = useState<string | null>(initial.error);
    const [draft, setDraft] = useState<PolicyDraft | null>(initial.value ? toDraft(initial.value) : null);
    const [ceiling, setCeiling] = useState<string>(String(initial.value?.max_user_key_ttl_days ?? DEFAULT_USER_KEY_TTL_DAYS));
    const { issuers, error: issuersError } = useOidcIssuers(canManage);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const [refused, setRefused] = useState<Partial<Record<PolicyField, string>>>({});


    const policy = useMemo(() => (draft ? fromDraft(draft) : null), [draft]);
    const problems = useMemo(() => (policy ? validatePolicy(policy) : {}), [policy]);
    const ceilingDays = Number(ceiling.trim() || NaN);
    const ceilingProblem =
        !Number.isInteger(ceilingDays) || ceilingDays < KEY_TTL_RANGE.min || ceilingDays > KEY_TTL_RANGE.max
            ? `Between ${KEY_TTL_RANGE.min} and ${KEY_TTL_RANGE.max} days.`
            : null;

    const fieldError = (field: PolicyField): string | null => refused[field] ?? problems[field] ?? null;

    const edit = (change: Partial<PolicyDraft>) => {
        setDraft((current) => (current ? { ...current, ...change } : current));
        setRefused({});
        setNotice(null);
    };

    const write = async (value: IdentityPolicyDefault | null) => {
        setSaving(true);
        setError(null);
        setNotice(null);
        try {
            const now = await gatewayClient.updateTenantIdentityPolicyDefault(value);
            setStored(now);
            setReadError(null);
            setDraft(now ? toDraft(now) : null);
            setCeiling(String(now?.max_user_key_ttl_days ?? DEFAULT_USER_KEY_TTL_DAYS));
            setRefused({});
            setNotice(now ? 'Tenant default saved.' : 'Tenant default removed.');
            onSaved?.(now);
        } catch (err) {
            setError(errorMessage(err, 'The tenant default was not saved'));
            const refusal = policyRefusal(err);
            if (refusal?.code === 'identity_policy_unknown_issuer') {
                setRefused({ allowed_issuers: `Not an OIDC identity provider of this tenant: ${(refusal.issuers ?? []).join(', ')}` });
            }
        } finally {
            setSaving(false);
        }
    };

    const valueOf = (row: PolicyField | 'max_user_key_ttl_days', value: IdentityPolicyDefault): string =>
        row === 'max_user_key_ttl_days' ? `${value.max_user_key_ttl_days} days` : fieldValueText(row, value[row]);

    return (
        <div className="space-y-4 max-w-3xl" data-testid="tenant-identity-default">
            <p className="text-xs text-secondary" data-testid="tenant-identity-explained">
                One of the contributions every group&apos;s identity policy is resolved from, field by field, the stricter value
                winning. It can tighten what applies across the tenant; it cannot lower the production floor of an AI System or the
                high-risk floor. It also sets how long a personal API key may live.
            </p>

            {(readError ?? error) && (
                <div className="flex items-start gap-2 p-3 border border-default rounded-lg bg-surface-alt text-sm text-red-700" data-testid="tenant-identity-error">
                    <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                    <span>{readError ?? error}</span>
                </div>
            )}
            {notice && (
                <p className="text-sm text-green-700" data-testid="tenant-identity-notice">
                    {notice}
                </p>
            )}

            {!readError && !stored && !draft && (
                <p className="text-sm text-secondary" data-testid="tenant-identity-none">
                    No tenant default is set: the development default applies where nothing stricter does, and a personal API key
                    may live at most {DEFAULT_USER_KEY_TTL_DAYS} days.
                </p>
            )}

            {!readError && stored && !canManage && (
                <table className="text-sm">
                    <tbody className={TABLE_TBODY}>
                        {ROWS.map((row) => (
                            <tr key={row}>
                                <td className="pr-4 py-1 text-secondary">{row === 'max_user_key_ttl_days' ? 'Personal key ceiling' : FIELD_LABELS[row]}</td>
                                <td className="py-1 text-default" data-testid={`tenant-identity-value-${row}`}>
                                    {valueOf(row, stored)}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}

            {!canManage ? (
                <p className="text-xs text-muted" data-testid="tenant-identity-no-manage">
                    Changing the tenant default needs the <code>{IDENTITY_POLICY_MANAGE}</code> permission.
                </p>
            ) : (
                <>
                    {!readError && !draft && (
                        <button
                            type="button"
                            onClick={() => setDraft(toDraft(DEVELOPMENT_DEFAULT))}
                            className="px-3 py-1.5 text-sm border border-strong rounded-lg text-secondary hover:bg-surface-alt"
                        >
                            Set a tenant default
                        </button>
                    )}
                    {!readError && draft && (
                        <div className="space-y-4" data-testid="tenant-identity-editor">
                            <IdentityPolicyFields draft={draft} onChange={edit} fieldError={fieldError} issuers={issuers} issuersError={issuersError} idPrefix="tenant-identity" />
                            <PolicyFieldRow label="Personal key ceiling" htmlFor="tenant-identity-user-key-ttl" error={ceilingProblem} field="max_user_key_ttl_days">
                                <input
                                    id="tenant-identity-user-key-ttl"
                                    type="number"
                                    min={KEY_TTL_RANGE.min}
                                    max={KEY_TTL_RANGE.max}
                                    value={ceiling}
                                    onChange={(e) => {
                                        setCeiling(e.target.value);
                                        setNotice(null);
                                    }}
                                    className="w-32 px-2 py-1 text-sm border border-strong rounded"
                                />
                                <span className="ml-2 text-xs text-muted">days</span>
                            </PolicyFieldRow>
                        </div>
                    )}
                    <div className="flex items-center gap-2">
                        {!readError && draft && (
                            <button
                                type="button"
                                disabled={saving || !policy || Object.keys(problems).length > 0 || ceilingProblem !== null}
                                onClick={asyncHandler(() => policy && write({ ...policy, max_user_key_ttl_days: ceilingDays }), 'save tenant identity default')}
                                className="px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50 flex items-center gap-1.5"
                            >
                                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                Save tenant default
                            </button>
                        )}
                        {(stored || readError) && (
                            <button
                                type="button"
                                disabled={saving}
                                onClick={asyncHandler(() => write(null), 'remove tenant identity default')}
                                className="px-3 py-1.5 text-sm border border-strong rounded-lg text-secondary hover:bg-surface-alt disabled:opacity-50 flex items-center gap-1.5"
                            >
                                <Trash2 className="w-4 h-4" />
                                Remove the tenant default
                            </button>
                        )}
                    </div>
                </>
            )}
        </div>
    );
};

export default TenantIdentityDefaultPanel;
