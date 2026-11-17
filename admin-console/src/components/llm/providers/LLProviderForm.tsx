/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Create / edit modal for `LlmProvider` rows.
 *
 * Design notes
 * ------------
 * 1. Slug (`key`) is locked once a provider exists. Renames go via
 *    a dedicated POST /rename endpoint that the server refuses if
 *    any models still reference the old slug — we don't expose that
 *    here yet; operators rename by deleting + recreating, which is
 *    fine for the first cut.
 * 2. Icon upload sits inside this form. We POST the file as soon as
 *    the operator picks one (multipart) — separate from the JSON
 *    save — so the provider row must already exist (i.e. only on
 *    edit). For new providers we save first, then re-open in edit
 *    to upload an icon. This mirrors the MCP server flow.
 * 3. URL fields are optional and validated server-side; we just
 *    trim + send. Empty strings collapse to `null` per the Pydantic
 *    schema.
 */

import React, { useEffect, useRef, useState } from 'react';
import {
    X,
    Save,
    Globe,
    Image as ImageIcon,
    Upload,
    Trash2,
    AlertCircle,
} from 'lucide-react';

import AuditInfoTab from '../../common/AuditInfoTab';
import FormTabBar from '../../common/FormTabBar';
import { useGateway } from '../../../hooks/useGateway';
import type {
    LlmProviderResponse,
    LlmProviderCreateRequest,
    LlmProviderUpdateRequest,
    LlmProviderCategory,
    LlmProviderAuthType,
} from '../../../lib/llmProviderTypes';
import ProviderIcon from '../config/ProviderIcon';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler } from '../../../lib/asyncSafe';

interface Props {
    /** When null, the form is in create mode. */
    provider: LlmProviderResponse | null;
    onClose: () => void;
    onSaved: (msg: string) => void | Promise<void>;
}

const CATEGORY_OPTIONS: { value: LlmProviderCategory; label: string }[] = [
    { value: 'chat', label: 'Chat / Reasoning' },
    { value: 'image', label: 'Image generation' },
    { value: 'audio', label: 'Audio (TTS / STT)' },
    { value: 'video', label: 'Video generation' },
    { value: 'embeddings', label: 'Embeddings' },
    { value: 'self_hosted', label: 'Self-hosted / OSS' },
    { value: 'internal', label: 'Internal' },
];

const AUTH_OPTIONS: { value: LlmProviderAuthType; label: string }[] = [
    { value: 'api_key', label: 'API key' },
    { value: 'oauth2', label: 'OAuth 2.0' },
    { value: 'gcp_service_account', label: 'GCP service account' },
    { value: 'aws_iam', label: 'AWS IAM' },
    { value: 'azure_ad', label: 'Azure AD' },
    { value: 'none', label: 'None' },
];

// Slug rule mirrors `^[a-z0-9][a-z0-9_-]{0,63}$` from the Pydantic
// validator — keep them in lockstep so we fail fast client-side.
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

interface FormState {
    key: string;
    display_name: string;
    category: LlmProviderCategory | '';
    default_auth_type: LlmProviderAuthType | '';
    default_api_url: string;
    developer_url: string;
    api_key_url: string;
    pricing_url: string;
    /** Human-facing docs page — click-out anchor in the admin UI;
     *  proxy never reads it. */
    model_catalog_url: string;
    /** Machine-callable /models endpoint — proxy hits it during
     *  catalog-probe health checks. NULL/blank → fall back to
     *  real-inference probe. */
    models_api_url: string;
    /** Comma-separated drop list — runtime-inherited by every model
     *  under this provider. Edited as a free-text input here; the
     *  proxy unions this with each model's own additional_drop_params. */
    additional_drop_params: string;
    /** Region this provider serves traffic
     *  from. Free-text per the open-string contract (operators
     *  name their own regions). Blank stored as NULL. */
    region: string;
    notes: string;
    enabled: boolean;
    /** Operator-authored label; authorship beside it is system-set. */
    version: string;
}

function emptyState(): FormState {
    return {
        version: '1.0',
        key: '',
        display_name: '',
        category: 'chat',
        default_auth_type: 'api_key',
        default_api_url: '',
        developer_url: '',
        api_key_url: '',
        pricing_url: '',
        model_catalog_url: '',
        models_api_url: '',
        additional_drop_params: '',
        region: '',
        notes: '',
        enabled: true,
    };
}

function fromProvider(p: LlmProviderResponse): FormState {
    return {
        version: p.version ?? '1.0',
        key: p.key,
        display_name: p.display_name,
        category: (p.category) ?? '',
        default_auth_type: p.default_auth_type ?? '',
        default_api_url: p.default_api_url ?? '',
        developer_url: p.developer_url ?? '',
        api_key_url: p.api_key_url ?? '',
        pricing_url: p.pricing_url ?? '',
        model_catalog_url: p.model_catalog_url ?? '',
        models_api_url: p.models_api_url ?? '',
        additional_drop_params: (p.additional_drop_params ?? []).join(', '),
        region: p.region ?? '',
        notes: p.notes ?? '',
        enabled: p.enabled,
    };
}

function parseDropParams(input: string): string[] {
    return input
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
}

const LLProviderForm: React.FC<Props> = ({ provider, onClose, onSaved }) => {
    const { gatewayClient } = useGateway();
    const isEdit = provider !== null;
    const [form, setForm] = useState<FormState>(() =>
        provider ? fromProvider(provider) : emptyState(),
    );
    // Local copy of the provider so the icon panel reflects fresh
    // upload/delete results without forcing a parent refresh.
    const [current, setCurrent] = useState<LlmProviderResponse | null>(provider);
    const [saving, setSaving] = useState(false);
    const [activeFormTab, setActiveFormTab] = useState<'general' | 'audit'>('general');
    const [error, setError] = useState<string | null>(null);
    const [iconUploading, setIconUploading] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        setForm(provider ? fromProvider(provider) : emptyState());
        setCurrent(provider);
        setError(null);
    }, [provider]);

    const validate = (): string | null => {
        if (!form.display_name.trim()) {
            return 'Display name is required.';
        }
        if (!isEdit) {
            if (!form.key.trim()) {
                return 'Slug is required.';
            }
            if (!SLUG_RE.test(form.key.trim())) {
                return 'Slug must be lowercase alphanumeric with optional `-` or `_`, max 64 chars.';
            }
        }
        return null;
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        const validationError = validate();
        if (validationError) {
            setError(validationError);
            // The error banner lives on the General tab.
            setActiveFormTab('general');
            return;
        }
        setSaving(true);
        setError(null);
        try {
            // Helper: collapse blank strings to null/undefined so the
            // server gets a clean payload and unsets fields properly
            // on update.
            const trimOrNull = (v: string): string | null =>
                v.trim() === '' ? null : v.trim();
            const trimOrUndef = (v: string): string | undefined =>
                v.trim() === '' ? undefined : v.trim();

            const dropParams = parseDropParams(form.additional_drop_params);
            if (isEdit && current) {
                const payload: LlmProviderUpdateRequest = {
                    version: form.version.trim() || undefined,
                    display_name: form.display_name.trim(),
                    // Null, like the fields below: the endpoint is `exclude_unset`, so
                    // undefined would keep the old value when "—" is chosen.
                    category: (form.category || null) as LlmProviderCategory | undefined,
                    default_auth_type: (form.default_auth_type || null) as LlmProviderAuthType | undefined,
                    default_api_url: trimOrNull(form.default_api_url),
                    developer_url: trimOrNull(form.developer_url),
                    api_key_url: trimOrNull(form.api_key_url),
                    pricing_url: trimOrNull(form.pricing_url),
                    model_catalog_url: trimOrNull(form.model_catalog_url),
                    models_api_url: trimOrNull(form.models_api_url),
                    additional_drop_params: dropParams.length === 0 ? null : dropParams,
                    region: trimOrNull(form.region),
                    notes: trimOrNull(form.notes),
                    enabled: form.enabled,
                };
                await gatewayClient.updateProvider(current.id, payload);
                await onSaved(`Updated provider ${form.display_name}`);
            } else {
                const payload: LlmProviderCreateRequest = {
                    version: form.version.trim() || undefined,
                    key: form.key.trim(),
                    display_name: form.display_name.trim(),
                    category: form.category || undefined,
                    default_auth_type: form.default_auth_type || undefined,
                    default_api_url: trimOrUndef(form.default_api_url),
                    developer_url: trimOrUndef(form.developer_url),
                    api_key_url: trimOrUndef(form.api_key_url),
                    pricing_url: trimOrUndef(form.pricing_url),
                    model_catalog_url: trimOrUndef(form.model_catalog_url),
                    models_api_url: trimOrUndef(form.models_api_url),
                    additional_drop_params: dropParams.length === 0 ? undefined : dropParams,
                    region: trimOrUndef(form.region),
                    notes: trimOrUndef(form.notes),
                    enabled: form.enabled,
                };
                const created = await gatewayClient.createProvider(payload);
                await onSaved(`Created provider ${created.display_name}`);
            }
        } catch (err) {
            setError(errorMessage(err) ?? String(err));
            setActiveFormTab('general');
        } finally {
            setSaving(false);
        }
    };

    const handleIconUpload = async (file: File) => {
        if (!current) return;
        setIconUploading(true);
        setError(null);
        try {
            const updated = await gatewayClient.uploadProviderIcon(current.id, file);
            setCurrent(updated);
        } catch (err) {
            setError(`Icon upload failed: ${errorMessage(err) ?? err}`);
        } finally {
            setIconUploading(false);
            if (fileInputRef.current) {
                fileInputRef.current.value = '';
            }
        }
    };

    const handleIconDelete = async () => {
        if (!current) return;
        setIconUploading(true);
        setError(null);
        try {
            await gatewayClient.deleteProviderIcon(current.id);
            setCurrent({
                ...current,
                has_icon: false,
                icon_uploaded_at: null,
                icon_mime_type: null,
                icon_size: null,
            });
        } catch (err) {
            setError(`Icon delete failed: ${errorMessage(err) ?? err}`);
        } finally {
            setIconUploading(false);
        }
    };

    const headerLabel = isEdit
        ? `Edit Provider — ${provider?.display_name ?? ''}`
        : 'New Provider';

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-3xl max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default flex-shrink-0">
                    <div className="flex items-center gap-3">
                        <Globe className="w-5 h-5 text-primary-600" />
                        <h2 className="text-lg font-semibold text-default">{headerLabel}</h2>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <FormTabBar
                    tabs={[
                        { id: 'general', label: 'General' },
                        { id: 'audit', label: 'Audit Info' },
                    ] as const}
                    active={activeFormTab}
                    onChange={setActiveFormTab}
                />

                {/* Body */}
                <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="scroll-region">
                    {activeFormTab === 'audit' && (
                        <div className="p-6">
                            <AuditInfoTab
                                meta={{
                                    version: form.version ?? '1.0',
                                    created_by: provider?.created_by,
                                    created_at: provider?.created_at,
                                    updated_by: provider?.updated_by,
                                    updated_at: provider?.updated_at,
                                }}
                                onVersionChange={(v) => setForm({ ...form, version: v })}
                                isNew={!provider}
                            />
                        </div>
                    )}
                    <div className={`p-6 space-y-5 ${activeFormTab === 'general' ? '' : 'hidden'}`}>
                        {error && (
                            <div className="bg-red-50 border border-red-200 rounded-lg p-3 flex items-start gap-2">
                                <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
                                <pre className="text-sm text-red-700 whitespace-pre-wrap font-mono flex-1 min-h-0 min-w-0">
                                    {error}
                                </pre>
                                <button
                                    type="button"
                                    onClick={() => setError(null)}
                                    className="text-red-400 hover:text-red-600"
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            </div>
                        )}

                        {/* Identity */}
                        <section className="space-y-3">
                            <h3 className="text-sm font-semibold text-default uppercase tracking-wide">
                                Identity
                            </h3>
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Slug <span className="text-red-600">*</span>
                                    </label>
                                    <input
                                        type="text"
                                        value={form.key}
                                        disabled={isEdit}
                                        onChange={e =>
                                            setForm({ ...form, key: e.target.value.toLowerCase() })
                                        }
                                        placeholder="e.g. openai, anthropic, my_internal"
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:bg-surface-alt disabled:text-muted"
                                    />
                                    <p className="text-xs text-muted mt-1">
                                        Lowercase identifier — used by the proxy to dispatch
                                        models. Cannot be changed after create.
                                    </p>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Display name <span className="text-red-600">*</span>
                                    </label>
                                    <input
                                        type="text"
                                        value={form.display_name}
                                        onChange={e =>
                                            setForm({ ...form, display_name: e.target.value })
                                        }
                                        placeholder="e.g. OpenAI"
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    />
                                </div>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Category
                                    </label>
                                    <select
                                        value={form.category}
                                        onChange={e =>
                                            setForm({
                                                ...form,
                                                category: e.target.value as LlmProviderCategory,
                                            })
                                        }
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    >
                                        <option value="">—</option>
                                        {CATEGORY_OPTIONS.map(o => (
                                            <option key={o.value} value={o.value}>
                                                {o.label}
                                            </option>
                                        ))}
                                    </select>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Default auth type
                                    </label>
                                    <select
                                        value={form.default_auth_type}
                                        onChange={e =>
                                            setForm({
                                                ...form,
                                                default_auth_type: e.target.value as LlmProviderAuthType,
                                            })
                                        }
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    >
                                        <option value="">—</option>
                                        {AUTH_OPTIONS.map(o => (
                                            <option key={o.value} value={o.value}>
                                                {o.label}
                                            </option>
                                        ))}
                                    </select>
                                </div>
                            </div>
                        </section>

                        {/* URLs */}
                        <section className="space-y-3">
                            <h3 className="text-sm font-semibold text-default uppercase tracking-wide">
                                URLs
                            </h3>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Default API URL
                                </label>
                                <input
                                    type="text"
                                    value={form.default_api_url}
                                    onChange={e =>
                                        setForm({ ...form, default_api_url: e.target.value })
                                    }
                                    placeholder="https://api.openai.com/v1"
                                    className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Base URL pre-filled when an operator adds a new model
                                    under this provider.
                                </p>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Developer site
                                    </label>
                                    <input
                                        type="text"
                                        value={form.developer_url}
                                        onChange={e =>
                                            setForm({ ...form, developer_url: e.target.value })
                                        }
                                        placeholder="https://platform.openai.com/docs"
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        API key page
                                    </label>
                                    <input
                                        type="text"
                                        value={form.api_key_url}
                                        onChange={e =>
                                            setForm({ ...form, api_key_url: e.target.value })
                                        }
                                        placeholder="https://platform.openai.com/api-keys"
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    />
                                </div>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Pricing
                                    </label>
                                    <input
                                        type="text"
                                        value={form.pricing_url}
                                        onChange={e =>
                                            setForm({ ...form, pricing_url: e.target.value })
                                        }
                                        placeholder="https://openai.com/pricing"
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Model catalog (docs page)
                                    </label>
                                    <input
                                        type="text"
                                        value={form.model_catalog_url}
                                        onChange={e =>
                                            setForm({ ...form, model_catalog_url: e.target.value })
                                        }
                                        placeholder="https://platform.openai.com/docs/models"
                                        className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                    />
                                    <p className="text-xs text-muted mt-1">
                                        Human-facing docs link about this provider's
                                        models. Surfaces in the admin UI as a click-out
                                        anchor — the proxy never reads it.
                                    </p>
                                </div>
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Models API endpoint
                                </label>
                                <input
                                    type="text"
                                    value={form.models_api_url}
                                    onChange={e =>
                                        setForm({ ...form, models_api_url: e.target.value })
                                    }
                                    placeholder="https://api.openai.com/v1/models"
                                    className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Machine-callable <code className="font-mono">/models</code>{' '}
                                    endpoint. The proxy hits this with the model's API key as
                                    its <strong>catalog-probe</strong> health check —
                                    verifies auth + reachability + that the configured
                                    model name exists at the provider, all at zero cost.
                                    Leave blank to fall back to a real-inference probe
                                    (cheap for chat / TTS / STT / embeddings; for image
                                    and video providers, leaving this blank means the
                                    health badge can&apos;t honestly verify the upstream).
                                    Self-hosted providers (vLLM, LM Studio) can use
                                    {' '}<code className="font-mono">&lt;MODEL_API_URL&gt;/v1/models</code>
                                    {' '}as a templated value — the proxy substitutes the
                                    model row&apos;s <code className="font-mono">api_url</code>
                                    {' '}at probe time.
                                </p>
                            </div>
                        </section>

                        {/* Provider-wide drop list */}
                        <section className="space-y-3">
                            <h3 className="text-sm font-semibold text-default uppercase tracking-wide">
                                Request body — drop params
                            </h3>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Additional drop params (provider-wide)
                                </label>
                                <input
                                    type="text"
                                    value={form.additional_drop_params}
                                    onChange={e =>
                                        setForm({ ...form, additional_drop_params: e.target.value })
                                    }
                                    placeholder="logit_bias, stream_options, parallel_tool_calls"
                                    className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Comma-separated. The gateway removes these from requests to
                                    every model of this provider that has{' '}
                                    <strong>Apply the provider&apos;s dropped-parameters list</strong> on
                                    (the default). A model&apos;s own list is removed as well, whatever
                                    that switch says.
                                </p>
                            </div>
                        </section>

                        {/* Data residency.
                          * The proxy reads this region and intersects it
                          * with the tenant's data_residency profile at
                          * request time; mismatches return 403. */}
                        <section className="space-y-3">
                            <h3 className="text-sm font-semibold text-default uppercase tracking-wide">
                                Data residency
                            </h3>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Region
                                </label>
                                <input
                                    type="text"
                                    value={form.region}
                                    onChange={e =>
                                        setForm({ ...form, region: e.target.value })
                                    }
                                    placeholder="us-east-1"
                                    className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Lowercase alphanumeric + hyphen / underscore (e.g.{' '}
                                    <code className="bg-surface-raised px-1 rounded">us-east-1</code>,{' '}
                                    <code className="bg-surface-raised px-1 rounded">eu-west-1</code>,{' '}
                                    <code className="bg-surface-raised px-1 rounded">global</code>). Blank = unspecified — the
                                    tenant's <code className="bg-surface-raised px-1 rounded">block_global</code>{' '}
                                    flag decides whether unspecified is allowed.
                                </p>
                            </div>
                        </section>

                        {/* Icon — only meaningful after the row exists */}
                        {isEdit && current && (
                            <section className="space-y-3">
                                <h3 className="text-sm font-semibold text-default uppercase tracking-wide flex items-center gap-2">
                                    <ImageIcon className="w-4 h-4" />
                                    Icon
                                </h3>
                                <div className="flex items-center gap-4 p-3 bg-surface-alt border border-default rounded-lg">
                                    <ProviderIcon
                                        provider={current}
                                        api={gatewayClient}
                                        size={48}
                                    />
                                    <div className="flex-1 text-xs text-muted min-h-0 min-w-0">
                                        {current.has_icon ? (
                                            <>
                                                Current icon: {current.icon_mime_type}
                                                {current.icon_size && (
                                                    <> · {Math.round(current.icon_size / 1024 * 10) / 10} KB</>
                                                )}
                                            </>
                                        ) : (
                                            <>No icon uploaded — falling back to coloured initials.</>
                                        )}
                                    </div>
                                    <input
                                        ref={fileInputRef}
                                        type="file"
                                        accept="image/svg+xml,image/png,image/jpeg,image/webp"
                                        onChange={e => {
                                            const file = e.target.files?.[0];
                                            if (file) void handleIconUpload(file);
                                        }}
                                        className="hidden"
                                    />
                                    <button
                                        type="button"
                                        onClick={() => fileInputRef.current?.click()}
                                        disabled={iconUploading}
                                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                                    >
                                        <Upload className="w-4 h-4" />
                                        {current.has_icon ? 'Replace' : 'Upload'}
                                    </button>
                                    {current.has_icon && (
                                        <button
                                            type="button"
                                            onClick={() => void handleIconDelete()}
                                            disabled={iconUploading}
                                            className="p-1.5 text-red-600 hover:bg-red-50 rounded transition-colors disabled:opacity-50"
                                            title="Remove icon"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    )}
                                </div>
                            </section>
                        )}

                        {/* Notes / state */}
                        <section className="space-y-3">
                            <h3 className="text-sm font-semibold text-default uppercase tracking-wide">
                                Notes & state
                            </h3>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Notes
                                </label>
                                <textarea
                                    value={form.notes}
                                    onChange={e => setForm({ ...form, notes: e.target.value })}
                                    rows={3}
                                    placeholder="Optional internal notes — visible only to admins."
                                    className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                                />
                            </div>
                            <label className="flex items-center gap-2 text-sm">
                                <input
                                    type="checkbox"
                                    checked={form.enabled}
                                    onChange={e => setForm({ ...form, enabled: e.target.checked })}
                                    className="w-4 h-4"
                                />
                                <span className="text-secondary">
                                    Enabled — admins can attach LLM models to this provider
                                </span>
                            </label>
                        </section>
                    </div>

                    {/* Footer */}
                    <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-default flex-shrink-0">
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={saving}
                            className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={saving}
                            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 rounded-lg transition-colors disabled:opacity-50"
                        >
                            <Save className="w-4 h-4" />
                            {saving ? 'Saving…' : isEdit ? 'Save changes' : 'Create provider'}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
};

export default LLProviderForm;
