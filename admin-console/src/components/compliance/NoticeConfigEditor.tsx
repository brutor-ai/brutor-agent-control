/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The AI-interaction notice configuration for one scope:
 * the tenant default or a per-system override. Enabled, surface (banner /
 * inline / both), the notice text per portal locale — each stored with
 * its SHA-256 digest, the value the portal's rendered-notice record cites
 * — and an exemption assertion with its basis and rationale. An asserted
 * exemption seals a declaration record: it is the operator's statement,
 * shown as DECLARED, never as evidence that no notice was needed.
 *
 * Every save returns a `seal` block; an unsealed save is shown as such.
 */

import { useState } from 'react';
import { Loader2, Save, Trash2 } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { errorMessage } from '../../lib/narrow';
import {
    NOTICE_EXEMPTION_BASES,
    NOTICE_EXEMPTION_BASIS_LABEL,
    NOTICE_SURFACES,
    PORTAL_LOCALES,
    shortHash,
    type NoticeConfig,
    type NoticeConfigSaveResponse,
    type SealStatus,
} from '../../lib/complianceEvidenceTypes';
import { BasisChip, RecordLink, SealNote } from './ComplianceUi';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, INPUT_CLASS } from './complianceFormat';
import { noticeFormFrom, toNoticePutBody, type NoticeForm } from './complianceForms';

interface Props {
    /** `null` = the tenant default. */
    aiSystemId: string | null;
    scopeLabel: string;
    /** The stored row for this scope, or null when none exists yet. */
    config: NoticeConfig | null;
    /** What the portal resolves for this scope today (system → tenant → default). */
    effective: NoticeConfig | null;
    onSaved: () => void;
    onOpenRecord: (recordId: string) => void;
}

export default function NoticeConfigEditor({ aiSystemId, scopeLabel, config, effective, onSaved, onOpenRecord }: Props) {
    const { gatewayClient } = useGateway();
    const [form, setForm] = useState<NoticeForm>(() => noticeFormFrom(config, effective));
    const [saving, setSaving] = useState(false);
    const [deleting, setDeleting] = useState(false);
    const [error, setError] = useState('');
    const [saved, setSaved] = useState<NoticeConfigSaveResponse | null>(null);
    const [deleteSeal, setDeleteSeal] = useState<SealStatus | null>(null);

    const update = (patch: Partial<NoticeForm>) => setForm((f) => ({ ...f, ...patch }));

    const save = async () => {
        setError('');
        setSaved(null);
        setDeleteSeal(null);
        if (form.exemptionAsserted && !form.exemptionBasis) {
            setError('An asserted exemption needs a basis.');
            return;
        }
        setSaving(true);
        try {
            const res = await gatewayClient.putNoticeConfig(toNoticePutBody(form), aiSystemId ?? undefined);
            setSaved(res);
            onSaved();
        } catch (e: unknown) {
            setError(errorMessage(e, 'Save failed'));
        } finally {
            setSaving(false);
        }
    };

    const remove = async () => {
        if (!config?.id) return;
        if (!window.confirm(`Remove the ${scopeLabel} notice config? The scope falls back to ${aiSystemId ? 'the tenant default' : 'the platform default'}.`)) return;
        setError('');
        setSaved(null);
        setDeleting(true);
        try {
            const res = await gatewayClient.deleteNoticeConfig(config.id);
            setDeleteSeal(res.seal);
            onSaved();
        } catch (e: unknown) {
            setError(errorMessage(e, 'Delete failed'));
        } finally {
            setDeleting(false);
        }
    };

    const digests = config?.text_digests ?? {};

    return (
        <div className="space-y-4 text-xs" data-testid="notice-editor">
            <div className="flex items-start justify-between gap-3 flex-wrap">
                <div>
                    <h3 className="text-sm font-semibold text-default">Notice — {scopeLabel}</h3>
                    <p className="text-muted">
                        {config
                            ? `Stored config${config.updated_at ? ` · ${new Date(config.updated_at).toLocaleString()}` : ''}${config.updated_by ? ` by ${config.updated_by}` : ''}`
                            : effective
                              ? `No config stored for this scope — the portal resolves the ${effective.source === 'tenant' ? 'tenant default' : effective.source === 'system' ? 'system config' : 'platform default'} shown here.`
                              : 'No config stored for this scope.'}
                    </p>
                    {config?.record_id ? (
                        <p className="text-secondary mt-0.5" data-testid="notice-config-record">
                            Sealed config <RecordLink recordId={config.record_id} onOpen={onOpenRecord} />
                        </p>
                    ) : (
                        config && <p className="text-amber-800 mt-0.5">Stored but not sealed — the next save seals it.</p>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    {config?.id && (
                        <button type="button" className={BUTTON_SECONDARY} onClick={() => void remove()} disabled={deleting || saving} data-testid="notice-delete">
                            {deleting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                            Remove
                        </button>
                    )}
                    <button type="button" className={BUTTON_PRIMARY} onClick={() => void save()} disabled={saving || deleting} data-testid="notice-save">
                        {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                        Save
                    </button>
                </div>
            </div>

            {error && (
                <p className="text-red-800 bg-red-50 border border-red-300 rounded p-2" data-testid="notice-error" role="alert">
                    {error}
                </p>
            )}
            {saved && (
                <div className="space-y-1" data-testid="notice-saved">
                    <SealNote seal={saved.seal} what="notice config" onOpenRecord={onOpenRecord} testId="notice-seal" />
                    {saved.declaration && <SealNote seal={saved.declaration} what="exemption declaration" onOpenRecord={onOpenRecord} testId="notice-declaration-seal" />}
                </div>
            )}
            {deleteSeal && <SealNote seal={deleteSeal} what="removal" onOpenRecord={onOpenRecord} testId="notice-delete-seal" />}

            <div className="flex flex-wrap items-center gap-4">
                <label className="inline-flex items-center gap-2">
                    <input type="checkbox" checked={form.enabled} onChange={(e) => update({ enabled: e.target.checked })} data-testid="notice-enabled" />
                    Show the notice
                </label>
                <label className="inline-flex items-center gap-2">
                    <span className="text-muted uppercase tracking-wide">Surface</span>
                    <select value={form.surface} onChange={(e) => update({ surface: e.target.value })} className={INPUT_CLASS} data-testid="notice-surface">
                        {NOTICE_SURFACES.map((s) => (
                            <option key={s} value={s}>
                                {s}
                            </option>
                        ))}
                    </select>
                </label>
            </div>

            <fieldset className="rounded-lg border border-default p-3 space-y-2">
                <legend className="px-1 text-muted uppercase tracking-wide">Text per locale</legend>
                <p className="text-muted">
                    Leave a locale empty to fall back to {aiSystemId ? 'the tenant default, then ' : ''}the portal{'’'}s built-in text. Each stored text
                    carries its digest — the value a rendered-notice record cites.
                </p>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
                    {PORTAL_LOCALES.map((l) => (
                        <label key={l.code} className="block">
                            <span className="text-secondary">
                                {l.label} <span className="font-mono text-muted">({l.code})</span>
                                {digests[l.code] && (
                                    <span className="font-mono text-muted ml-2" title={digests[l.code]} data-testid={`notice-digest-${l.code}`}>
                                        digest {shortHash(digests[l.code], 12)}
                                    </span>
                                )}
                            </span>
                            <textarea
                                value={form.texts[l.code] ?? ''}
                                onChange={(e) => update({ texts: { ...form.texts, [l.code]: e.target.value } })}
                                className={`${INPUT_CLASS} block w-full mt-0.5 h-16`}
                                placeholder={effective?.texts?.[l.code] ? `inherits: ${effective.texts[l.code]}` : 'portal default'}
                                data-testid={`notice-text-${l.code}`}
                            />
                        </label>
                    ))}
                </div>
            </fieldset>

            <fieldset className="rounded-lg border border-default p-3 space-y-2" data-testid="notice-exemption">
                <legend className="px-1 text-muted uppercase tracking-wide inline-flex items-center gap-2">
                    Exemption <BasisChip basis="declared" />
                </legend>
                <label className="inline-flex items-center gap-2">
                    <input
                        type="checkbox"
                        checked={form.exemptionAsserted}
                        onChange={(e) => update({ exemptionAsserted: e.target.checked })}
                        data-testid="notice-exemption-asserted"
                    />
                    Assert that no notice is required for this scope
                </label>
                {form.exemptionAsserted && (
                    <div className="space-y-2">
                        <label className="block">
                            <span className="text-muted uppercase tracking-wide">Basis</span>
                            <select value={form.exemptionBasis} onChange={(e) => update({ exemptionBasis: e.target.value })} className={`${INPUT_CLASS} block mt-0.5`} data-testid="notice-exemption-basis">
                                <option value="">choose…</option>
                                {NOTICE_EXEMPTION_BASES.map((b) => (
                                    <option key={b} value={b}>
                                        {NOTICE_EXEMPTION_BASIS_LABEL[b]}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label className="block">
                            <span className="text-muted uppercase tracking-wide">Rationale</span>
                            <textarea
                                value={form.exemptionRationale}
                                onChange={(e) => update({ exemptionRationale: e.target.value })}
                                className={`${INPUT_CLASS} block w-full mt-0.5 h-16`}
                                data-testid="notice-exemption-rationale"
                            />
                        </label>
                        <p className="text-amber-800">
                            An exemption is the operator{'’'}s declaration. It is sealed and shown as DECLARED — it is not evidence that no notice
                            was needed.
                        </p>
                        {config?.exemption?.declaration_record_id && (
                            <p className="text-secondary" data-testid="notice-exemption-record">
                                Sealed declaration <RecordLink recordId={config.exemption.declaration_record_id} onOpen={onOpenRecord} />
                            </p>
                        )}
                    </div>
                )}
            </fieldset>
        </div>
    );
}
