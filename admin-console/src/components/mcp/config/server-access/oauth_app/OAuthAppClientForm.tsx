/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import { AlertCircle, Shield, X } from 'lucide-react';
import { errorMessage } from '../../../../../lib/narrow';
import { asyncHandler } from '../../../../../lib/asyncSafe';

interface OAuthAppClient {
    id: string;
    name: string;
    app_type: 'webapp' | 'mobile' | 'desktop' | 'cli' | 'service';
    idp_id: string;
    client_id: string;
    redirect_uri: string;
    scopes: string[];
    client_type: 'confidential' | 'public' | 'public_with_secret';
    supports_pkce: boolean;
    allowed_grant_types: string[];
    description?: string;
}

/** The subset of an IdP config this form needs to render its picker. */
interface IdPOption {
    id: string;
    name: string;
}

/** Wire shape POSTed/PUT to the control plane for an OAuth app client. */
export interface OAuthAppClientPayload {
    name: string;
    app_type: OAuthAppClient['app_type'];
    idp_id: string;
    client_id: string;
    /** Omitted for public clients, and on edit when left blank (keep current). */
    client_secret?: string;
    redirect_uri: string;
    scopes: string[];
    client_type: OAuthAppClient['client_type'];
    supports_pkce: boolean;
    allowed_grant_types: string[];
    description?: string;
}

interface Props {
    client: OAuthAppClient | null;
    idps: IdPOption[];
    onSave: (data: OAuthAppClientPayload) => Promise<void>;
    onCancel: () => void;
}

const OAuthAppClientForm: React.FC<Props> = ({ client, idps, onSave, onCancel }) => {
    const [formData, setFormData] = useState({
        name: '',
        app_type: 'webapp' as 'webapp' | 'mobile' | 'desktop' | 'cli' | 'service',
        idp_id: '',
        client_id: '',
        client_secret: '',
        redirect_uri: '',
        scopes: 'openid, profile, email',
        client_type: 'confidential' as 'confidential' | 'public' | 'public_with_secret',
        supports_pkce: true,
        allowed_grant_types: ['authorization_code'],
        description: ''
    });
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (client) {
            setFormData({
                name: client.name,
                app_type: client.app_type,
                idp_id: client.idp_id,
                client_id: client.client_id,
                client_secret: '',
                redirect_uri: client.redirect_uri,
                scopes: client.scopes.join(', '),
                client_type: client.client_type,
                supports_pkce: client.supports_pkce,
                allowed_grant_types: client.allowed_grant_types,
                description: client.description || ''
            });
        }
    }, [client]);

    const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
        const { name, value, type } = e.target;
        const checked = (e.target as HTMLInputElement).checked;

        setFormData(prev => ({
            ...prev,
            [name]: type === 'checkbox' ? checked : value
        }));

        // Clear error when user starts typing
        if (error) {
            setError('');
        }
    }, [error]);

    const handleSubmit = async () => {
        setError('');

        if (!formData.name || !formData.idp_id || !formData.client_id || !formData.redirect_uri) {
            setError('Please fill in all required fields');
            return;
        }

        if ((formData.client_type === 'confidential' || formData.client_type === 'public_with_secret') && !formData.client_secret && !client) {
            setError('Client secret is required for confidential and public-with-secret clients');
            return;
        }

        setSaving(true);
        try {
            const hasSecret = formData.client_type === 'confidential' || formData.client_type === 'public_with_secret';
            const payload: OAuthAppClientPayload = {
                name: formData.name,
                app_type: formData.app_type,
                idp_id: formData.idp_id,
                client_id: formData.client_id,
                client_secret: hasSecret && formData.client_secret ? formData.client_secret : undefined,
                redirect_uri: formData.redirect_uri,
                scopes: formData.scopes.split(',').map(s => s.trim()).filter(Boolean),
                client_type: formData.client_type,
                supports_pkce: formData.supports_pkce,
                allowed_grant_types: formData.allowed_grant_types,
                description: formData.description || undefined
            };

            await onSave(payload);
        } catch (err) {
            setError(errorMessage(err, 'Failed to save OAuth app client'));
            setSaving(false);
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
                <div className="flex items-center justify-between px-6 py-4 border-b">
                    <h3 className="text-lg font-semibold text-default">
                        {client ? 'Edit OAuth App Client' : 'Create OAuth App Client'}
                    </h3>
                    <button
                        onClick={onCancel}
                        className="text-muted hover:text-secondary transition-colors"
                        disabled={saving}
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <div className="p-6 space-y-4">
                    {error && (
                        <div className="p-3 bg-red-50 border border-red-200 rounded-md">
                            <div className="flex items-center space-x-2">
                                <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0" />
                                <span className="text-sm text-red-700">{error}</span>
                            </div>
                        </div>
                    )}

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Client Name *</label>
                        <input
                            type="text"
                            name="name"
                            value={formData.name}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            placeholder="e.g., Web App Client"
                            disabled={saving}
                        />
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Application Type *</label>
                        <select
                            name="app_type"
                            value={formData.app_type}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            disabled={!!client || saving}
                        >
                            <option value="webapp">Web Application</option>
                            <option value="mobile">Mobile App</option>
                            <option value="desktop">Desktop App</option>
                            <option value="cli">CLI Tool</option>
                            <option value="service">Service/Backend</option>
                        </select>
                        {client && (
                            <p className="text-xs text-muted mt-1">Cannot change app type for existing client</p>
                        )}
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Identity Provider (Auth Server) *</label>
                        <select
                            name="idp_id"
                            value={formData.idp_id}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            disabled={!!client || saving}
                        >
                            <option value="">Select IdP</option>
                            {idps.map((idp) => (
                                <option key={idp.id} value={idp.id}>
                                    {idp.name}
                                </option>
                            ))}
                        </select>
                        {client && (
                            <p className="text-xs text-muted mt-1">Cannot change IdP for existing client</p>
                        )}
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Client Type *</label>
                        <select
                            name="client_type"
                            value={formData.client_type}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            disabled={!!client || saving}
                        >
                            <option value="confidential">Confidential (client secret, no PKCE)</option>
                            <option value="public">Public (PKCE, no client secret)</option>
                            <option value="public_with_secret">Public with Secret (PKCE + client secret)</option>
                        </select>
                        <p className="text-xs text-muted mt-1">
                            {formData.client_type === 'confidential'
                                ? 'Secure server-side apps that can safely store secrets. Uses client secret for token exchange.'
                                : formData.client_type === 'public'
                                    ? 'Browser-based or mobile apps that cannot safely store secrets. Uses PKCE for security.'
                                    : 'Apps requiring both PKCE and a client secret (e.g., HubSpot). Some providers require this combination.'}
                        </p>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Client ID *</label>
                        <input
                            type="text"
                            name="client_id"
                            value={formData.client_id}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            placeholder="From IdP"
                            disabled={saving}
                        />
                        <p className="text-xs text-muted mt-1">Must match the client ID configured in the Identity Provider</p>
                    </div>

                    {(formData.client_type === 'confidential' || formData.client_type === 'public_with_secret') && (
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Client Secret {client ? '(leave empty to keep current)' : '*'}
                            </label>
                            <input
                                type="password"
                                name="client_secret"
                                value={formData.client_secret}
                                onChange={handleChange}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                placeholder={client ? 'Enter new secret to change' : 'From IdP'}
                                disabled={saving}
                            />
                        </div>
                    )}

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Redirect URI *</label>
                        <input
                            type="text"
                            name="redirect_uri"
                            value={formData.redirect_uri}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            placeholder="http://localhost:3000/callback"
                            disabled={saving}
                        />
                        <p className="text-xs text-muted mt-1">
                            Must match the redirect URI configured for the client ID in your Identity Provider
                        </p>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Scopes - the scopes that this client supports</label>
                        <input
                            type="text"
                            name="scopes"
                            value={formData.scopes}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            placeholder="openid"
                            disabled={saving}
                        />
                        <p className="text-xs text-muted mt-1">SPACE-separated list. IMPORTANT - must match scopes configured for the client ID in the Identity Provider. The 'openid' scope is required in most cases for calling the 'openid-connect/userinfo' endpoint</p>
                    </div>

                    <div className="flex items-center">
                        <input
                            type="checkbox"
                            id="supports_pkce"
                            name="supports_pkce"
                            checked={formData.supports_pkce}
                            onChange={handleChange}
                            className="h-4 w-4 text-primary focus:ring-primary border-strong rounded"
                            disabled={saving}
                        />
                        <label htmlFor="supports_pkce" className="ml-2 flex items-center text-sm text-secondary">
                            <Shield className="w-4 h-4 mr-1" />
                            Enable PKCE (Recommended for security)
                        </label>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">Description</label>
                        <textarea
                            name="description"
                            value={formData.description}
                            onChange={handleChange}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            rows={2}
                            placeholder="Optional description"
                            disabled={saving}
                        />
                    </div>
                </div>

                <div className="flex justify-end space-x-3 px-6 py-4 border-t bg-surface-alt">
                    <button
                        onClick={onCancel}
                        disabled={saving}
                        className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={asyncHandler(handleSubmit, 'click')}
                        disabled={saving}
                        className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        {saving ? 'Saving...' : client ? 'Update Client' : 'Create Client'}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default OAuthAppClientForm;