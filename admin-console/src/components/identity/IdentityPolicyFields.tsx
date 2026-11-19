/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The six fields of an identity policy, as a form: a group's own policy
 * (Governance → Identity) and the tenant identity default edit the same
 * fields. Each field shows the problem `fieldError` names for it.
 */

import React from 'react';

import {
    ASSURANCE_LEVELS,
    KEY_TTL_RANGE,
    LEVEL_LABELS,
    TOKEN_LIFETIME_RANGE,
    levelsToOffer,
    type PolicyField,
} from '../../lib/identityPolicy';
import type { Issuer, PolicyDraft } from './policyDraft';
import { LevelTradeOffs } from './IdentityTradeOffs';

export const PolicyFieldRow: React.FC<{
    label: string;
    /** The test id of the field's problem line. */
    field: string;
    htmlFor?: string;
    error: string | null;
    children: React.ReactNode;
}> = ({ label, field, htmlFor, error, children }) => (
    <div className="space-y-1">
        <label htmlFor={htmlFor} className="block text-xs font-medium text-secondary">
            {label}
        </label>
        {children}
        {error && (
            <p className="text-xs text-red-700" data-testid={`field-error-${field}`}>
                {error}
            </p>
        )}
    </div>
);

interface Props {
    draft: PolicyDraft;
    onChange: (change: Partial<PolicyDraft>) => void;
    fieldError: (field: PolicyField) => string | null;
    /** The tenant's OIDC identity providers. */
    issuers: readonly Issuer[];
    /** Why the identity providers could not be read, if they could not. */
    issuersError?: string | null;
    /** Prefix of the inputs' ids, unique on the page. */
    idPrefix: string;
}

const IdentityPolicyFields: React.FC<Props> = ({ draft, onChange, fieldError, issuers, issuersError = null, idPrefix }) => {
    // An issuer the policy names that is not in the list (deleted, or not readable) is still shown, so it can be removed.
    const listed = [
        ...issuers,
        ...(draft.allowed_issuers ?? []).filter((id) => !issuers.some((i) => i.id === id)).map((id) => ({ id, name: id })),
    ];
    return (
        <>
            <PolicyFieldRow label="Mode" htmlFor={`${idPrefix}-mode`} error={fieldError('mode')} field="mode">
                <select
                    id={`${idPrefix}-mode`}
                    value={draft.mode}
                    onChange={(e) => onChange({ mode: e.target.value === 'enforce' ? 'enforce' : 'audit' })}
                    className="px-2 py-1 text-sm border border-strong rounded"
                >
                    <option value="audit">audit: serve, record below policy</option>
                    <option value="enforce">enforce: refuse below policy</option>
                </select>
            </PolicyFieldRow>

            <PolicyFieldRow label="Minimum credential level" htmlFor={`${idPrefix}-level`} error={fieldError('min_assurance')} field="min_assurance">
                <select
                    id={`${idPrefix}-level`}
                    value={draft.min_assurance}
                    onChange={(e) => {
                        const level = ASSURANCE_LEVELS.find((l) => l === e.target.value);
                        if (level) onChange({ min_assurance: level });
                    }}
                    className="px-2 py-1 text-sm border border-strong rounded"
                >
                    {levelsToOffer(draft.min_assurance).map((level) => (
                        <option key={level} value={level}>
                            {LEVEL_LABELS[level]}
                        </option>
                    ))}
                </select>
                <div className="mt-2">
                    <LevelTradeOffs selected={draft.min_assurance} />
                </div>
            </PolicyFieldRow>

            <PolicyFieldRow label="Personal API keys" htmlFor={`${idPrefix}-user-keys`} error={fieldError('allow_user_keys')} field="allow_user_keys">
                <label className="flex items-center gap-2 text-sm text-secondary">
                    <input
                        id={`${idPrefix}-user-keys`}
                        type="checkbox"
                        checked={draft.allow_user_keys}
                        onChange={(e) => onChange({ allow_user_keys: e.target.checked })}
                    />
                    Accept personal API keys
                </label>
            </PolicyFieldRow>

            <PolicyFieldRow label="Identity providers" error={fieldError('allowed_issuers')} field="allowed_issuers">
                <label className="flex items-center gap-2 text-sm text-secondary">
                    <input
                        type="checkbox"
                        checked={draft.allowed_issuers === null}
                        onChange={(e) => onChange({ allowed_issuers: e.target.checked ? null : [] })}
                    />
                    Any identity provider
                </label>
                {draft.allowed_issuers !== null && (
                    <div className="mt-1 ml-5 space-y-1">
                        {listed.map((issuer) => (
                            <label key={issuer.id} className="flex items-center gap-2 text-sm text-secondary">
                                <input
                                    type="checkbox"
                                    aria-label={issuer.name}
                                    checked={draft.allowed_issuers?.includes(issuer.id) ?? false}
                                    onChange={(e) => {
                                        const current = draft.allowed_issuers ?? [];
                                        onChange({
                                            allowed_issuers: e.target.checked ? [...current, issuer.id] : current.filter((id) => id !== issuer.id),
                                        });
                                    }}
                                />
                                {issuer.name}
                            </label>
                        ))}
                        {issuersError && (
                            <p className="text-xs text-red-700" data-testid="issuers-error">
                                {issuersError}
                            </p>
                        )}
                        {listed.length === 0 && !issuersError && <p className="text-xs text-muted">This tenant has no OIDC identity provider.</p>}
                    </div>
                )}
            </PolicyFieldRow>

            <PolicyFieldRow label="Longest workload token" htmlFor={`${idPrefix}-lifetime`} error={fieldError('max_token_lifetime_s')} field="max_token_lifetime_s">
                <input
                    id={`${idPrefix}-lifetime`}
                    type="number"
                    min={TOKEN_LIFETIME_RANGE.min}
                    max={TOKEN_LIFETIME_RANGE.max}
                    value={draft.max_token_lifetime_s}
                    onChange={(e) => onChange({ max_token_lifetime_s: e.target.value })}
                    className="w-32 px-2 py-1 text-sm border border-strong rounded"
                />
                <span className="ml-2 text-xs text-muted">seconds</span>
            </PolicyFieldRow>

            <PolicyFieldRow label="Longest API key lifetime" htmlFor={`${idPrefix}-key-ttl`} error={fieldError('max_key_ttl_days')} field="max_key_ttl_days">
                <input
                    id={`${idPrefix}-key-ttl`}
                    type="number"
                    min={KEY_TTL_RANGE.min}
                    max={KEY_TTL_RANGE.max}
                    value={draft.max_key_ttl_days}
                    placeholder="no ceiling"
                    onChange={(e) => onChange({ max_key_ttl_days: e.target.value })}
                    className="w-32 px-2 py-1 text-sm border border-strong rounded"
                />
                <span className="ml-2 text-xs text-muted">days</span>
            </PolicyFieldRow>
        </>
    );
};

export default IdentityPolicyFields;
