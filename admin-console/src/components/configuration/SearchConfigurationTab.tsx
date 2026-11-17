/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import type { components } from '../../lib/generated/control-plane-api';

import {
    Search,
    Save,
    AlertCircle,
    CheckCircle,
    Loader2,
    Key,
    Globe,
    Shield,
    Trash2,
    Info,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import type { ApiSendableBody } from '../../lib/api/controlPlaneTypes';
import { errorKind, errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import NotInThisRelease from '../common/NotInThisRelease';

/**
 * Aliased to the generated schema instead of re-declared.
 *
 * The hand-written version disagreed with the wire on optionality (it declared
 * `string[] | null` where the server may omit the key entirely), which is the
 * kind of gap a `Promise<any>` boundary hides until a page throws on a
 * response the server is entitled to send.
 */
type SearchConfig = components['schemas']['SearchProviderConfig'];

const PROVIDERS = [
    {
        id: 'brave',
        name: 'Brave Search',
        description: 'Privacy-focused search. 2,000 free queries/month.',
        requiresApiKey: true,
        fields: ['api_key'],
    },
    {
        id: 'google',
        name: 'Google Custom Search',
        description: 'Best coverage. 100 free queries/day. Requires API key + CX ID.',
        requiresApiKey: true,
        fields: ['api_key', 'google_cx_id'],
    },
    {
        id: 'searxng',
        name: 'SearXNG (Self-Hosted)',
        description: 'Open-source meta-search. Unlimited, no API key needed.',
        requiresApiKey: false,
        fields: ['searxng_base_url'],
    },
];

/** The maximum of results the Control Plane stores when none is set. */
const DEFAULT_MAX_RESULTS = 10;

const SearchConfigurationTab: React.FC = () => {
    const { gatewayClient } = useGateway();

    const [config, setConfig] = useState<SearchConfig>({
        provider: 'brave',
        api_key_configured: false,
        google_cx_id: null,
        searxng_base_url: null,
        max_results_per_query: DEFAULT_MAX_RESULTS,
        safe_search: true,
        allowed_domains: [],
        blocked_domains: [],
        daily_query_limit: null,
        enabled: true,
    });

    const [apiKey, setApiKey] = useState('');
    const [allowedDomainsText, setAllowedDomainsText] = useState('');
    const [blockedDomainsText, setBlockedDomainsText] = useState('');

    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [success, setSuccess] = useState('');
    const [error, setError] = useState('');

    const loadConfig = useCallback(async () => {
        setLoading(true);
        try {
            const data = await gatewayClient.getSearchProviderConfig();
            setConfig(data);
            setAllowedDomainsText((data.allowed_domains || []).join('\n'));
            setBlockedDomainsText((data.blocked_domains || []).join('\n'));
        } catch (err) {
            console.error('Failed to load search config:', errorKind(err));
            // The form below still renders with its built-in defaults, and Save
            // would write them over the real config: this must not be silent.
            setError(errorMessage(err, 'Failed to load search configuration'));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    useEffect(() => {
        runBackground(loadConfig(), 'config');
    }, [loadConfig]);

    const handleSave = async () => {
        setSaving(true);
        setError('');
        setSuccess('');

        try {
            const payload: ApiSendableBody<'/v1/admin/system-servers/search-config', 'put'> = {
                provider: config.provider,
                google_cx_id: config.google_cx_id || null,
                searxng_base_url: config.searxng_base_url || null,
                safe_search: config.safe_search,
                allowed_domains: allowedDomainsText
                    .split('\n')
                    .map(d => d.trim())
                    .filter(Boolean),
                blocked_domains: blockedDomainsText
                    .split('\n')
                    .map(d => d.trim())
                    .filter(Boolean),
                enabled: config.enabled,
            };

            if (apiKey.trim()) {
                payload.api_key = apiKey.trim();
            }

            await gatewayClient.updateSearchProviderConfig(payload);
            setApiKey('');
            setSuccess('Search configuration saved successfully');
            setTimeout(() => setSuccess(''), 4000);
            await loadConfig();
        } catch (err) {
            setError(errorMessage(err, 'Failed to save configuration'));
        } finally {
            setSaving(false);
        }
    };

    const handleClearApiKey = async () => {
        try {
            await gatewayClient.clearSearchApiKey();
            setSuccess('API key cleared');
            setTimeout(() => setSuccess(''), 3000);
            await loadConfig();
        } catch (err) {
            setError(errorMessage(err, 'Failed to clear API key'));
        }
    };

    const selectedProvider = PROVIDERS.find(p => p.id === config.provider);

    if (loading) {
        return (
            <div className="flex items-center justify-center py-12">
                <Loader2 className="w-6 h-6 animate-spin text-primary" />
                <span className="ml-2 text-secondary">Loading search configuration...</span>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            {/* Header */}
            <div>
                <div className="flex items-center space-x-3 mb-2">
                    <Search className="w-6 h-6 text-primary" />
                    <h2 className="text-xl font-bold text-default">Search Provider Configuration</h2>
                </div>
                <p className="text-sm text-secondary">
                    Configure the upstream search provider for the built-in Web Search MCP server.
                    All search queries from agents and portal users are routed through this provider.
                </p>
            </div>

            {/* Success / Error */}
            {success && (
                <div className="bg-green-50 border border-green-200 rounded-lg p-3 flex items-center space-x-2">
                    <CheckCircle className="w-5 h-5 text-green-600" />
                    <p className="text-sm text-green-800">{success}</p>
                </div>
            )}
            {error && (
                <div className="bg-red-50 border border-red-200 rounded-lg p-3 flex items-center space-x-2">
                    <AlertCircle className="w-5 h-5 text-red-600" />
                    <p className="text-sm text-red-800">{error}</p>
                </div>
            )}

            {/* Enabled toggle */}
            <div className="bg-surface rounded-lg border p-5">
                <div className="flex items-center justify-between">
                    <div>
                        <h3 className="text-sm font-semibold text-default">Search Enabled</h3>
                        <p className="text-xs text-muted mt-0.5">Enable or disable web search for this tenant</p>
                    </div>
                    <label className="relative inline-flex items-center cursor-pointer">
                        <input
                            type="checkbox"
                            checked={config.enabled}
                            onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
                            className="sr-only peer"
                        />
                        <div className="w-11 h-6 bg-surface-inset peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-primary/20 rounded-full peer peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-surface after:border-strong after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary" />
                    </label>
                </div>
            </div>

            {/* Provider Selection */}
            <div className="bg-surface rounded-lg border p-5">
                <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                    <Globe className="w-4 h-4" />
                    <span>Search Provider</span>
                </h3>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    {PROVIDERS.map((provider) => (
                        <button
                            key={provider.id}
                            onClick={() => setConfig({ ...config, provider: provider.id })}
                            className={`text-left p-4 rounded-lg border-2 transition-all ${
                                config.provider === provider.id
                                    ? 'border-primary bg-primary/5'
                                    : 'border-default hover:border-strong'
                            }`}
                        >
                            <div className="font-medium text-sm text-default">{provider.name}</div>
                            <div className="text-xs text-muted mt-1">{provider.description}</div>
                        </button>
                    ))}
                </div>
            </div>

            {/* Provider-Specific Settings */}
            <div className="bg-surface rounded-lg border p-5">
                <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                    <Key className="w-4 h-4" />
                    <span>Provider Credentials</span>
                </h3>

                {selectedProvider?.requiresApiKey && (
                    <div className="space-y-4">
                        <div>
                            <label className="block text-xs font-medium text-secondary mb-1">API Key</label>
                            <div className="flex items-center space-x-2">
                                <input
                                    type="password"
                                    value={apiKey}
                                    onChange={(e) => setApiKey(e.target.value)}
                                    placeholder={config.api_key_configured ? '••••••••• (configured)' : 'Enter API key'}
                                    className="flex-1 px-3 py-2 border border-strong rounded-lg text-sm focus:ring-2 focus:ring-primary/50 focus:border-primary min-h-0 min-w-0"
                                />
                                {config.api_key_configured && (
                                    <button
                                        onClick={asyncHandler(handleClearApiKey, 'click')}
                                        className="p-2 text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                                        title="Clear API key"
                                    >
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                )}
                            </div>
                            <p className="text-xs text-muted mt-1">
                                {config.provider === 'brave'
                                    ? 'Get one at brave.com/search/api'
                                    : 'Google Cloud Console API key'
                                }
                            </p>
                        </div>

                        {config.provider === 'google' && (
                            <div>
                                <label className="block text-xs font-medium text-secondary mb-1">
                                    Custom Search Engine ID (CX)
                                </label>
                                <input
                                    type="text"
                                    value={config.google_cx_id || ''}
                                    onChange={(e) => setConfig({ ...config, google_cx_id: e.target.value })}
                                    placeholder="e.g. a1b2c3d4e5f6g7h8i"
                                    className="w-full px-3 py-2 border border-strong rounded-lg text-sm focus:ring-2 focus:ring-primary/50 focus:border-primary"
                                />
                                <p className="text-xs text-muted mt-1">
                                    Set up at programmablesearchengine.google.com
                                </p>
                            </div>
                        )}
                    </div>
                )}

                {config.provider === 'searxng' && (
                    <div>
                        <label className="block text-xs font-medium text-secondary mb-1">SearXNG Instance URL</label>
                        <input
                            type="url"
                            value={config.searxng_base_url || ''}
                            onChange={(e) => setConfig({ ...config, searxng_base_url: e.target.value })}
                            placeholder="e.g. http://searxng:8080"
                            className="w-full px-3 py-2 border border-strong rounded-lg text-sm focus:ring-2 focus:ring-primary/50 focus:border-primary"
                        />
                        <p className="text-xs text-muted mt-1">
                            Your self-hosted SearXNG instance URL
                        </p>
                    </div>
                )}
            </div>

            {/* Governance Settings */}
            <div className="bg-surface rounded-lg border p-5">
                <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                    <Shield className="w-4 h-4" />
                    <span>Governance</span>
                </h3>

                {/* The gateway applies neither a daily query limit nor a
                    maximum of results in this release: not offered, and not
                    sent, so a stored value stays as it is and is shown. */}
                <div className="space-y-2 mb-4">
                    {config.daily_query_limit != null && (
                        <NotInThisRelease
                            testId="search-daily-limit-unavailable"
                            label="Daily Query Limit"
                            value={String(config.daily_query_limit)}
                        />
                    )}
                    {config.max_results_per_query !== DEFAULT_MAX_RESULTS && (
                        <NotInThisRelease
                            testId="search-max-results-unavailable"
                            label="Max Results Per Query"
                            value={String(config.max_results_per_query)}
                        />
                    )}
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="flex items-center space-x-3">
                        <input
                            type="checkbox"
                            id="safeSearch"
                            checked={config.safe_search}
                            onChange={(e) => setConfig({ ...config, safe_search: e.target.checked })}
                            className="h-4 w-4 text-primary border-strong rounded focus:ring-primary"
                        />
                        <label htmlFor="safeSearch" className="text-sm text-secondary">
                            Enforce safe search
                        </label>
                    </div>
                </div>

                {/* Domain Restrictions */}
                <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label className="block text-xs font-medium text-secondary mb-1 flex items-center space-x-1">
                            <span>Allowed Domains</span>
                            <span className="group relative">
                                <Info className="w-3 h-3 text-muted" />
                                <span className="hidden group-hover:block absolute bottom-full left-1/2 -translate-x-1/2 mb-1 w-48 p-2 bg-gray-900 text-white text-xs rounded shadow-lg z-10">
                                    If set, only results from these domains are returned
                                </span>
                            </span>
                        </label>
                        <textarea
                            value={allowedDomainsText}
                            onChange={(e) => setAllowedDomainsText(e.target.value)}
                            placeholder="One domain per line&#10;e.g. github.com&#10;docs.rust-lang.org"
                            rows={4}
                            className="w-full px-3 py-2 border border-strong rounded-lg text-sm font-mono focus:ring-2 focus:ring-primary/50 focus:border-primary"
                        />
                    </div>

                    <div>
                        <label className="block text-xs font-medium text-secondary mb-1 flex items-center space-x-1">
                            <span>Blocked Domains</span>
                            <span className="group relative">
                                <Info className="w-3 h-3 text-muted" />
                                <span className="hidden group-hover:block absolute bottom-full left-1/2 -translate-x-1/2 mb-1 w-48 p-2 bg-gray-900 text-white text-xs rounded shadow-lg z-10">
                                    Results from these domains are excluded (ignored if allowed domains is set)
                                </span>
                            </span>
                        </label>
                        <textarea
                            value={blockedDomainsText}
                            onChange={(e) => setBlockedDomainsText(e.target.value)}
                            placeholder="One domain per line&#10;e.g. pinterest.com&#10;quora.com"
                            rows={4}
                            className="w-full px-3 py-2 border border-strong rounded-lg text-sm font-mono focus:ring-2 focus:ring-primary/50 focus:border-primary"
                        />
                    </div>
                </div>
            </div>

            {/* Save Button */}
            <div className="flex justify-end">
                <button
                    onClick={asyncHandler(handleSave, 'click')}
                    disabled={saving}
                    className="flex items-center space-x-2 px-6 py-2.5 bg-primary text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors"
                >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    <span>{saving ? 'Saving...' : 'Save Configuration'}</span>
                </button>
            </div>
        </div>
    );
};

export default SearchConfigurationTab;
