/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AuditExportForm — create / edit modal for an audit export destination.
 *
 * Mirrors the structure of IdPServerForm (modal + tabbed layout) but
 * scaled down — this surface has fewer fields. Two tabs:
 *   - Basic: name, type, schedule, enabled
 *   - Backend: type-specific config (S3 bucket / webhook URL, secrets,
 *     optional field allowlist)
 *
 * Secrets: the Control Plane stores `secret_access_key`, `bearer_token`,
 *   `token` and a webhook's `headers`, and never returns one. A response
 *   says whether each is stored (`has_secret_access_key`,
 *   `has_bearer_token`, `has_token`, `has_headers`, and `header_names`).
 *   A secret field therefore starts empty. On an update, a secret that is
 *   not sent stays as stored, a typed value replaces it, and an empty
 *   string removes it (offered for the bearer token only: the other two
 *   are needed by their destination type).
 *
 *   An update's `config` replaces the stored configuration except its
 *   secrets, so the keys the form has no field for are sent back as they
 *   were read.
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { AlertCircle, Info, Database, Loader } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { AuditExportDestination } from '../../lib/types';
import { asyncHandler } from '../../lib/asyncSafe';
import StoredTokenField from '../common/StoredTokenField';

interface AuditExportFormProps {
    destination?: AuditExportDestination | null;
    isEdit?: boolean;
    onSuccess?: () => void;
    onCancel: () => void;
}

/** The configuration keys of a destination type the form has no field for. An update sends them back as read. */
/** Column names shown as examples under the allow-list. The Control Plane holds the list and refuses a name that is not in it. */
const EXAMPLE_PROXY_LOG_COLUMNS = ['id', 'timestamp', 'proxy_type', 'success', 'duration_ms'];

const CARRIED_CONFIG_KEYS: Record<AuditExportDestination['destination_type'], readonly string[]> = {
    s3: ['endpoint_url', 'sse', 'sse_kms_key_id'],
    webhook: [],
    splunk_hec: [],
};

interface FieldErrorProps { name: string; errors: Record<string, string>; }
const FieldError: React.FC<FieldErrorProps> = ({ name, errors }) =>
    errors[name] ? (
        <div className="flex items-center gap-1 mt-1 text-red-500 text-sm">
            <AlertCircle size={14} />
            <span>{errors[name]}</span>
        </div>
    ) : null;


const AuditExportForm: React.FC<AuditExportFormProps> = ({
    destination,
    isEdit = false,
    onSuccess,
    onCancel,
}) => {
    const { createAuditExportDestination, updateAuditExportDestination } = useGateway();

    const [activeTab, setActiveTab] = useState<'basic' | 'backend'>('basic');
    const [submitting, setSubmitting] = useState(false);

    // initialFormState lives in a ref so React's setState doesn't
    // recreate it on every render (same trick as IdPServerForm).
    const initialFormState = useRef({
        name: '',
        destination_type: 's3' as 's3' | 'webhook' | 'splunk_hec',
        enabled: true,
        interval_seconds: 3600,
        // S3 fields
        s3_bucket: '',
        s3_prefix: 'brutor/proxy-logs',
        s3_region: 'us-east-1',
        s3_access_key_id: '',
        s3_secret_access_key: '',
        // Webhook fields
        webhook_url: '',
        webhook_bearer_token: '',
        // Splunk HEC fields
        splunk_url: '',
        splunk_token: '',
        splunk_index: '',
        splunk_sourcetype: 'brutor:proxy_log',
        splunk_source: '',
        splunk_host: '',
        // Shared
        field_allowlist: '',  // CSV; empty = no filter
    });

    const [formData, setFormData] = useState(initialFormState.current);
    const [errors, setErrors] = useState<Record<string, string>>({});
    // Whether removing the stored bearer token of a webhook is asked for.
    const [removeBearerToken, setRemoveBearerToken] = useState(false);
    const loadedDestIdRef = useRef<string | null>(null);

    // Preload from an existing destination on edit. Only fires when
    // the destination ID changes — guards against a re-render loop.
    useEffect(() => {
        const destId = destination?.id || null;
        if (destination && destId !== loadedDestIdRef.current) {
            const cfg = (destination.config || {}) as Record<string, unknown>;
            setFormData({
                name: destination.name || '',
                destination_type: destination.destination_type,
                enabled: destination.enabled ?? true,
                interval_seconds: destination.interval_seconds ?? 3600,
                s3_bucket: String(cfg.bucket ?? ''),
                s3_prefix: String(cfg.prefix ?? 'brutor/proxy-logs'),
                s3_region: String(cfg.region ?? 'us-east-1'),
                s3_access_key_id: String(cfg.access_key_id ?? ''),
                s3_secret_access_key: '',
                webhook_url: String(cfg.url ?? ''),
                webhook_bearer_token: '',
                // Splunk HEC preload — `url` is shared with webhook
                // shape (different field meaning per backend). Read
                // it once; whichever destination_type is active
                // picks it up.
                splunk_url: String(cfg.url ?? ''),
                splunk_token: '',
                splunk_index: String(cfg.index ?? ''),
                splunk_sourcetype: String(cfg.sourcetype ?? 'brutor:proxy_log'),
                splunk_source: String(cfg.source ?? ''),
                splunk_host: String(cfg.host ?? ''),
                field_allowlist: Array.isArray(destination.field_allowlist)
                    ? destination.field_allowlist.join(', ')
                    : '',
            });
            setRemoveBearerToken(false);
            loadedDestIdRef.current = destId;
        } else if (!destination && loadedDestIdRef.current !== null) {
            setFormData(initialFormState.current);
            loadedDestIdRef.current = null;
        }
    }, [destination?.id]);

    // What the Control Plane says is stored. Undefined for a destination that is not saved yet.
    const storedConfig = isEdit && destination ? destination.config : undefined;
    const stored = (flag: string): boolean | undefined => (storedConfig ? storedConfig[flag] === true : undefined);
    const storedHeaderNames = Array.isArray(storedConfig?.header_names)
        ? storedConfig.header_names.filter((name): name is string => typeof name === 'string')
        : [];

    const setSecret = (name: 's3_secret_access_key' | 'webhook_bearer_token' | 'splunk_token', typed: string) => {
        setFormData(prev => ({ ...prev, [name]: typed }));
        if (errors[name]) {
            setErrors(prev => {
                const newErrors = { ...prev };
                delete newErrors[name];
                return newErrors;
            });
        }
    };

    const handleChange = useCallback(
        (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
            const { name, value, type } = e.target;
            const checked = (e.target as HTMLInputElement).checked;
            setFormData(prev => ({
                ...prev,
                [name]: type === 'checkbox' ? checked
                    : type === 'number' ? Number(value)
                    : value,
            }));
            if (errors[name]) {
                setErrors(prev => {
                    const newErrors = { ...prev };
                    delete newErrors[name];
                    return newErrors;
                });
            }
        }, [errors]);

    const validateForm = (): boolean => {
        const newErrors: Record<string, string> = {};
        if (!formData.name.trim()) newErrors.name = 'Name is required';
        if (formData.interval_seconds < 60) {
            newErrors.interval_seconds = 'Interval must be at least 60 seconds';
        }
        if (formData.destination_type === 's3') {
            if (!formData.s3_bucket.trim()) newErrors.s3_bucket = 'Bucket is required';
            if (!formData.s3_access_key_id.trim()) newErrors.s3_access_key_id = 'Access key ID is required';
            // Required unless one is stored: left empty, the stored one is kept.
            if (!stored('has_secret_access_key') && !formData.s3_secret_access_key.trim()) {
                newErrors.s3_secret_access_key = 'Secret access key is required';
            }
        } else if (formData.destination_type === 'webhook') {
            if (!formData.webhook_url.trim()) newErrors.webhook_url = 'URL is required';
            else {
                try { new URL(formData.webhook_url.trim()); }
                catch { newErrors.webhook_url = 'Must be a valid URL'; }
            }
        } else if (formData.destination_type === 'splunk_hec') {
            if (!formData.splunk_url.trim()) newErrors.splunk_url = 'HEC URL is required';
            else {
                try { new URL(formData.splunk_url.trim()); }
                catch { newErrors.splunk_url = 'Must be a valid URL'; }
            }
            // Required unless one is stored: left empty, the stored one is kept.
            if (!stored('has_token') && !formData.splunk_token.trim()) {
                newErrors.splunk_token = 'HEC token is required';
            }
        }
        setErrors(newErrors);
        return Object.keys(newErrors).length === 0;
    };

    const buildConfigPayload = (): Record<string, unknown> => {
        // The type-specific configuration. A secret is named only when one
        // was typed, or, for the bearer token, as an empty string when
        // removing the stored one was asked for.
        const config: Record<string, unknown> = {};
        for (const key of CARRIED_CONFIG_KEYS[formData.destination_type]) {
            const value = storedConfig?.[key];
            if (value !== undefined && value !== null) config[key] = value;
        }
        if (formData.destination_type === 's3') {
            config.bucket = formData.s3_bucket.trim();
            if (formData.s3_prefix.trim()) config.prefix = formData.s3_prefix.trim();
            if (formData.s3_region.trim()) config.region = formData.s3_region.trim();
            config.access_key_id = formData.s3_access_key_id.trim();
            if (formData.s3_secret_access_key) config.secret_access_key = formData.s3_secret_access_key;
        } else if (formData.destination_type === 'webhook') {
            config.url = formData.webhook_url.trim();
            if (removeBearerToken) config.bearer_token = '';
            else if (formData.webhook_bearer_token) config.bearer_token = formData.webhook_bearer_token;
        } else if (formData.destination_type === 'splunk_hec') {
            config.url = formData.splunk_url.trim();
            if (formData.splunk_token) config.token = formData.splunk_token;
            if (formData.splunk_index.trim()) config.index = formData.splunk_index.trim();
            if (formData.splunk_sourcetype.trim()) config.sourcetype = formData.splunk_sourcetype.trim();
            if (formData.splunk_source.trim()) config.source = formData.splunk_source.trim();
            if (formData.splunk_host.trim()) config.host = formData.splunk_host.trim();
        }
        return config;
    };

    const buildAllowlist = (): string[] | null => {
        const tokens = formData.field_allowlist
            .split(',').map(s => s.trim()).filter(Boolean);
        return tokens.length ? tokens : null;
    };

    const handleSubmit = async () => {
        if (!validateForm()) return;
        setSubmitting(true);
        try {
            const payload = {
                name: formData.name.trim(),
                enabled: formData.enabled,
                interval_seconds: formData.interval_seconds,
                config: buildConfigPayload(),
                field_allowlist: buildAllowlist(),
            };
            if (isEdit && destination?.id) {
                await updateAuditExportDestination(destination.id, payload);
            } else {
                await createAuditExportDestination({
                    ...payload,
                    destination_type: formData.destination_type,
                });
            }
            if (onSuccess) onSuccess();
        } catch (error) {
            setErrors(prev => ({
                ...prev,
                submit: error instanceof Error ? error.message : 'Failed to save destination',
            }));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full mx-4 max-h-[90vh] flex flex-col">
                <div className="px-6 py-4 border-b flex-shrink-0">
                    <h2 className="text-2xl font-semibold text-default">
                        {isEdit ? 'Edit Audit Export Destination' : 'New Audit Export Destination'}
                    </h2>
                    <p className="text-sm text-secondary mt-1">
                        Configure where proxy logs are exported on a schedule (S3 or webhook)
                    </p>
                </div>

                <div className="flex border-b flex-shrink-0">
                    {[
                        { id: 'basic' as const, label: 'Basic', icon: Info },
                        { id: 'backend' as const, label: 'Backend', icon: Database },
                    ].map(({ id, label, icon: Icon }) => (
                        <button
                            key={id}
                            onClick={() => setActiveTab(id)}
                            className={`flex items-center space-x-2 px-6 py-3 border-b-2 transition-colors ${
                                activeTab === id
                                    ? 'border-blue-500 text-blue-600 bg-blue-50'
                                    : 'border-transparent text-secondary hover:text-default hover:bg-surface-alt'
                            }`}
                        >
                            <Icon size={16} />
                            <span className="font-medium">{label}</span>
                        </button>
                    ))}
                </div>

                <div className="scroll-region px-6 py-4">
                    {activeTab === 'basic' && (
                        <div>
                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Name <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="text" name="name" value={formData.name}
                                    onChange={handleChange}
                                    placeholder="e.g., Splunk evidence vault"
                                    className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 ${errors.name ? 'border-red-500' : 'border-strong'}`}
                                />
                                <FieldError name="name" errors={errors} />
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Destination Type <span className="text-red-500">*</span>
                                </label>
                                <select
                                    name="destination_type"
                                    value={formData.destination_type}
                                    onChange={handleChange}
                                    disabled={isEdit}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:cursor-not-allowed"
                                >
                                    <option value="s3">S3 / S3-compatible (MinIO, R2, GCS, Wasabi)</option>
                                    <option value="splunk_hec">Splunk HEC (purpose-built, event envelope)</option>
                                    <option value="webhook">Generic webhook (Datadog, custom HTTP collector)</option>
                                </select>
                                {isEdit && (
                                    <p className="text-xs text-muted mt-1">
                                        Destination type cannot be changed after creation — delete and recreate to switch.
                                    </p>
                                )}
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Interval (seconds) <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="number" name="interval_seconds"
                                    value={formData.interval_seconds}
                                    onChange={handleChange}
                                    min={60}
                                    className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 ${errors.interval_seconds ? 'border-red-500' : 'border-strong'}`}
                                />
                                <p className="text-xs text-muted mt-1">
                                    How often the exporter runs. Min 60s, typical: 300 (5 min SIEM), 3600 (hourly S3), 86400 (daily).
                                </p>
                                <FieldError name="interval_seconds" errors={errors} />
                            </div>

                            <div className="mb-4">
                                <label className="flex items-center">
                                    <input
                                        type="checkbox" name="enabled"
                                        checked={formData.enabled}
                                        onChange={handleChange}
                                        className="mr-2"
                                    />
                                    <span className="text-sm font-medium text-secondary">Enabled</span>
                                </label>
                                <p className="text-xs text-muted mt-1 ml-6">
                                    Disabled destinations stay in the table but are skipped by the scheduler.
                                </p>
                            </div>
                        </div>
                    )}

                    {activeTab === 'backend' && formData.destination_type === 's3' && (
                        <div>
                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Bucket <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="text" name="s3_bucket" value={formData.s3_bucket}
                                    onChange={handleChange}
                                    placeholder="acme-audit-vault"
                                    className={`w-full px-3 py-2 border rounded-md ${errors.s3_bucket ? 'border-red-500' : 'border-strong'}`}
                                />
                                <FieldError name="s3_bucket" errors={errors} />
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">Prefix</label>
                                <input
                                    type="text" name="s3_prefix" value={formData.s3_prefix}
                                    onChange={handleChange}
                                    placeholder="brutor/proxy-logs"
                                    className="w-full px-3 py-2 border border-strong rounded-md"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Objects land at &lt;prefix&gt;/&lt;tenant&gt;/&lt;YYYY&gt;/&lt;MM&gt;/&lt;DD&gt;/&lt;HHMMSS&gt;.jsonl.gz
                                </p>
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">Region</label>
                                <input
                                    type="text" name="s3_region" value={formData.s3_region}
                                    onChange={handleChange}
                                    placeholder="us-east-1"
                                    className="w-full px-3 py-2 border border-strong rounded-md"
                                />
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Access Key ID <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="text" name="s3_access_key_id"
                                    value={formData.s3_access_key_id}
                                    onChange={handleChange}
                                    placeholder="AKIAIOSFODNN7EXAMPLE"
                                    className={`w-full px-3 py-2 border rounded-md font-mono text-sm ${errors.s3_access_key_id ? 'border-red-500' : 'border-strong'}`}
                                />
                                <FieldError name="s3_access_key_id" errors={errors} />
                            </div>

                            <div className="mb-4">
                                <StoredTokenField
                                    id="audit-export-secret-access-key"
                                    label="Secret Access Key"
                                    value={formData.s3_secret_access_key}
                                    onChange={(typed) => setSecret('s3_secret_access_key', typed)}
                                    stored={stored('has_secret_access_key')}
                                    removeStored={false}
                                    onRemoveStoredChange={() => undefined}
                                    owner="destination"
                                    storedTestId="audit-export-secret-access-key-stored"
                                    noun="secret access key"
                                    removable={false}
                                    emptyPlaceholder="Required. Stored encrypted."
                                />
                                <FieldError name="s3_secret_access_key" errors={errors} />
                            </div>

                            {renderFieldAllowlist()}
                        </div>
                    )}

                    {activeTab === 'backend' && formData.destination_type === 'webhook' && (
                        <div>
                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Webhook URL <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="url" name="webhook_url" value={formData.webhook_url}
                                    onChange={handleChange}
                                    placeholder="https://collector.example.com/api/v2/logs"
                                    className={`w-full px-3 py-2 border rounded-md ${errors.webhook_url ? 'border-red-500' : 'border-strong'}`}
                                />
                                <FieldError name="webhook_url" errors={errors} />
                                <p className="text-xs text-muted mt-1">
                                    For Splunk HEC specifically, switch destination type to "Splunk HEC" — it sends the right auth scheme + event envelope automatically.
                                </p>
                            </div>

                            <div className="mb-4">
                                <StoredTokenField
                                    id="audit-export-bearer-token"
                                    label="Bearer Token"
                                    value={formData.webhook_bearer_token}
                                    onChange={(typed) => setSecret('webhook_bearer_token', typed)}
                                    stored={stored('has_bearer_token')}
                                    removeStored={removeBearerToken}
                                    onRemoveStoredChange={setRemoveBearerToken}
                                    owner="destination"
                                    storedTestId="audit-export-bearer-token-stored"
                                    noun="bearer token"
                                    emptyPlaceholder="Optional. Sent as Authorization: Bearer"
                                />
                            </div>

                            {stored('has_headers') && (
                                <p className="mb-4 text-xs text-muted" data-testid="audit-export-headers-stored">
                                    Stored request headers: {storedHeaderNames.join(', ') || 'names not listed'}. Their values are never shown, and saving keeps them.
                                </p>
                            )}

                            {renderFieldAllowlist()}
                        </div>
                    )}

                    {/* Splunk HEC backend tab */}
                    {activeTab === 'backend' && formData.destination_type === 'splunk_hec' && (
                        <div>
                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    HEC URL <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="url" name="splunk_url" value={formData.splunk_url}
                                    onChange={handleChange}
                                    placeholder="https://splunk.acme.com:8088/services/collector/event"
                                    className={`w-full px-3 py-2 border rounded-md ${errors.splunk_url ? 'border-red-500' : 'border-strong'}`}
                                />
                                <p className="text-xs text-muted mt-1">
                                    Use the <code className="bg-surface-raised px-1 rounded">/services/collector/event</code> endpoint — Brutor sends the HEC event envelope per record.
                                </p>
                                <FieldError name="splunk_url" errors={errors} />
                            </div>

                            <div className="mb-4">
                                <StoredTokenField
                                    id="audit-export-token"
                                    label="HEC Token"
                                    value={formData.splunk_token}
                                    onChange={(typed) => setSecret('splunk_token', typed)}
                                    stored={stored('has_token')}
                                    removeStored={false}
                                    onRemoveStoredChange={() => undefined}
                                    owner="destination"
                                    storedTestId="audit-export-token-stored"
                                    noun="Splunk HEC token"
                                    removable={false}
                                    emptyPlaceholder="From Splunk → Data Inputs → HTTP Event Collector"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Encrypted at rest. Sent as <code className="bg-surface-raised px-1 rounded">Authorization: Splunk &lt;token&gt;</code>.
                                </p>
                                <FieldError name="splunk_token" errors={errors} />
                            </div>

                            <div className="grid grid-cols-2 gap-4 mb-4">
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">Index (optional)</label>
                                    <input
                                        type="text" name="splunk_index" value={formData.splunk_index}
                                        onChange={handleChange}
                                        placeholder="audit_prod"
                                        className="w-full px-3 py-2 border border-strong rounded-md text-sm font-mono"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">Sourcetype</label>
                                    <input
                                        type="text" name="splunk_sourcetype" value={formData.splunk_sourcetype}
                                        onChange={handleChange}
                                        placeholder="brutor:proxy_log"
                                        className="w-full px-3 py-2 border border-strong rounded-md text-sm font-mono"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">Source (optional)</label>
                                    <input
                                        type="text" name="splunk_source" value={formData.splunk_source}
                                        onChange={handleChange}
                                        placeholder="brutor-tenant-1"
                                        className="w-full px-3 py-2 border border-strong rounded-md text-sm font-mono"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">Host (optional)</label>
                                    <input
                                        type="text" name="splunk_host" value={formData.splunk_host}
                                        onChange={handleChange}
                                        placeholder="gateway-prod-01"
                                        className="w-full px-3 py-2 border border-strong rounded-md text-sm font-mono"
                                    />
                                </div>
                            </div>

                            {renderFieldAllowlist()}
                        </div>
                    )}
                </div>

                <div className="px-6 py-4 border-t flex-shrink-0 flex justify-between items-center">
                    {errors.submit && (
                        <div className="text-red-500 text-sm flex items-center">
                            <AlertCircle size={14} className="mr-1" />
                            {errors.submit}
                        </div>
                    )}
                    <div className="flex space-x-2 ml-auto">
                        <button
                            type="button" onClick={onCancel} disabled={submitting}
                            className="px-4 py-2 border border-strong rounded-md text-secondary hover:bg-surface-alt"
                        >
                            Cancel
                        </button>
                        <button
                            type="button" onClick={asyncHandler(handleSubmit, 'click')} disabled={submitting}
                            className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700 disabled:opacity-50 flex items-center"
                        >
                            {submitting && <Loader size={14} className="animate-spin mr-2" />}
                            {isEdit ? 'Save Changes' : 'Create Destination'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );

    function renderFieldAllowlist() {
        return (
            <div className="mb-4 mt-6 pt-4 border-t border-default">
                <label htmlFor="audit-export-field-allowlist" className="block text-sm font-medium text-secondary mb-1">
                    Field Allowlist (optional)
                </label>
                <textarea
                    id="audit-export-field-allowlist"
                    name="field_allowlist" value={formData.field_allowlist}
                    onChange={handleChange}
                    placeholder="Empty = ship every column. To redact, list specific columns comma-separated."
                    rows={3}
                    className="w-full px-3 py-2 border border-strong rounded-md font-mono text-xs"
                />
                <p className="text-xs text-muted mt-1" data-testid="audit-export-allowlist-help">
                    Proxy-log column names, for example {EXAMPLE_PROXY_LOG_COLUMNS.join(', ')}.
                    Every name must be a column that exists: a list that names one that does not is refused, and the refusal lists the columns.
                    Include <code className="bg-surface-raised px-1 rounded">chain</code> to keep each row verifiable.
                </p>
            </div>
        );
    }
};

export default AuditExportForm;
