/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Tenant → Evidence: the witnesses that
 * countersign this tenant's log heads, and the key id the log's records
 * are signed under.
 *
 * Witnesses are configuration and are written here; receipts, heads and
 * records are the core's. A tenant admin adds tenant-scoped witnesses;
 * platform-scoped ones are listed read-only unless the viewer is a system
 * admin. `same_operator` is an honest flag: a witness the same operator
 * runs earns a head `self-witnessed`, never `witnessed`.
 *
 * Also here: the tenant's evidence switches
 * (`salt_digests_default`, `per_tenant_key`), the key ids its records are
 * signed under (deployment and tenant, active and retired) and "Rotate
 * key" — refused by the CP with 409 `per_tenant_key_disabled` while the
 * tenant key is off, which the card explains instead of failing silently.
 * Pins are validated per kind before they reach the CP: a scitt pin is the
 * witness's Ed25519 public key (64 hex); an rfc3161 pin is the SHA-256 of
 * the TSA certificate DER (64 hex) or the certificate PEM.
 */

import { isTenantKeyUnavailable, TENANT_KEY_UNAVAILABLE_TEXT } from '../../lib/gatewayRefusals';
import { useCallback, useState } from 'react';
import { createPortal } from 'react-dom';
import { KeyRound, Loader2, Pencil, Plus, RotateCw, Trash2, X } from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import ReadFailureNotice from '../common/ReadFailureNotice';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { failureSaying } from '../../hooks/useRead';
import { useToast } from '../common/Toast';
import { useConfirm } from '../common/ConfirmationDialog';
import { errorMessage, failureSentence } from '../../lib/narrow';
import {
    errorCode,
    normalizeWitnessPin,
    validateWitnessPin,
    validateWitnessUrl,
    type EvidenceSettings,
    type SigningKeyList,
} from '../../lib/evidenceSurfaceTypes';
import { HelpTip } from '../ui/HelpTip';
import {
    shortHash,
    WITNESS_KINDS,
    type EvidenceLogItem,
    type EvidenceWitness,
    type WitnessKind,
    type WitnessScope,
} from '../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';

interface WitnessForm {
    scope: WitnessScope;
    kind: WitnessKind;
    url: string;
    operator_label: string;
    pinned_key: string;
    enabled: boolean;
    same_operator: boolean;
}

const EMPTY_FORM: WitnessForm = {
    scope: 'tenant',
    kind: 'scitt',
    url: '',
    operator_label: '',
    pinned_key: '',
    enabled: true,
    same_operator: false,
};

const INPUT = 'mt-1 w-full rounded-md border border-default bg-surface px-2 py-1.5 text-sm';

interface PanelData {
    witnesses: EvidenceWitness[];
    log: EvidenceLogItem | null;
    settings: EvidenceSettings | null;
    keys: SigningKeyList | null;
    /** Why the log, the settings or the keys could not be loaded; '' where they were. */
    failed: Record<'log' | 'settings' | 'keys', string>;
}

const PIN_HELP =
    'Receipts are checked against this key; a receipt under any other key grades unverified, and with no pin every receipt does. ' +
    'scitt: the witness’s Ed25519 public key as 64 hex characters (32 bytes), as its operator publishes it. ' +
    'rfc3161: the SHA-256 of the timestamp authority’s certificate in DER form, as 64 hex characters ' +
    '(e.g. openssl x509 -in tsa.pem -outform DER | sha256sum), or the certificate itself as PEM. ' +
    'A timestamp dates a head; it never raises its grade.';

/** Which form fields fail validation — `null` means the field is fine. */
function formErrors(form: WitnessForm): { url: string | null; operator: string | null; pin: string | null } {
    return {
        url: validateWitnessUrl(form.url),
        operator: form.operator_label.trim() ? null : 'Operator label is required.',
        pin: validateWitnessPin(form.kind, form.pinned_key),
    };
}

export default function TenantEvidencePanel() {
    const { gatewayClient } = useGateway();
    const { principal } = useAuth();
    const toast = useToast();
    const confirm = useConfirm();
    // A witness of every tenant's log is for an administrator of no tenant:
    // a contributed principal (slot `app.principals`).
    const canPlatformScope = Boolean(principal);

    const [editing, setEditing] = useState<{ id: string | null; form: WitnessForm } | null>(null);
    const [saving, setSaving] = useState(false);

    // Witnesses are the panel; the log is a card that may simply not exist
    // yet, so its failure does not fail the panel.
    const loader = useCallback(async (): Promise<PanelData> => {
        const [w, l, st, k] = await Promise.allSettled([
            gatewayClient.listEvidenceWitnesses(),
            gatewayClient.listEvidenceLogs(),
            gatewayClient.getEvidenceSettings(),
            gatewayClient.listEvidenceSigningKeys(),
        ]);
        if (w.status === 'rejected') throw w.reason;
        return {
            witnesses: w.value.items,
            log: l.status === 'fulfilled' ? (l.value.items[0] ?? null) : null,
            settings: st.status === 'fulfilled' ? st.value : null,
            keys: k.status === 'fulfilled' ? k.value : null,
            failed: {
                log: l.status === 'rejected' ? failureSentence('Failed to load the evidence log', l.reason) : '',
                settings: st.status === 'rejected' ? failureSentence('Failed to load the evidence settings', st.reason) : '',
                keys: k.status === 'rejected' ? failureSentence('Failed to load the signing keys', k.reason) : '',
            },
        };
    }, [gatewayClient]);
    const { data, error, reload, setData } = useKeyedLoad('witnesses', loader, 'Failed to load witnesses');
    const witnesses = data?.witnesses ?? null;
    const log = data?.log ?? null;
    const settings = data?.settings ?? null;
    const keys = data?.keys ?? null;

    const [settingsBusy, setSettingsBusy] = useState(false);
    const [rotating, setRotating] = useState(false);
    const [rotateNote, setRotateNote] = useState<{ tone: 'ok' | 'warn' | 'error'; text: string } | null>(null);
    const [touched, setTouched] = useState(false);

    const saveSetting = async (name: 'per_tenant_key' | 'salt_digests_default', value: boolean) => {
        if (name === 'per_tenant_key' && !value) {
            const ok = await confirm({
                title: 'Turn off the per-tenant key',
                message:
                    'New records will be signed by the deployment key again. Records already signed by the tenant key keep verifying — retired and inactive keys stay trusted.',
                confirmLabel: 'Turn off',
            });
            if (!ok) return;
        }
        setSettingsBusy(true);
        try {
            const next = await gatewayClient.putEvidenceSettings(name === 'per_tenant_key' ? { per_tenant_key: value } : { salt_digests_default: value });
            setData((prev) => (prev ? { ...prev, settings: next } : prev));
            toast.success('Evidence settings saved');
        } catch (e: unknown) {
            toast.error(`Save failed: ${errorMessage(e)}`);
        } finally {
            setSettingsBusy(false);
        }
    };

    const rotate = async () => {
        const ok = await confirm({
            title: 'Rotate the tenant key',
            message:
                'The core creates a new tenant signing key (the first rotation creates it) and seals a rotation record signed by the old key. The old key id stays trusted for verification.',
            confirmLabel: 'Rotate',
        });
        if (!ok) return;
        setRotating(true);
        setRotateNote(null);
        try {
            const r = await gatewayClient.rotateEvidenceKey();
            setRotateNote({
                tone: 'ok',
                text: `New key ${shortHash(r.kid, 16)}${r.previous_kid ? ` replaces ${shortHash(r.previous_kid, 16)}` : ''}${r.rotation_record_id ? ` · rotation record ${shortHash(r.rotation_record_id, 16)}` : ''}.`,
            });
            reload();
        } catch (e: unknown) {
            if (errorCode(e) === 'per_tenant_key_disabled') {
                setRotateNote({
                    tone: 'warn',
                    text: 'Per-tenant keys are off for this tenant. Turn on “Per-tenant key” above, then rotate to create it.',
                });
            } else if (isTenantKeyUnavailable(e)) {
                // The key that should hand over is the one that cannot be read.
                setRotateNote({
                    tone: 'error',
                    text: `${TENANT_KEY_UNAVAILABLE_TEXT} The rotation was refused for the same reason. Ask whoever runs the installation to check the encryption key of the gateway and the gateway log, then rotate again.`,
                });
            } else {
                setRotateNote({ tone: 'error', text: `Rotation failed: ${errorMessage(e)}` });
            }
        } finally {
            setRotating(false);
        }
    };

    const canEdit = (w: EvidenceWitness) => w.scope === 'tenant' || canPlatformScope;

    const openCreate = () => {
        setTouched(false);
        setEditing({ id: null, form: { ...EMPTY_FORM } });
    };
    const openEdit = (w: EvidenceWitness) => {
        setTouched(false);
        setEditing({
            id: w.id,
            form: {
                scope: w.scope === 'platform' ? 'platform' : 'tenant',
                kind: w.kind === 'rfc3161' ? 'rfc3161' : 'scitt',
                url: w.url,
                operator_label: w.operator_label,
                pinned_key: w.pinned_key ?? '',
                enabled: w.enabled,
                same_operator: w.same_operator,
            },
        });
    };

    const errors = editing ? formErrors(editing.form) : null;
    const invalid = !!errors && !!(errors.url || errors.operator || errors.pin);

    const save = async () => {
        if (!editing) return;
        const { id, form } = editing;
        setTouched(true);
        if (invalid) return;
        setSaving(true);
        try {
            if (id === null) {
                await gatewayClient.createEvidenceWitness({
                    scope: form.scope,
                    kind: form.kind,
                    url: form.url.trim(),
                    operator_label: form.operator_label.trim(),
                    pinned_key: normalizeWitnessPin(form.pinned_key),
                    enabled: form.enabled,
                    same_operator: form.same_operator,
                });
                toast.success('Witness added');
            } else {
                await gatewayClient.updateEvidenceWitness(id, {
                    url: form.url.trim(),
                    operator_label: form.operator_label.trim(),
                    pinned_key: normalizeWitnessPin(form.pinned_key),
                    enabled: form.enabled,
                    same_operator: form.same_operator,
                });
                toast.success('Witness updated');
            }
            setEditing(null);
            reload();
        } catch (e: unknown) {
            toast.error(`Save failed: ${errorMessage(e)}`);
        } finally {
            setSaving(false);
        }
    };

    const remove = async (w: EvidenceWitness) => {
        const ok = await confirm({
            title: 'Remove witness',
            message: `Remove ${w.operator_label} (${w.url})? Heads it has already countersigned keep their receipts; new heads will no longer be sent to it.`,
            confirmLabel: 'Remove',
        });
        if (!ok) return;
        try {
            await gatewayClient.deleteEvidenceWitness(w.id);
            toast.success('Witness removed');
            reload();
        } catch (e: unknown) {
            toast.error(`Remove failed: ${errorMessage(e)}`);
        }
    };

    return (
        <div className="space-y-5" data-testid="tenant-evidence-panel">
            {/* Key card */}
            <div className="rounded-lg border border-default bg-surface-alt p-4 flex items-start gap-3" data-testid="tenant-evidence-key">
                <KeyRound className="w-5 h-5 text-primary-600 mt-0.5" />
                <div className="min-w-0 min-h-0 flex-1">
                    <h4 className="text-sm font-medium text-default">Tenant log key</h4>
                    {log ? (
                        <dl className="mt-1 grid grid-cols-1 sm:grid-cols-3 gap-x-6 gap-y-1 text-xs">
                            <div>
                                <dt className="text-muted uppercase tracking-wide">Key id</dt>
                                <dd className="text-default font-mono break-all" title={log.kid ?? undefined}>
                                    {log.kid ?? '—'}
                                </dd>
                            </div>
                            <div>
                                <dt className="text-muted uppercase tracking-wide">Log id</dt>
                                <dd className="text-default font-mono break-all">{log.log_id}</dd>
                            </div>
                            <div>
                                <dt className="text-muted uppercase tracking-wide">Tree size</dt>
                                <dd className="text-default">{log.tree_size.toLocaleString()}</dd>
                            </div>
                        </dl>
                    ) : data?.failed.log ? (
                        <ReadFailureNotice
                            compact
                            className="block mt-1"
                            failure={failureSaying(data.failed.log)}
                            onRetry={reload}
                            data-testid="tenant-evidence-log-failed"
                        />
                    ) : (
                        <p className="text-xs text-muted mt-1" data-testid="tenant-evidence-no-log">
                            No tenant log yet — the core creates it, and its key id, with the first sealed record.
                        </p>
                    )}
                    <p className="text-xs text-muted mt-2">
                        The producer key never leaves the core; only key ids are listed here. Retired keys stay trusted,
                        so records they signed keep verifying after a rotation.
                    </p>

                    {keys && keys.items.length > 0 && (
                        <table className="mt-2 w-full text-xs" data-testid="tenant-evidence-keys">
                            <thead className="text-muted">
                                <tr>
                                    <th className="text-left font-medium py-0.5">Key id</th>
                                    <th className="text-left font-medium py-0.5">Scope</th>
                                    <th className="text-left font-medium py-0.5">Status</th>
                                    <th className="text-left font-medium py-0.5">Retired</th>
                                </tr>
                            </thead>
                            <tbody>
                                {keys.items.map((k) => (
                                    <tr key={k.kid} data-testid={`evidence-key-${k.kid}`}>
                                        <td className="font-mono text-default py-0.5" title={k.kid}>
                                            {shortHash(k.kid, 16)}
                                            {k.kid === (keys.log_kid ?? log?.kid) && (
                                                <span className="ml-1 text-[10px] text-primary-600">signing now</span>
                                            )}
                                        </td>
                                        <td className="text-secondary py-0.5">{k.scope}</td>
                                        <td className="py-0.5">
                                            <span
                                                className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                                                    k.status === 'active' ? 'bg-green-100 text-green-800' : 'bg-surface-raised text-secondary'
                                                }`}
                                            >
                                                {k.status}
                                            </span>
                                        </td>
                                        <td className="text-secondary py-0.5">{k.retired_at ? new Date(k.retired_at).toLocaleString() : '—'}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                    <ReadFailureNotice
                        compact
                        className="block mt-2"
                        failure={failureSaying(data?.failed.keys)}
                        onRetry={reload}
                        data-testid="tenant-evidence-keys-failed"
                    />
                    {keys && keys.items.length === 0 && (
                        <p className="text-xs text-muted mt-2" data-testid="tenant-evidence-keys-deployment-only">
                            Records are signed by the deployment key configured in the core; no tenant or retired keys exist.
                        </p>
                    )}

                    <div className="mt-3 flex items-center gap-2 flex-wrap">
                        <button
                            type="button"
                            onClick={() => void rotate()}
                            disabled={rotating}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-strong hover:bg-surface-hover disabled:opacity-50"
                            data-testid="evidence-rotate-key"
                        >
                            {rotating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCw className="w-3.5 h-3.5" />}
                            Rotate key
                        </button>
                        {settings && !settings.per_tenant_key && (
                            <span className="text-[11px] text-muted">Needs the per-tenant key switched on.</span>
                        )}
                    </div>
                    {rotateNote && (
                        <p
                            className={`text-xs mt-2 ${
                                rotateNote.tone === 'ok' ? 'text-green-800' : rotateNote.tone === 'warn' ? 'text-amber-800' : 'text-red-700'
                            }`}
                            data-testid="evidence-rotate-note"
                        >
                            {rotateNote.text}
                        </p>
                    )}
                </div>
            </div>

            {/* Settings card */}
            <div className="rounded-lg border border-default bg-surface p-4 space-y-3" data-testid="tenant-evidence-settings">
                <h4 className="text-sm font-medium text-default">Evidence settings</h4>
                {settings ? (
                    <>
                        <label className="flex items-start gap-3">
                            <input
                                type="checkbox"
                                checked={settings.salt_digests_default}
                                disabled={settingsBusy}
                                onChange={(e) => void saveSetting('salt_digests_default', e.target.checked)}
                                className="mt-0.5 h-4 w-4 rounded border-gray-300"
                                data-testid="evidence-salt-default"
                            />
                            <span className="text-xs">
                                <span className="font-medium text-default inline-flex items-center gap-1">
                                    Salt content digests by default
                                    <HelpTip text="Digests of prompts, arguments and results are salted with a fresh per-record salt, so a short or guessable value cannot be recovered by hashing candidates. High-risk systems salt regardless; a system's compliance profile can still set salt_digests explicitly. Digests of bodies that were not captured are omitted, never left unsalted." />
                                </span>
                                <span className="block text-secondary">
                                    Applies to systems whose profile does not say, and to ungoverned traffic.
                                </span>
                            </span>
                        </label>
                        <label className="flex items-start gap-3">
                            <input
                                type="checkbox"
                                checked={settings.per_tenant_key}
                                disabled={settingsBusy}
                                onChange={(e) => void saveSetting('per_tenant_key', e.target.checked)}
                                className="mt-0.5 h-4 w-4 rounded border-gray-300"
                                data-testid="evidence-per-tenant-key"
                            />
                            <span className="text-xs">
                                <span className="font-medium text-default inline-flex items-center gap-1">
                                    Per-tenant key
                                    <HelpTip text="Sign this tenant's records with a key of its own instead of the deployment key. Switching it on does not create the key — Rotate key does, with a hand-over record signed by the deployment key. The public key document lists tenant keys by log id, never by tenant id." />
                                </span>
                                <span className="block text-secondary">Off: records are signed by the deployment key.</span>
                            </span>
                        </label>
                        {settings.updated_by && (
                            <p className="text-[11px] text-muted">
                                Last changed by {settings.updated_by}
                                {settings.updated_at ? ` · ${new Date(settings.updated_at).toLocaleString()}` : ''}
                            </p>
                        )}
                    </>
                ) : (
                    <ReadFailureNotice
                        compact
                        failure={failureSaying(data?.failed.settings)}
                        onRetry={reload}
                        data-testid="tenant-evidence-settings-failed"
                    />
                )}
            </div>

            {/* Witnesses */}
            <div>
                <div className="flex items-center justify-between mb-2">
                    <div>
                        <h4 className="text-sm font-medium text-default inline-flex items-center gap-1" id="evidence-witnesses">
                            Witnesses
                            <HelpTip text="scitt witnesses register each tree head and return a receipt checked against the pinned key: one independent receipt grades the head witnessed, two or more plural, a same-operator one self-witnessed. rfc3161 timestamp authorities date a head and never raise its grade. Heads left standalone or self-witnessed for over an hour raise an inbox finding for tenants with a high-risk system." />
                        </h4>
                        <p className="text-xs text-muted">
                            Independent services that countersign tree heads. One receipt under a pinned key makes a head
                            witnessed; two independently operated witnesses make it plural.
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={openCreate}
                        className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700"
                        data-testid="witness-add"
                    >
                        <Plus className="w-3.5 h-3.5" /> Add witness
                    </button>
                </div>

                {error && (
                    <p className="p-3 mb-2 rounded-lg border border-red-300 bg-red-50 text-sm text-red-800" data-testid="tenant-evidence-error">
                        {error}
                    </p>
                )}

                <div className={TABLE_CONTAINER}>
                    <table className="w-full text-xs">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={TABLE_TH}>Kind</th>
                                <th className={TABLE_TH}>URL</th>
                                <th className={TABLE_TH}>Operator</th>
                                <th className={TABLE_TH}>Pinned key</th>
                                <th className={TABLE_TH}>Scope</th>
                                <th className={TABLE_TH}>Enabled</th>
                                <th className={TABLE_TH}></th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {witnesses === null && !error && (
                                <tr>
                                    <td colSpan={7} className="px-4 py-4 text-muted">
                                        <Loader2 className="w-3.5 h-3.5 animate-spin inline-block mr-1" /> Loading…
                                    </td>
                                </tr>
                            )}
                            {witnesses?.length === 0 && (
                                <tr>
                                    <td colSpan={7} className="px-4 py-4 text-muted" data-testid="witnesses-empty">
                                        No witnesses configured — every head stays standalone until one is added.
                                    </td>
                                </tr>
                            )}
                            {witnesses?.map((w) => (
                                <tr key={w.id} data-testid={`witness-row-${w.id}`}>
                                    <td className="px-4 py-2 font-mono text-default">{w.kind}</td>
                                    <td className="px-4 py-2 text-default break-all">{w.url}</td>
                                    <td className="px-4 py-2 text-default">
                                        {w.operator_label}
                                        {w.same_operator && (
                                            <span
                                                className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-amber-100 text-amber-800"
                                                title="Run by the same operator as this platform — heads it countersigns grade self-witnessed"
                                                data-testid={`witness-same-operator-${w.id}`}
                                            >
                                                same operator
                                            </span>
                                        )}
                                    </td>
                                    <td className="px-4 py-2 font-mono text-secondary" title={w.pinned_key ?? undefined}>
                                        {w.pinned_key ? shortHash(w.pinned_key, 16) : <span className="text-amber-800">none pinned</span>}
                                    </td>
                                    <td className="px-4 py-2 text-secondary">{w.scope}</td>
                                    <td className="px-4 py-2 text-secondary">{w.enabled ? 'on' : 'off'}</td>
                                    <td className="px-4 py-2 text-right whitespace-nowrap">
                                        {canEdit(w) && (
                                            <>
                                                <button
                                                    type="button"
                                                    onClick={() => openEdit(w)}
                                                    aria-label={`Edit ${w.operator_label}`}
                                                    className="p-1 text-muted hover:text-default"
                                                    data-testid={`witness-edit-${w.id}`}
                                                >
                                                    <Pencil className="w-3.5 h-3.5" />
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => void remove(w)}
                                                    aria-label={`Remove ${w.operator_label}`}
                                                    className="p-1 text-muted hover:text-red-600"
                                                    data-testid={`witness-delete-${w.id}`}
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                </button>
                                            </>
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>

            {editing &&
                createPortal(
                    <div
                        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
                        role="dialog"
                        aria-modal="true"
                        aria-label={editing.id ? 'Edit witness' : 'Add witness'}
                        data-testid="witness-dialog"
                        onMouseDown={(e) => {
                            if (e.target === e.currentTarget) setEditing(null);
                        }}
                    >
                        <form
                            noValidate
                            className="bg-surface border border-default rounded-xl shadow-xl w-full max-w-lg"
                            onSubmit={(e) => {
                                e.preventDefault();
                                void save();
                            }}
                        >
                            <div className="flex items-center justify-between px-5 py-3 border-b border-default">
                                <h2 className="text-sm font-semibold text-default">{editing.id ? 'Edit witness' : 'Add witness'}</h2>
                                <button type="button" onClick={() => setEditing(null)} aria-label="Close" className="p-1 rounded hover:bg-surface-raised text-muted">
                                    <X className="w-5 h-5" />
                                </button>
                            </div>
                            <div className="px-5 py-4 space-y-3 text-sm">
                                {editing.id === null && (
                                    <label className="block">
                                        <span className="text-xs text-muted">Scope</span>
                                        <select
                                            value={editing.form.scope}
                                            onChange={(e) => setEditing({ ...editing, form: { ...editing.form, scope: e.target.value as WitnessScope } })}
                                            className={INPUT}
                                            disabled={!canPlatformScope}
                                            data-testid="witness-scope"
                                        >
                                            <option value="tenant">tenant — this tenant's log only</option>
                                            {canPlatformScope && <option value="platform">platform — every tenant's log</option>}
                                        </select>
                                        {!canPlatformScope && (
                                            <span className="text-[11px] text-muted">Tenant admins add tenant-scoped witnesses.</span>
                                        )}
                                    </label>
                                )}
                                <label className="block">
                                    <span className="text-xs text-muted">Kind</span>
                                    <select
                                        value={editing.form.kind}
                                        onChange={(e) => setEditing({ ...editing, form: { ...editing.form, kind: e.target.value as WitnessKind } })}
                                        className={INPUT}
                                        disabled={editing.id !== null}
                                        data-testid="witness-kind"
                                    >
                                        {WITNESS_KINDS.map((k) => (
                                            <option key={k} value={k}>
                                                {k === 'scitt' ? 'scitt — SCITT transparency service' : 'rfc3161 — RFC 3161 timestamp authority'}
                                            </option>
                                        ))}
                                    </select>
                                </label>
                                <label className="block">
                                    <span className="text-xs text-muted">URL</span>
                                    <input
                                        type="url"
                                        required
                                        value={editing.form.url}
                                        onChange={(e) => setEditing({ ...editing, form: { ...editing.form, url: e.target.value } })}
                                        placeholder="https://witness.example.org"
                                        className={INPUT}
                                        data-testid="witness-url"
                                    />
                                    {touched && errors?.url && (
                                        <span className="text-[11px] text-red-700" data-testid="witness-url-error">
                                            {errors.url}
                                        </span>
                                    )}
                                </label>
                                <label className="block">
                                    <span className="text-xs text-muted">Operator label</span>
                                    <input
                                        type="text"
                                        required
                                        value={editing.form.operator_label}
                                        onChange={(e) => setEditing({ ...editing, form: { ...editing.form, operator_label: e.target.value } })}
                                        placeholder="Who runs it"
                                        className={INPUT}
                                        data-testid="witness-operator"
                                    />
                                    {touched && errors?.operator && (
                                        <span className="text-[11px] text-red-700" data-testid="witness-operator-error">
                                            {errors.operator}
                                        </span>
                                    )}
                                </label>
                                <div className="block">
                                    <span className="text-xs text-muted inline-flex items-center gap-1">
                                        {editing.form.kind === 'rfc3161'
                                            ? 'Pinned TSA certificate (SHA-256 of the DER, or PEM)'
                                            : 'Pinned Ed25519 public key (64 hex)'}
                                        <HelpTip text={PIN_HELP} />
                                    </span>
                                    <textarea
                                        rows={editing.form.kind === 'rfc3161' ? 3 : 1}
                                        value={editing.form.pinned_key}
                                        onChange={(e) => setEditing({ ...editing, form: { ...editing.form, pinned_key: e.target.value } })}
                                        className={`${INPUT} font-mono text-xs`}
                                        aria-label="Pinned key"
                                        data-testid="witness-pinned-key"
                                    />
                                    {errors?.pin ? (
                                        <span className="text-[11px] text-red-700" data-testid="witness-pin-error">
                                            {errors.pin}
                                        </span>
                                    ) : (
                                        !editing.form.pinned_key.trim() && (
                                            <span className="text-[11px] text-amber-800" data-testid="witness-pin-empty">
                                                No pin: every receipt from this witness grades unverified.
                                            </span>
                                        )
                                    )}
                                </div>
                                <div className="flex items-center gap-4">
                                    <label className="inline-flex items-center gap-1.5 text-xs text-default">
                                        <input
                                            type="checkbox"
                                            checked={editing.form.enabled}
                                            onChange={(e) => setEditing({ ...editing, form: { ...editing.form, enabled: e.target.checked } })}
                                            className="h-4 w-4 rounded border-gray-300"
                                            data-testid="witness-enabled"
                                        />
                                        Enabled
                                    </label>
                                    <label className="inline-flex items-center gap-1.5 text-xs text-default" title="Heads this witness countersigns grade self-witnessed">
                                        <input
                                            type="checkbox"
                                            checked={editing.form.same_operator}
                                            onChange={(e) => setEditing({ ...editing, form: { ...editing.form, same_operator: e.target.checked } })}
                                            className="h-4 w-4 rounded border-gray-300"
                                            data-testid="witness-same-operator"
                                        />
                                        Same operator as this platform
                                    </label>
                                </div>
                            </div>
                            <div className="flex justify-end gap-2 px-5 py-3 border-t border-default">
                                <button
                                    type="button"
                                    onClick={() => setEditing(null)}
                                    className="px-3 py-1.5 text-sm rounded-md border border-strong hover:bg-surface-hover"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="submit"
                                    disabled={saving || (touched && invalid)}
                                    className="inline-flex items-center gap-2 px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                                    data-testid="witness-save"
                                >
                                    {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                                    {editing.id ? 'Save' : 'Add'}
                                </button>
                            </div>
                        </form>
                    </div>,
                    document.body,
                )}
        </div>
    );
}
