/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { AlertCircle, Check, Info, Settings, Shield, Globe, Loader } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { IdPConfig } from '../../lib/types';
import { asyncHandler } from '../../lib/asyncSafe';
import { refusalMessage } from '../../lib/serverRefusal';
import { unavailableProtocol } from './protocolAvailability';
import { CheckboxField, TextField } from './IdPFormFields';
import { errorKind } from '../../lib/narrow';

interface IdPServerFormProps {
    idp?: IdPConfig | null;
    isEdit?: boolean;
    onSave?: () => void;
    onSuccess?: () => void;
    onCancel: () => void;
}

/** The OIDC endpoint fields the form holds. */
const OIDC_FORM_FIELDS = [
    'well_known_url',
    'authorization_endpoint',
    'token_endpoint',
    'jwks_uri',
    'userinfo_endpoint',
    'revocation_endpoint',
    'introspection_endpoint',
] as const;

const IdPServerForm: React.FC<IdPServerFormProps> = ({
                                                         idp,
                                                         isEdit = false,
                                                         onSave,
                                                         onSuccess,
                                                         onCancel
                                                     }) => {
    const { createIdPConfig, updateIdPConfig } = useGateway();

    const [activeTab, setActiveTab] = useState('basic');
    const [submitting, setSubmitting] = useState(false);

    // Initial form state - memoized to prevent recreation. An identity
    // provider is an OIDC one: the console has no other protocol.
    const initialFormState = useRef({
        name: '',
        issuer: '',
        protocol: 'oidc',
        type: 'oidc_discoverable',
        well_known_url: '',
        authorization_endpoint: '',
        token_endpoint: '',
        jwks_uri: '',
        userinfo_endpoint: '',
        revocation_endpoint: '',
        introspection_endpoint: '',
        supports_pkce: true,
        supports_refresh_tokens: true,
        supports_client_credentials: false,
        scopes_supported: '',
        token_endpoint_auth_methods: 'client_secret_post',
        response_types_supported: 'code',
        grant_types_supported: 'authorization_code,refresh_token',
        discovery_cache_ttl: 3600,
        enabled: true,
        verified: false,
        description: '',
        logo_url: '',
        // Accepted `aud` values for workload-identity (agent)
        // tokens, one per line or comma-separated; split on submit.
        // Empty: the core accepts no workload tokens from this IdP.
        workload_audiences: '',
    });

    const [formData, setFormData] = useState(initialFormState.current);

    // A stored provider of another protocol (SAML) cannot be edited: shown
    // as OIDC, a save would turn it into an OIDC provider.
    const unsupportedProtocol = unavailableProtocol(idp);
    const [errors, setErrors] = useState<Record<string, string>>({});

    // Use ref to track the last loaded IdP ID to prevent unnecessary reloads
    const loadedIdpIdRef = useRef<string | null>(null);

    // Load initial data when component mounts or when a different idp is passed
    // Only load when the IdP ID actually changes, not on every render
    useEffect(() => {
        const idpId = idp?.id || null;

        // Only load if we haven't loaded this specific IdP yet
        if (idp && idpId !== loadedIdpIdRef.current) {
            setFormData({
                name: idp.name || '',
                issuer: idp.issuer || '',
                protocol: 'oidc',
                type: idp.type || 'oidc_discoverable',
                well_known_url: idp.well_known_url || '',
                authorization_endpoint: idp.authorization_endpoint || '',
                token_endpoint: idp.token_endpoint || '',
                jwks_uri: idp.jwks_uri || '',
                userinfo_endpoint: idp.userinfo_endpoint || '',
                revocation_endpoint: idp.revocation_endpoint || '',
                introspection_endpoint: idp.introspection_endpoint || '',
                supports_pkce: idp.supports_pkce ?? true,
                supports_refresh_tokens: idp.supports_refresh_tokens ?? true,
                supports_client_credentials: idp.supports_client_credentials ?? false,
                scopes_supported: Array.isArray(idp.scopes_supported)
                    ? idp.scopes_supported.join(', ')
                    : idp.scopes_supported || '',
                token_endpoint_auth_methods: Array.isArray(idp.token_endpoint_auth_methods)
                    ? idp.token_endpoint_auth_methods.join(', ')
                    : idp.token_endpoint_auth_methods || 'client_secret_post',
                response_types_supported: Array.isArray(idp.response_types_supported)
                    ? idp.response_types_supported.join(', ')
                    : idp.response_types_supported || 'code',
                grant_types_supported: Array.isArray(idp.grant_types_supported)
                    ? idp.grant_types_supported.join(', ')
                    : idp.grant_types_supported || 'authorization_code,refresh_token',
                discovery_cache_ttl: idp.discovery_cache_ttl || 3600,
                enabled: idp.enabled ?? true,
                verified: idp.verified ?? false,
                description: idp.description || '',
                logo_url: idp.logo_url || '',
                workload_audiences: Array.isArray(idp.workload_audiences)
                    ? idp.workload_audiences.join('\n')
                    : '',
            });
            loadedIdpIdRef.current = idpId;
        } else if (!idp && loadedIdpIdRef.current !== null) {
            // Reset form when switching from edit to create
            setFormData(initialFormState.current);
            loadedIdpIdRef.current = null;
        }
    }, [idp?.id]); // Only depend on the ID, not the whole object

    // Memoized handleChange to prevent recreation on every render
    const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
        const { name, value, type } = e.target;
        const checked = (e.target as HTMLInputElement).checked;

        // Update the form - React guarantees 'prev' is always the latest state.
        const nextValue = type === 'checkbox' ? checked : value;
        setFormData(prev => ({ ...prev, [name]: nextValue }));

        // Clear field error if exists
        if (errors[name]) {
            setErrors(prev => {
                const newErrors = { ...prev };
                delete newErrors[name];
                return newErrors;
            });
        }
    }, [errors]); // Only recreate if errors change

    const validateForm = () => {
        const newErrors: Record<string, string> = {};

        if (!formData.name.trim()) newErrors.name = 'Name is required';
        if (!formData.issuer.trim()) newErrors.issuer = 'Issuer is required';
        if (!formData.type) newErrors.type = 'Type is required';

        if (!formData.authorization_endpoint.trim()) {
            newErrors.authorization_endpoint = 'Authorization endpoint is required';
        }
        if (!formData.token_endpoint.trim()) {
            newErrors.token_endpoint = 'Token endpoint is required';
        }

        const urlFields = ['issuer', 'logo_url', ...OIDC_FORM_FIELDS] as const;

        urlFields.forEach(field => {
            const value = formData[field];
            if (value && typeof value === 'string' && value.trim()) {
                try {
                    new URL(value.trim());
                } catch {
                    newErrors[field] = 'Must be a valid URL';
                }
            }
        });

        if (formData.discovery_cache_ttl < 60) {
            newErrors.discovery_cache_ttl = 'Cache TTL must be at least 60 seconds';
        }

        setErrors(newErrors);
        return Object.keys(newErrors).length === 0;
    };

    const handleSubmit = async () => {
        if (!validateForm()) {
            return;
        }

        setSubmitting(true);
        try {
            const configData = {
                ...formData,
                scopes_supported: formData.scopes_supported
                    .split(',')
                    .map(s => s.trim())
                    .filter(Boolean),
                token_endpoint_auth_methods: formData.token_endpoint_auth_methods
                    .split(',')
                    .map(s => s.trim())
                    .filter(Boolean),
                response_types_supported: formData.response_types_supported
                    .split(',')
                    .map(s => s.trim())
                    .filter(Boolean),
                grant_types_supported: formData.grant_types_supported
                    .split(',')
                    .map(s => s.trim())
                    .filter(Boolean),
                // Always sent, so clearing the field clears the
                // audiences (no workload tokens accepted).
                workload_audiences: formData.workload_audiences
                    .split(/[\n,]/)
                    .map(s => s.trim())
                    .filter(Boolean),
            };

            // What the last save was refused for no longer stands.
            setErrors(prev => {
                if (!prev.submit) return prev;
                const rest = { ...prev };
                delete rest.submit;
                return rest;
            });

            if (isEdit && idp?.id) {
                await updateIdPConfig(idp.id, configData);
            } else {
                await createIdPConfig(configData);
            }

            if (onSuccess) onSuccess();
            if (onSave) onSave();
        } catch (error) {
            console.error('❌ Failed to save IdP config:', errorKind(error));
            setErrors(prev => ({
                ...prev,
                submit: refusalMessage(error, 'Failed to save configuration')
            }));
        } finally {
            setSubmitting(false);
        }
    };

    if (unsupportedProtocol) {
        return (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
                <div className="bg-surface rounded-lg shadow-xl max-w-xl w-full mx-4 flex flex-col" role="alertdialog" aria-labelledby="idp-unsupported-title">
                    <div className="px-6 py-4 border-b">
                        <h2 id="idp-unsupported-title" className="text-2xl font-semibold text-default">
                            Identity Provider
                        </h2>
                    </div>
                    <p className="px-6 py-4 text-sm text-secondary" data-testid="idp-unsupported-protocol">
                        This identity provider uses the protocol <span className="font-mono">{unsupportedProtocol}</span>,
                        which this Admin Console cannot edit. It has not been changed.
                    </p>
                    <div className="px-6 py-4 bg-surface-alt border-t flex justify-end rounded-b-lg">
                        <button
                            type="button"
                            onClick={onCancel}
                            className="px-4 py-2 border border-strong rounded-md text-secondary hover:bg-surface-raised transition-colors"
                        >
                            Close
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full mx-4 max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="px-6 py-4 border-b flex-shrink-0">
                    <h2 className="text-2xl font-semibold text-default">
                        {isEdit ? 'Edit Identity Provider' : 'Add New Identity Provider'}
                    </h2>
                    <p className="text-sm text-secondary mt-1">
                        Configure OIDC / OAuth 2.0 identity provider settings
                    </p>
                </div>

                {/* Tab Navigation */}
                <div className="flex border-b flex-shrink-0">
                    {[
                        { id: 'basic', label: 'Basic Settings', icon: Info },
                        { id: 'endpoints', label: 'Endpoints', icon: Globe },
                        { id: 'oauth', label: 'OAuth Settings', icon: Shield },
                        { id: 'advanced', label: 'Advanced', icon: Settings },
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

                {/* Form Content - Scrollable */}
                <div className="scroll-region px-6 py-4">
                    {/* Basic Settings Tab */}
                    {activeTab === 'basic' && (
                        <div>
                            <TextField
                                label="Provider Name"
                                name="name"
                                value={formData.name}
                                onChange={handleChange}
                                errors={errors}
                                required
                                placeholder="e.g., Google, Okta, Auth0"
                                helpText="A friendly name for this identity provider"
                            />

                            <TextField
                                label="Issuer URL"
                                name="issuer"
                                value={formData.issuer}
                                onChange={handleChange}
                                errors={errors}
                                required
                                placeholder="https://accounts.google.com"
                                helpText="The issuer identifier from the IdP"
                            />

                                    <div className="mb-4">
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Provider Type <span className="text-red-500">*</span>
                                        </label>
                                        <select
                                            name="type"
                                            value={formData.type}
                                            onChange={handleChange}
                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                        >
                                            <option value="oidc_discoverable">OIDC (Auto-Discovery)</option>
                                            <option value="oidc">OIDC (Manual Configuration)</option>
                                            <option value="oauth2">OAuth 2.0</option>
                                        </select>
                                        <p className="text-xs text-muted mt-1">
                                            Auto-discovery uses the .well-known endpoint for configuration
                                        </p>
                                    </div>

                                    <TextField
                                        label="Well-Known Discovery URL"
                                        name="well_known_url"
                                        value={formData.well_known_url}
                                        onChange={handleChange}
                                        errors={errors}
                                        placeholder="https://accounts.google.com/.well-known/openid-configuration"
                                        helpText="OIDC discovery endpoint (optional if manually configuring)"
                                    />

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Description
                                </label>
                                <textarea
                                    name="description"
                                    value={formData.description}
                                    onChange={handleChange}
                                    rows={3}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                    placeholder="Additional notes about this provider"
                                />
                            </div>

                            <TextField
                                label="Logo URL"
                                name="logo_url"
                                value={formData.logo_url}
                                onChange={handleChange}
                                errors={errors}
                                placeholder="https://example.com/logo.png"
                                helpText="Optional logo to display in login UI"
                            />

                            <CheckboxField
                                label="Enabled"
                                name="enabled"
                                checked={formData.enabled}
                                onChange={handleChange}
                                helpText="Allow users to authenticate with this provider"
                            />
                        </div>
                    )}

                    {/* Endpoints Tab */}
                    {activeTab === 'endpoints' && (
                        <div>
                            <TextField
                                label="Authorization Endpoint"
                                name="authorization_endpoint"
                                value={formData.authorization_endpoint}
                                onChange={handleChange}
                                errors={errors}
                                required
                                placeholder="https://accounts.google.com/o/oauth2/v2/auth"
                                helpText="OAuth 2.0 authorization endpoint"
                            />

                            <TextField
                                label="Token Endpoint"
                                name="token_endpoint"
                                value={formData.token_endpoint}
                                onChange={handleChange}
                                errors={errors}
                                required
                                placeholder="https://oauth2.googleapis.com/token"
                                helpText="OAuth 2.0 token endpoint"
                            />

                            <TextField
                                label="JWKS URI"
                                name="jwks_uri"
                                value={formData.jwks_uri}
                                onChange={handleChange}
                                errors={errors}
                                placeholder="https://www.googleapis.com/oauth2/v3/certs"
                                helpText="JSON Web Key Set URI for validating tokens"
                            />

                            <div className="mb-4">
                                <label
                                    htmlFor="idp-workload-audiences"
                                    className="block text-sm font-medium text-secondary mb-1"
                                >
                                    Workload token audiences
                                </label>
                                <textarea
                                    id="idp-workload-audiences"
                                    name="workload_audiences"
                                    value={formData.workload_audiences}
                                    onChange={handleChange}
                                    rows={2}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                    placeholder="api://brutor-gateway"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Workload identity (agents) tokens from this IdP are accepted only when their{' '}
                                    <code>aud</code> matches one of these. Leave empty to not accept workload tokens
                                    from this IdP. One per line or comma-separated.
                                </p>
                            </div>

                            <TextField
                                label="UserInfo Endpoint"
                                name="userinfo_endpoint"
                                value={formData.userinfo_endpoint}
                                onChange={handleChange}
                                errors={errors}
                                placeholder="https://openidconnect.googleapis.com/v1/userinfo"
                                helpText="OIDC userinfo endpoint for fetching user details"
                            />

                            <TextField
                                label="Revocation Endpoint"
                                name="revocation_endpoint"
                                value={formData.revocation_endpoint}
                                onChange={handleChange}
                                errors={errors}
                                placeholder="https://oauth2.googleapis.com/revoke"
                                helpText="Token revocation endpoint (optional)"
                            />

                            <TextField
                                label="Introspection Endpoint"
                                name="introspection_endpoint"
                                value={formData.introspection_endpoint}
                                onChange={handleChange}
                                errors={errors}
                                placeholder="https://oauth2.googleapis.com/tokeninfo"
                                helpText="Token introspection endpoint (optional)"
                            />
                        </div>
                    )}

                    {/* OAuth Settings Tab */}
                    {activeTab === 'oauth' && (
                        <div>
                            <CheckboxField
                                label="Supports PKCE"
                                name="supports_pkce"
                                checked={formData.supports_pkce}
                                onChange={handleChange}
                                helpText="Proof Key for Code Exchange (recommended for security)"
                            />

                            <CheckboxField
                                label="Supports Refresh Tokens"
                                name="supports_refresh_tokens"
                                checked={formData.supports_refresh_tokens}
                                onChange={handleChange}
                                helpText="Allow long-lived refresh tokens"
                            />

                            <CheckboxField
                                label="Supports Client Credentials"
                                name="supports_client_credentials"
                                checked={formData.supports_client_credentials}
                                onChange={handleChange}
                                helpText="Support machine-to-machine authentication"
                            />

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Globally Supported Scopes
                                </label>
                                <input
                                    type="text"
                                    name="scopes_supported"
                                    value={formData.scopes_supported}
                                    onChange={handleChange}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                    placeholder="openid, profile, email"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Comma-separated list of supported OAuth scopes. See client configuration for what is specifically enabled.
                                </p>
                            </div>
                        </div>
                    )}

                    {/* Advanced Configuration Tab */}
                    {activeTab === 'advanced' && (
                        <div>
                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Token Endpoint Auth Methods
                                </label>
                                <input
                                    type="text"
                                    name="token_endpoint_auth_methods"
                                    value={formData.token_endpoint_auth_methods}
                                    onChange={handleChange}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                    placeholder="client_secret_post, client_secret_basic"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Comma-separated list of supported authentication methods
                                </p>
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Response Types Supported
                                </label>
                                <input
                                    type="text"
                                    name="response_types_supported"
                                    value={formData.response_types_supported}
                                    onChange={handleChange}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                    placeholder="code, token"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Comma-separated list of OAuth response types
                                </p>
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Grant Types Supported
                                </label>
                                <input
                                    type="text"
                                    name="grant_types_supported"
                                    value={formData.grant_types_supported}
                                    onChange={handleChange}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                                    placeholder="authorization_code, refresh_token, client_credentials"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Comma-separated list of OAuth grant types
                                </p>
                            </div>

                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Discovery Cache TTL (seconds)
                                </label>
                                <input
                                    type="number"
                                    name="discovery_cache_ttl"
                                    value={formData.discovery_cache_ttl}
                                    onChange={handleChange}
                                    min={60}
                                    className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                                        errors.discovery_cache_ttl ? 'border-red-500' : 'border-strong'
                                    }`}
                                />
                                <p className="text-xs text-muted mt-1">
                                    How long to cache discovery data (minimum 60 seconds)
                                </p>
                                {errors.discovery_cache_ttl && (
                                    <div className="flex items-center gap-1 mt-1 text-red-500 text-sm">
                                        <AlertCircle size={14} />
                                        <span>{errors.discovery_cache_ttl}</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    )}

                </div>

                {/* Error Display */}
                {/* A save the Control Plane refused (a 422 among them), in its words. */}
                {errors.submit && (
                    <div className="px-6 py-3 bg-red-50 border-t border-red-200" role="alert" data-testid="idp-form-submit-error">
                        <div className="flex items-center gap-2 text-red-700">
                            <AlertCircle size={16} />
                            <span className="text-sm">{errors.submit}</span>
                        </div>
                    </div>
                )}

                {/* Footer Actions */}
                <div className="px-6 py-4 bg-surface-alt border-t flex gap-3 justify-end rounded-b-lg flex-shrink-0">
                    <button
                        type="button"
                        onClick={onCancel}
                        disabled={submitting}
                        className="px-4 py-2 border border-strong rounded-md text-secondary hover:bg-surface-raised transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={asyncHandler(handleSubmit, 'click')}
                        disabled={submitting}
                        className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 disabled:opacity-50"
                    >
                        {submitting ? (
                            <>
                                <Loader size={16} className="animate-spin" />
                                <span>{isEdit ? 'Updating...' : 'Creating...'}</span>
                            </>
                        ) : (
                            <>
                                <Check size={16} />
                                <span>{isEdit ? 'Update Provider' : 'Create Provider'}</span>
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default IdPServerForm;