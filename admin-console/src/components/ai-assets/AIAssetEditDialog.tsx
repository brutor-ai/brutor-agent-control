/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI Asset create/edit dialog.
 *
 * One dialog drives both flows via the `mode` prop:
 *   - `create` — operator registers a *manual* asset; `asset_kind` +
 *     `ref_id` are editable (and required).
 *   - `edit`   — governance overlay on an existing asset; `asset_kind`
 *     + `ref_id` are read-only (discovered assets own those fields).
 *
 * Controlled `useState` per field (NO react-hook-form). Save calls
 * create/update on the gateway client, then closes + triggers the
 * parent refresh. Validation / 422 errors surface inline + as a toast.
 */

import React, { useState } from 'react';
import { Loader2, X } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import InfoBanner from '../common/InfoBanner';
import type {
    AIAsset,
    AIAssetCreateRequest,
    AssetKind,
    AssetStatus,
    EnforcementMode,
    LifecycleStage,
    RiskTier,
} from '../../lib/aiAssetTypes';
import {
    ASSET_KIND_LABEL,
    LIFECYCLE_STAGE_LABEL,
    RISK_TIER_LABEL,
    STATUS_LABEL,
} from '../../lib/aiAssetTypes';
import { asyncHandler } from '../../lib/asyncSafe';
import NotInThisRelease from '../common/NotInThisRelease';

interface Props {
    mode: 'create' | 'edit';
    /** Existing asset when editing; null when creating. */
    initial: AIAsset | null;
    onClose: () => void;
    onSaved: () => void | Promise<void>;
}

const INPUT_CLASS =
    'w-full px-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500';
const LABEL_CLASS = 'block text-xs font-medium text-secondary mb-1';

const AIAssetEditDialog: React.FC<Props> = ({ mode, initial, onClose, onSaved }) => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    const [assetKind, setAssetKind] = useState<AssetKind>(
        initial?.asset_kind ?? 'llm_model',
    );
    const [refId, setRefId] = useState(initial?.ref_id ?? '');
    const [displayName, setDisplayName] = useState(initial?.display_name ?? '');
    const [owner, setOwner] = useState(initial?.owner ?? '');
    const [lifecycleStage, setLifecycleStage] = useState<LifecycleStage>(
        initial?.lifecycle_stage ?? 'proposed',
    );
    // '' is the "unset" sentinel that maps to null on the wire.
    const [riskTier, setRiskTier] = useState<RiskTier | ''>(
        initial?.risk_tier ?? '',
    );
    const [intendedUse, setIntendedUse] = useState(initial?.intended_use ?? '');
    // '' = not assessed (null on the wire); 'none' = explicit
    // not-GPAI assertion the server stores as a cleared flag.
    const [gpai, setGpai] = useState<'' | 'none' | 'gpai' | 'gpai_systemic_risk'>(
        initial?.gpai_classification ?? '',
    );
    const [operatedBy, setOperatedBy] = useState<'' | 'self' | 'third_party'>(
        initial?.operated_by ?? '',
    );
    // The register does not act on traffic in this release: no enforcement
    // mode is offered or sent; a stored one other than "off" is shown.
    const storedEnforcement: EnforcementMode = initial?.enforcement_mode ?? 'off';
    const [status, setStatus] = useState<AssetStatus>(initial?.status ?? 'ok');
    const [tags, setTags] = useState<string[]>(initial?.tags ?? []);
    const [tagInput, setTagInput] = useState('');

    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const isCreate = mode === 'create';
    const refReadOnly = !isCreate;
    const effectiveKind = isCreate ? assetKind : initial?.asset_kind;
    // Annex III classifies by SYSTEM purpose: only ai_system rows carry
    // an editable tier; components display the derived one instead.
    const riskEditable = effectiveKind === 'ai_system';
    const gpaiEditable = effectiveKind === 'llm_model';

    function addTag() {
        const v = tagInput.trim();
        if (!v) return;
        if (!tags.includes(v)) setTags([...tags, v]);
        setTagInput('');
    }

    function removeTag(t: string) {
        setTags(tags.filter((x) => x !== t));
    }

    function onTagKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
        if (e.key === 'Enter') {
            e.preventDefault();
            addTag();
        }
    }

    async function handleSave() {
        setError(null);
        if (isCreate && !refId.trim()) {
            setError('Reference ID is required.');
            return;
        }
        setSaving(true);
        try {
            if (isCreate) {
                const body: AIAssetCreateRequest = {
                    asset_kind: assetKind,
                    ref_id: refId.trim(),
                    display_name: displayName.trim() || null,
                    owner: owner.trim() || null,
                    lifecycle_stage: lifecycleStage,
                    risk_tier: riskEditable && riskTier !== '' ? riskTier : null,
                    intended_use: intendedUse.trim() || null,
                    gpai_classification: gpaiEditable && gpai !== '' ? gpai : null,
                    operated_by: operatedBy === '' ? null : operatedBy,
                    tags,
                };
                await gatewayClient.createAIAsset(body);
                toast.success('Asset created');
            } else if (initial) {
                await gatewayClient.updateAIAsset(initial.id, {
                    display_name: displayName.trim() || null,
                    owner: owner.trim() || null,
                    lifecycle_stage: lifecycleStage,
                    // Components never send a tier — theirs is derived.
                    risk_tier: riskEditable && riskTier !== '' ? riskTier : null,
                    intended_use: intendedUse.trim() || null,
                    gpai_classification: gpaiEditable && gpai !== '' ? gpai : null,
                    operated_by: operatedBy === '' ? null : operatedBy,
                    status,
                    tags,
                });
                toast.success('Asset updated');
            }
            await onSaved();
            onClose();
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
            toast.error(isCreate ? 'Create failed' : 'Save failed', msg);
        } finally {
            setSaving(false);
        }
    }

    return (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between border-b border-default px-5 py-4">
                    <h2 className="text-lg font-semibold text-default">
                        {isCreate ? 'Register AI asset' : 'Edit AI asset'}
                    </h2>
                    <button
                        onClick={onClose}
                        disabled={saving}
                        className="p-1.5 hover:bg-surface-alt rounded-md transition-colors disabled:opacity-50"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5 text-muted" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-5 space-y-4">
                    {error && (
                        <InfoBanner variant="error" className="mb-0">
                            <p>{error}</p>
                        </InfoBanner>
                    )}

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={LABEL_CLASS}>Asset kind</label>
                            {isCreate ? (
                                <select
                                    value={assetKind}
                                    onChange={(e) => setAssetKind(e.target.value as AssetKind)}
                                    className={INPUT_CLASS}
                                >
                                    {(Object.keys(ASSET_KIND_LABEL) as AssetKind[]).map((k) => (
                                        <option key={k} value={k}>
                                            {ASSET_KIND_LABEL[k]}
                                        </option>
                                    ))}
                                </select>
                            ) : (
                                <p className="px-3 py-2 text-sm text-default bg-surface-alt rounded-lg border border-default">
                                    {ASSET_KIND_LABEL[assetKind]}
                                </p>
                            )}
                        </div>
                        <div>
                            <label className={LABEL_CLASS}>
                                Reference ID {isCreate && <span className="text-red-500">*</span>}
                            </label>
                            {refReadOnly ? (
                                <p className="px-3 py-2 text-sm text-default bg-surface-alt rounded-lg border border-default font-mono truncate">
                                    {refId}
                                </p>
                            ) : (
                                <input
                                    type="text"
                                    value={refId}
                                    onChange={(e) => setRefId(e.target.value)}
                                    placeholder="e.g. gpt-5.2 or server slug"
                                    className={INPUT_CLASS}
                                />
                            )}
                        </div>
                    </div>

                    <div>
                        <label className={LABEL_CLASS}>Display name</label>
                        <input
                            type="text"
                            value={displayName}
                            onChange={(e) => setDisplayName(e.target.value)}
                            placeholder="Human-friendly name"
                            className={INPUT_CLASS}
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={LABEL_CLASS}>Owner</label>
                            <input
                                type="text"
                                value={owner}
                                onChange={(e) => setOwner(e.target.value)}
                                placeholder="Team or person"
                                className={INPUT_CLASS}
                            />
                        </div>
                        <div>
                            <label className={LABEL_CLASS}>Lifecycle stage</label>
                            <select
                                value={lifecycleStage}
                                onChange={(e) =>
                                    setLifecycleStage(e.target.value as LifecycleStage)
                                }
                                className={INPUT_CLASS}
                            >
                                {(Object.keys(LIFECYCLE_STAGE_LABEL) as LifecycleStage[]).map(
                                    (s) => (
                                        <option key={s} value={s}>
                                            {LIFECYCLE_STAGE_LABEL[s]}
                                        </option>
                                    ),
                                )}
                            </select>
                        </div>
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={LABEL_CLASS}>Risk tier</label>
                            {riskEditable ? (
                                <select
                                    value={riskTier}
                                    onChange={(e) =>
                                        setRiskTier(e.target.value as RiskTier | '')
                                    }
                                    className={INPUT_CLASS}
                                    data-testid="asset-risk-select"
                                >
                                    <option value="">— Unset —</option>
                                    {(Object.keys(RISK_TIER_LABEL) as RiskTier[]).map((r) => (
                                        <option key={r} value={r}>
                                            {RISK_TIER_LABEL[r]}
                                        </option>
                                    ))}
                                </select>
                            ) : (
                                <div
                                    className="px-3 py-2 rounded-lg border border-default bg-surface-alt text-sm text-secondary"
                                    data-testid="asset-risk-derived"
                                >
                                    {initial?.derived_risk_tier
                                        ? `${RISK_TIER_LABEL[initial.derived_risk_tier]} — via ${(initial.owning_systems ?? [])
                                            .map((o) => o.name)
                                            .join(', ') || 'owning systems'}`
                                        : 'Derived from owning AI Systems'}
                                    <p className="text-xs text-muted mt-1">
                                        Risk classifies the system{'\u2019'}s purpose (Annex III), so
                                        components inherit it from the systems that bind them.
                                    </p>
                                </div>
                            )}
                        </div>
                        <div>
                            <label className={LABEL_CLASS}>Operated by</label>
                            <select
                                value={operatedBy}
                                onChange={(e) =>
                                    setOperatedBy(e.target.value as '' | 'self' | 'third_party')
                                }
                                className={INPUT_CLASS}
                                data-testid="asset-operated-by"
                            >
                                <option value="">— Unset —</option>
                                <option value="self">Self-operated</option>
                                <option value="third_party">Third party</option>
                            </select>
                            <p className="text-xs text-muted mt-1">
                                Third party marks constituents you have obligations
                                over but no operational control of.
                            </p>
                        </div>
                    </div>

                    {gpaiEditable && (
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={LABEL_CLASS}>GPAI classification</label>
                                <select
                                    value={gpai}
                                    onChange={(e) =>
                                        setGpai(e.target.value as '' | 'none' | 'gpai' | 'gpai_systemic_risk')
                                    }
                                    className={INPUT_CLASS}
                                    data-testid="asset-gpai-select"
                                >
                                    <option value="">— Not assessed —</option>
                                    <option value="none">Not GPAI (assessed)</option>
                                    <option value="gpai">GPAI model</option>
                                    <option value="gpai_systemic_risk">GPAI — systemic risk</option>
                                </select>
                                <p className="text-xs text-muted mt-1">
                                    Art 53{'\u2013'}55 obligations attach to the model, on
                                    their own track. Systems binding this model surface
                                    the exposure.
                                </p>
                            </div>
                        </div>
                    )}

                    {storedEnforcement !== 'off' && (
                        <NotInThisRelease
                            testId="asset-enforcement-unavailable"
                            label="Enforcement mode"
                            value={storedEnforcement}
                            consequence="It is kept as it is."
                        />
                    )}

                    {!isCreate && (
                        <div>
                            <label className={LABEL_CLASS}>Status</label>
                            <select
                                value={status}
                                onChange={(e) => setStatus(e.target.value as AssetStatus)}
                                className={INPUT_CLASS}
                            >
                                {(Object.keys(STATUS_LABEL) as AssetStatus[]).map((s) => (
                                    <option key={s} value={s}>
                                        {STATUS_LABEL[s]}
                                    </option>
                                ))}
                            </select>
                        </div>
                    )}

                    <div>
                        <label className={LABEL_CLASS}>Intended use</label>
                        <textarea
                            value={intendedUse}
                            onChange={(e) => setIntendedUse(e.target.value)}
                            rows={3}
                            placeholder="What this asset is approved to do"
                            className={INPUT_CLASS}
                        />
                    </div>

                    <div>
                        <label className={LABEL_CLASS}>Tags</label>
                        <div className="flex flex-wrap gap-1.5 mb-2">
                            {tags.map((t) => (
                                <span
                                    key={t}
                                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-primary-50 text-primary-700 border border-primary-200"
                                >
                                    {t}
                                    <button
                                        type="button"
                                        onClick={() => removeTag(t)}
                                        className="hover:text-primary-900"
                                        aria-label={`Remove tag ${t}`}
                                    >
                                        <X className="w-3 h-3" />
                                    </button>
                                </span>
                            ))}
                        </div>
                        <input
                            type="text"
                            value={tagInput}
                            onChange={(e) => setTagInput(e.target.value)}
                            onKeyDown={onTagKeyDown}
                            onBlur={addTag}
                            placeholder="Type a tag and press Enter"
                            className={INPUT_CLASS}
                        />
                    </div>
                </div>

                {/* Footer */}
                <div className="flex justify-end gap-3 border-t border-default px-5 py-4">
                    <button
                        onClick={onClose}
                        disabled={saving}
                        className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={asyncHandler(handleSave, 'click')}
                        disabled={saving}
                        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors disabled:opacity-50"
                    >
                        {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                        {isCreate ? 'Create asset' : 'Save changes'}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AIAssetEditDialog;
