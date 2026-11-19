/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Governance → Identity: what credential a caller of this group must
 * present.
 *
 * The effective policy is resolved field by field from the development
 * default, the tenant default, every explicit policy down the hierarchy, the
 * production floor of an AI System and the high-risk floor; the stricter
 * value wins. The tab shows each field with where it comes from and the
 * floor the group inherits, so an administrator sees why a field has its
 * value before trying to change it. The group's own policy may tighten the
 * floor, never loosen it: a looser value is named at its field before Save,
 * and the Control Plane's refusal is shown at the fields it names.
 *
 * For an AI System, the credentials that reached it (its whole subtree) in
 * the last 30 days are listed with their level and when they were last seen.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, Loader2, Save, Trash2 } from 'lucide-react';

import { useAuth } from '../../../hooks/useAuth';
import { useOidcIssuers } from '../../../hooks/useOidcIssuers';
import { ResourceGroup } from '../../../lib/groupTypes';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { ago } from '../../../lib/relativeTime';
import {
    FIELD_LABELS,
    IDENTITY_POLICY_MANAGE,
    IDENTITY_POLICY_READ,
    KIND_LABELS,
    LEVEL_LABELS,
    POLICY_FIELDS,
    credentialKey,
    fieldValueText,
    fieldsBelowFloor,
    policyRefusal,
    sourceText,
    validatePolicy,
    type IdentityCredentialList,
    type IdentityPolicy,
    type IdentityPolicyResponse,
    type PolicyField,
} from '../../../lib/identityPolicy';
import IdentityPolicyFields from '../../identity/IdentityPolicyFields';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import { fromDraft, toDraft, type PolicyDraft } from '../../identity/policyDraft';

interface Props {
    group: ResourceGroup;
}

const CREDENTIAL_DAYS = 30;

const GroupIdentityTab: React.FC<Props> = ({ group }) => {
    const { gatewayClient, hasPermission } = useAuth();
    const canRead = hasPermission(IDENTITY_POLICY_READ);
    const canManage = hasPermission(IDENTITY_POLICY_MANAGE);

    const [data, setData] = useState<IdentityPolicyResponse | null>(null);
    const [draft, setDraft] = useState<PolicyDraft | null>(null);
    const [credentials, setCredentials] = useState<IdentityCredentialList | null>(null);
    const [credentialsError, setCredentialsError] = useState<string | null>(null);
    const { issuers, error: issuersError } = useOidcIssuers(canManage);
    const [loading, setLoading] = useState(canRead);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    /** The Control Plane's refusal, per field it named. */
    const [refused, setRefused] = useState<Partial<Record<PolicyField, string>>>({});

    const show = useCallback((answer: IdentityPolicyResponse) => {
        setData(answer);
        setDraft(toDraft(answer.policy ?? answer.inherited_floor));
        setRefused({});
    }, []);

    // A promise chain: every setState happens after an answer, none
    // synchronously in the effect that starts it.
    const load = useCallback(
        () =>
            canRead
                ? gatewayClient.fetchIdentityPolicy(group.id).then(
                      (answer) => {
                          show(answer);
                          setError(null);
                          setLoading(false);
                          if (!answer.is_ai_system) return;
                          gatewayClient.fetchIdentityCredentials(group.id, { days: CREDENTIAL_DAYS }).then(
                              (list) => {
                                  setCredentials(list);
                                  setCredentialsError(null);
                              },
                              (err: unknown) => setCredentialsError(errorMessage(err, 'Failed to load the credentials of this AI System')),
                          );
                      },
                      (err: unknown) => {
                          setData(null);
                          setError(errorMessage(err, 'Failed to load the identity policy'));
                          setLoading(false);
                      },
                  )
                : Promise.resolve(),
        [canRead, gatewayClient, group.id, show],
    );

    useEffect(() => {
        runBackground(load(), 'identity policy');
    }, [load]);


    const policy = useMemo(() => (draft ? fromDraft(draft) : null), [draft]);
    const problems = useMemo(() => (policy ? validatePolicy(policy) : {}), [policy]);
    const belowFloor = useMemo(() => (policy && data ? fieldsBelowFloor(policy, data.inherited_floor) : []), [policy, data]);

    const fieldError = (field: PolicyField): string | null => {
        if (refused[field]) return refused[field] ?? null;
        if (problems[field]) return problems[field] ?? null;
        if (data && belowFloor.includes(field)) {
            return `This is below the inherited floor (${fieldValueText(field, data.inherited_floor[field])}, ${sourceText(data.sources[field], data)}).`;
        }
        return null;
    };

    const write = async (value: IdentityPolicy | null) => {
        setSaving(true);
        setError(null);
        setNotice(null);
        try {
            show(await gatewayClient.putIdentityPolicy(group.id, value));
            setNotice(value === null ? "This group's policy was removed." : 'Policy saved.');
        } catch (err) {
            setError(errorMessage(err, 'The policy was not saved'));
            const refusal = policyRefusal(err);
            const named: Partial<Record<PolicyField, string>> = {};
            for (const problem of refusal?.problems ?? []) {
                const field = POLICY_FIELDS.find((f) => f === problem.field);
                if (!field) continue;
                named[field] =
                    `Requested ${fieldValueText(field, problem.requested)}; the floor is ${fieldValueText(field, problem.floor)}` +
                    (problem.source ? `, ${sourceText(problem.source, { group_id: group.id })}.` : '.');
            }
            if (refusal?.code === 'identity_policy_unknown_issuer') {
                named.allowed_issuers = `Not an OIDC identity provider of this tenant: ${(refusal.issuers ?? []).join(', ')}`;
            }
            setRefused(named);
        } finally {
            setSaving(false);
        }
    };

    const edit = (change: Partial<PolicyDraft>) => {
        setDraft((current) => (current ? { ...current, ...change } : current));
        setRefused({});
        setNotice(null);
    };

    if (!canRead) {
        return (
            <div className="p-6" data-testid="identity-no-read">
                <p className="text-sm text-secondary">
                    Reading the identity policy needs the <code>{IDENTITY_POLICY_READ}</code> permission.
                </p>
            </div>
        );
    }

    if (loading && !data) {
        return (
            <div className="flex items-center justify-center py-16">
                <Loader2 className="w-6 h-6 animate-spin text-primary-500" />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6" data-testid="identity-tab">
            {error && (
                <div className="flex items-start gap-2 p-3 border border-default rounded-lg bg-surface-alt text-sm text-red-700" data-testid="identity-error">
                    <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                    <span>{error}</span>
                </div>
            )}
            {notice && (
                <p className="text-sm text-green-700" data-testid="identity-notice">
                    {notice}
                </p>
            )}

            {data && (
                <>
                    <section className="space-y-2">
                        <h3 className="text-sm font-semibold text-default">Effective policy</h3>
                        {!data.attributed_to_ai_system && (
                            <p className="text-xs text-secondary" data-testid="identity-unattributed">
                                No AI System governs this group: its calls enter no AI System&apos;s evidence. The policy still applies to them.
                            </p>
                        )}
                        <div className={TABLE_CONTAINER}>
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={TABLE_TH}>Field</th>
                                    <th className={TABLE_TH}>Effective</th>
                                    <th className={TABLE_TH}>From</th>
                                    <th className={TABLE_TH}>Inherited floor</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {POLICY_FIELDS.map((field) => (
                                    <tr key={field} data-testid={`effective-${field}`}>
                                        <td className="px-3 py-2 text-secondary">{FIELD_LABELS[field]}</td>
                                        <td className="px-3 py-2 text-default font-medium">{fieldValueText(field, data.effective[field])}</td>
                                        <td className="px-3 py-2 text-secondary">{sourceText(data.sources[field], data)}</td>
                                        <td className="px-3 py-2 text-secondary" data-testid={`floor-${field}`}>
                                            {fieldValueText(field, data.inherited_floor[field])}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                        </div>
                        <p className="text-xs text-muted">
                            Personal API keys may live at most {data.max_user_key_ttl_days} days (the tenant&apos;s ceiling).
                        </p>
                    </section>

                    <section className="text-xs text-secondary space-y-1 border border-default rounded-lg p-3 bg-surface-alt" data-testid="identity-explained">
                        <p>
                            <strong>Lifecycle default.</strong> An AI System that is proposed or approved is in development: unless a
                            stricter policy applies, calls are judged in audit mode at level key. Once it is active, deprecated or retired
                            it is in production: the policy enforces level workload and refuses personal keys, for the system and every
                            group under it, and no explicit or tenant policy lowers that. A high-risk system enforces and refuses personal keys at every stage, and in
                            production requires workload with proof of possession.
                        </p>
                        <p>
                            <strong>Audit or enforce.</strong> In audit mode a call below the policy is served and recorded as
                            {' '}<code>below_policy</code>. In enforce mode it is refused with 403 <code>CREDENTIAL_ASSURANCE_INSUFFICIENT</code>
                            {' '}and recorded as <code>refused</code>.
                        </p>
                    </section>

                    {!canManage ? (
                        <p className="text-xs text-muted" data-testid="identity-no-manage">
                            Changing this group&apos;s policy needs the <code>{IDENTITY_POLICY_MANAGE}</code> permission.
                        </p>
                    ) : (
                        draft && (
                            <section className="space-y-4" data-testid="identity-editor">
                                <h3 className="text-sm font-semibold text-default">
                                    This group&apos;s own policy {data.policy ? '' : '(none set: starting from the inherited floor)'}
                                </h3>

                                <IdentityPolicyFields draft={draft} onChange={edit} fieldError={fieldError} issuers={issuers} issuersError={issuersError} idPrefix="identity" />

                                <div className="flex items-center gap-2">
                                    <button
                                        type="button"
                                        disabled={saving || !policy || Object.keys(problems).length > 0}
                                        onClick={asyncHandler(() => policy && write(policy), 'save identity policy')}
                                        className="px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50 flex items-center gap-1.5"
                                    >
                                        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                        Save policy
                                    </button>
                                    {data.policy && (
                                        <button
                                            type="button"
                                            disabled={saving}
                                            onClick={asyncHandler(() => write(null), 'remove identity policy')}
                                            className="px-3 py-1.5 text-sm border border-strong rounded-lg text-secondary hover:bg-surface-alt disabled:opacity-50 flex items-center gap-1.5"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                            Remove this group&apos;s policy
                                        </button>
                                    )}
                                </div>
                            </section>
                        )
                    )}

                    {data.is_ai_system && (
                        <section className="space-y-2">
                            <h3 className="text-sm font-semibold text-default">Credentials seen in the last {CREDENTIAL_DAYS} days</h3>
                            {credentialsError && <p className="text-sm text-red-700">{credentialsError}</p>}
                            {credentials && credentials.items.length === 0 && (
                                <p className="text-sm text-muted" data-testid="identity-credentials-empty">
                                    No credential reached this AI System in the last {CREDENTIAL_DAYS} days.
                                </p>
                            )}
                            {credentials && credentials.items.length > 0 && (
                                <div className={TABLE_CONTAINER}>
                                <table className="w-full text-sm" data-testid="identity-credentials">
                                    <thead className={TABLE_THEAD}>
                                        <tr>
                                            <th className={TABLE_TH}>Credential</th>
                                            <th className={TABLE_TH}>Kind</th>
                                            <th className={TABLE_TH}>Level</th>
                                            <th className={`${TABLE_TH} text-right`}>Calls (below policy / refused)</th>
                                            <th className={TABLE_TH}>Last seen</th>
                                        </tr>
                                    </thead>
                                    <tbody className={TABLE_TBODY}>
                                        {credentials.items.map((c) => (
                                            <tr key={credentialKey(c)} data-testid={`credential-${c.credential_ref ?? 'no-reference'}`}>
                                                <td className="px-3 py-2">
                                                    <div className="text-default">{c.api_key_name ?? c.agent_id ?? c.credential_ref ?? KIND_LABELS[c.credential_kind]}</div>
                                                    <div className="text-xs text-muted font-mono break-all">{c.credential_ref ?? 'no reference'}</div>
                                                </td>
                                                <td className="px-3 py-2 text-secondary">{KIND_LABELS[c.credential_kind]}</td>
                                                <td className="px-3 py-2 text-secondary">{c.credential_assurance ? LEVEL_LABELS[c.credential_assurance] : 'outside the ladder'}</td>
                                                <td className="px-3 py-2 text-right text-secondary">
                                                    {c.calls} ({c.below_policy_calls} / {c.refused_calls})
                                                </td>
                                                <td className="px-3 py-2 text-secondary">
                                                    {c.last_seen ? <time dateTime={c.last_seen} title={c.last_seen}>{ago(c.last_seen)}</time> : '—'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                                </div>
                            )}
                            {credentials && credentials.total > credentials.items.length && (
                                <p className="text-xs text-muted">
                                    Showing the {credentials.items.length} most recently seen of {credentials.total}.
                                </p>
                            )}
                        </section>
                    )}
                </>
            )}
        </div>
    );
};

export default GroupIdentityTab;
