/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useState } from 'react';
import {
    X,
    ChevronRight,
    ChevronLeft,
    Download,
    CheckCircle,
    Package,
    Key,
    Shield,
    Eye,
    EyeOff,
    AlertCircle,
    Loader,
    Info,
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import { useSlot } from '../../../app/slots';
import {
    MCPCatalogEntry,
    CatalogImportRequest,
    CatalogImportCredentials,
    CatalogImportResponse
} from '../../../lib/mcpCatalogTypes';
import { refusalMessage, serverRefusal } from '../../../lib/serverRefusal';
import { asyncHandler } from '../../../lib/asyncSafe';
import { errorKind } from '../../../lib/narrow';

interface MCPCatalogImportModalProps {
    isOpen: boolean;
    onClose: () => void;
    entry: MCPCatalogEntry;
    onImported: () => void;
}

const STEPS = ['Review', 'Configure Credentials', 'Confirm & Import'] as const;

const MCPCatalogImportModal: React.FC<MCPCatalogImportModalProps> = ({ isOpen, onClose, entry, onImported }) => {
    const toast = useToast();
    const { gatewayClient } = useGateway();

    // What installs entries of this entry's runtime, where something does
    // (slot `mcp-catalog-import.steps`). The console itself imports an entry
    // as configuration: with nothing contributed that is all the wizard
    // offers, whatever the runtime, and it asks the server nothing first.
    const installer = useSlot('mcp-catalog-import.steps').find((item) => item.runtime === entry.runtime);

    const [currentStep, setCurrentStep] = useState(0);
    const [importing, setImporting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    // Wall-clock elapsed seconds during a containerised import. Drives
    // the stage label ("Pulling Docker image…" → "Starting container…"
    // → "Probing capabilities…") so the user has something to watch
    // while the backend grinds for 30–90s. Reset back to 0 between
    // imports.
    const [importElapsedSecs, setImportElapsedSecs] = useState(0);
    // True when the synchronous import POST exceeded the configured
    // timeout. The backend may still complete in the background, so
    // we render a "may have succeeded — please refresh" CTA rather
    // than a hard failure.
    const [timedOut, setTimedOut] = useState(false);
    // Populated when the backend's post-deploy MCP probe failed.
    // The container is running but the wrapped server isn't speaking
    // JSON-RPC correctly — typical cause is a required CLI flag the
    // env-only credential model can't satisfy. The amber banner below
    // shows the warnings + raw log so the operator can act.
    const [probeWarnings, setProbeWarnings] = useState<string[]>([]);
    const [probeLogExcerpt, setProbeLogExcerpt] = useState<string | null>(null);

    // Tick the elapsed counter while an import is in flight. Cleared
    // on every state change so we don't keep counting after the
    // request settles.
    useEffect(() => {
        if (!importing) {
            setImportElapsedSecs(0);
            return;
        }
        const start = Date.now();
        const interval = window.setInterval(() => {
            setImportElapsedSecs(Math.floor((Date.now() - start) / 1000));
        }, 1000);
        return () => window.clearInterval(interval);
    }, [importing]);

    // Step 1: Review
    const [nameOverride, setNameOverride] = useState('');

    // Step 2: Credentials
    const [clientId, setClientId] = useState('');
    const [clientSecret, setClientSecret] = useState('');
    const [envValues, setEnvValues] = useState<Record<string, string>>({});
    const [headerValues, setHeaderValues] = useState<Record<string, string>>({});
    const [urlVars, setUrlVars] = useState<Record<string, string>>({});
    const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});

    // Detect URL template variables in fixedURL (e.g. https://gitmcp.io/{user}/{repo})
    const fixedURL: string | undefined = entry.remote_config?.fixedURL;
    const urlTemplateVars: string[] = fixedURL
        ? Array.from(fixedURL.matchAll(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g)).map(m => m[1])
        : [];
    const hasUrlVars = urlTemplateVars.length > 0;

    // Determine if credentials step is needed — auth-based, env vars, or URL template vars
    const hasEnvVars = (entry.env_vars || []).length > 0;
    const needsCredentials = entry.auth_pattern !== 'none' || hasEnvVars || hasUrlVars;
    const effectiveSteps = needsCredentials ? STEPS : [STEPS[0], STEPS[2]];
    const totalSteps = effectiveSteps.length;

    const runtimeBadge = (runtime: string) => {
        switch (runtime) {
            case 'remote':
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-blue-100 text-blue-700">Remote</span>;
            case 'containerized':
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-purple-100 text-purple-700">Containerized</span>;
            default:
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-700">{runtime}</span>;
        }
    };

    const authBadge = (auth: string) => {
        switch (auth) {
            case 'oauth':
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-green-100 text-green-700">OAuth</span>;
            case 'api_key':
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-yellow-100 text-yellow-700">API Key</span>;
            case 'header':
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-orange-100 text-orange-700">Header</span>;
            case 'none':
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-600">None</span>;
            default:
                return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-600">{auth}</span>;
        }
    };

    const toggleSecret = (key: string) => {
        setShowSecrets((prev) => ({ ...prev, [key]: !prev[key] }));
    };

    const goNext = () => {
        if (currentStep < totalSteps - 1) {
            setCurrentStep(currentStep + 1);
            setError(null);
        }
    };

    const goBack = () => {
        if (currentStep > 0) {
            setCurrentStep(currentStep - 1);
            setError(null);
        }
    };

    const handleImport = async () => {
        setImporting(true);
        setError(null);
        setTimedOut(false);
        setProbeWarnings([]);
        setProbeLogExcerpt(null);

        try {
            const credentials: CatalogImportCredentials = {};

            if (entry.auth_pattern === 'oauth') {
                if (clientId) credentials.client_id = clientId;
                if (clientSecret) credentials.client_secret = clientSecret;
            } else if (entry.auth_pattern === 'api_key') {
                if (Object.keys(envValues).length > 0) {
                    credentials.env_values = envValues;
                }
            } else if (entry.auth_pattern === 'header') {
                if (Object.keys(headerValues).length > 0) {
                    credentials.header_values = headerValues;
                }
            }

            // Also send env vars for non-auth entries (containerized servers)
            if (entry.auth_pattern === 'none' && Object.keys(envValues).length > 0) {
                credentials.env_values = envValues;
            }

            // URL template variables (e.g. {user}, {repo})
            if (hasUrlVars && Object.keys(urlVars).length > 0) {
                (credentials as any).url_vars = urlVars;
            }

            const importRequest: CatalogImportRequest = {
                enabled: true,
                deploy_container: false,
                ...installer?.request,
            };
            if (nameOverride.trim()) {
                importRequest.name_override = nameOverride.trim();
            }
            if (Object.keys(credentials).length > 0) {
                importRequest.credentials = credentials;
            }

            // An installation is one synchronous call that can run for
            // minutes; the installer says how long to give it. An import
            // as configuration completes in <1s and stays on the default.
            const importTimeout = installer?.timeoutMs;
            const result: CatalogImportResponse = await gatewayClient.importMCPCatalogEntry(
                entry.id,
                importRequest as Record<string, unknown>,
                { timeout: importTimeout },
            );

            // Backend's post-deploy probe ran. If it raised warnings,
            // the container is up but the server isn't responding
            // correctly (e.g. wrapped binary missing a CLI flag).
            // Render those in-modal instead of toasting "Successful"
            // and walking away. Operator can read the captured
            // container log excerpt to act on the root cause.
            if (result.deployment_warnings && result.deployment_warnings.length > 0) {
                setProbeWarnings(result.deployment_warnings);
                setProbeLogExcerpt(result.container_log_excerpt ?? null);
                toast.warning?.(
                    'Imported with warnings',
                    result.message || `${entry.name} imported but its MCP server is not responding correctly.`,
                );
                onImported();
            } else {
                toast.success('Import Successful', result.message || `${entry.name} has been imported as an MCP server.`);
                onImported();
            }
        } catch (err) {
            console.error('Import failed:', errorKind(err));
            // Frontend `request()` throws "Request timeout after Nms"
            // when the abort signal fires. The backend may still be
            // pulling the image at that point — treat this case as
            // "indeterminate" rather than a hard failure, refresh the
            // parent's server list so the user can see if it actually
            // landed, and surface a recovery CTA in the modal.
            //
            // A refusal the server gave a reason for (`{detail: {error,
            // message}}`: the edition does not deploy containers, the
            // tenant's policy, no container backend) is not that case,
            // whatever its sentence mentions: nothing was written. Its
            // sentence is shown and the wizard stays open.
            const message: string = refusalMessage(err, 'Failed to import catalog entry.');
            const isTimeout = serverRefusal(err) === null && /timeout/i.test(message);
            if (isTimeout) {
                setTimedOut(true);
                onImported();
            } else {
                setError(message);
            }
        } finally {
            setImporting(false);
        }
    };

    if (!isOpen) return null;

    const currentStepLabel = effectiveSteps[currentStep];

    const renderStepIndicator = () => (
        <div className="flex items-center justify-center space-x-2 mb-6">
            {effectiveSteps.map((step, idx) => (
                <React.Fragment key={step}>
                    <div className="flex items-center space-x-2">
                        <div
                            className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${
                                idx < currentStep
                                    ? 'bg-green-100 text-green-700'
                                    : idx === currentStep
                                        ? 'bg-primary-500 text-white'
                                        : 'bg-surface-alt text-muted'
                            }`}
                        >
                            {idx < currentStep ? (
                                <CheckCircle className="w-5 h-5" />
                            ) : (
                                idx + 1
                            )}
                        </div>
                        <span
                            className={`text-sm hidden sm:inline ${
                                idx === currentStep ? 'text-default font-medium' : 'text-muted'
                            }`}
                        >
                            {step}
                        </span>
                    </div>
                    {idx < effectiveSteps.length - 1 && (
                        <div className="w-8 h-px bg-gray-300" />
                    )}
                </React.Fragment>
            ))}
        </div>
    );

    const renderReviewStep = () => (
        <div className="space-y-4">
            <div className="flex items-start space-x-4 p-4 bg-surface-alt rounded-lg border border-strong">
                <div className="w-12 h-12 rounded bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center flex-shrink-0">
                    <Package className="w-7 h-7 text-white" />
                </div>
                <div className="flex-1 min-h-0 min-w-0">
                    <h3 className="text-base font-semibold text-default">{entry.name}</h3>
                    {entry.short_description && (
                        <p className="text-sm text-secondary mt-1">{entry.short_description}</p>
                    )}
                    <div className="flex items-center gap-2 mt-2">
                        {runtimeBadge(entry.runtime)}
                        {authBadge(entry.auth_pattern)}
                    </div>
                </div>
            </div>

            {entry.tool_preview && entry.tool_preview.length > 0 && (
                <div className="text-sm text-secondary">
                    <span className="font-medium text-default">{entry.tool_preview.length}</span> tool{entry.tool_preview.length !== 1 ? 's' : ''} available:
                    <span className="ml-1">{entry.tool_preview.map(t => t.name).join(', ')}</span>
                </div>
            )}

            <div>
                <label className="block text-sm font-medium text-default mb-1">
                    Server Name Override (optional)
                </label>
                <input
                    type="text"
                    value={nameOverride}
                    onChange={(e) => setNameOverride(e.target.value)}
                    placeholder={entry.name}
                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                />
                <p className="text-xs text-muted mt-1">Leave blank to use the catalog entry name.</p>
            </div>
        </div>
    );

    const renderUrlVarsSection = () => {
        if (!hasUrlVars) return null;

        // Compute preview URL with current values
        let preview = fixedURL || '';
        for (const varName of urlTemplateVars) {
            const val = urlVars[varName] || `{${varName}}`;
            preview = preview.replace(new RegExp(`\\{${varName}\\}`, 'g'), val);
        }

        return (
            <div className="space-y-3 pb-4 mb-4 border-b border-strong">
                <div className="flex items-start space-x-2 p-3 bg-purple-50 border border-purple-200 rounded-md">
                    <Info className="w-4 h-4 text-purple-600 flex-shrink-0 mt-0.5" />
                    <div className="text-sm text-purple-800">
                        <p className="font-medium mb-1">This server URL has template variables.</p>
                        <p className="text-xs font-mono break-all">{fixedURL}</p>
                    </div>
                </div>
                {urlTemplateVars.map((varName) => (
                    <div key={varName}>
                        <label className="block text-sm font-medium text-default mb-1">
                            <code className="text-purple-700 bg-purple-50 px-1 py-0.5 rounded">{`{${varName}}`}</code>
                            <span className="text-red-500 ml-1">*</span>
                        </label>
                        <input
                            type="text"
                            value={urlVars[varName] || ''}
                            onChange={(e) => setUrlVars({ ...urlVars, [varName]: e.target.value })}
                            placeholder={`Enter ${varName}`}
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                        />
                    </div>
                ))}
                {Object.keys(urlVars).length > 0 && (
                    <div className="text-xs text-muted">
                        <span className="font-medium">Resolved URL:</span>{' '}
                        <span className="font-mono text-secondary break-all">{preview}</span>
                    </div>
                )}
            </div>
        );
    };

    const renderCredentialsStep = () => {
        if (entry.auth_pattern === 'oauth') {
            return (
                <div className="space-y-4">
                    <div className="flex items-center space-x-2 p-3 bg-blue-50 border border-blue-200 rounded-md">
                        <Shield className="w-4 h-4 text-blue-600 flex-shrink-0" />
                        <span className="text-sm text-blue-800">This server uses OAuth authentication. Provide your client credentials.</span>
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-default mb-1">Client ID</label>
                        <input
                            type="text"
                            value={clientId}
                            onChange={(e) => setClientId(e.target.value)}
                            placeholder="Enter OAuth client ID"
                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                        />
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-default mb-1">Client Secret</label>
                        <div className="relative">
                            <input
                                type={showSecrets['client_secret'] ? 'text' : 'password'}
                                value={clientSecret}
                                onChange={(e) => setClientSecret(e.target.value)}
                                placeholder="Enter OAuth client secret"
                                className="w-full px-3 py-2 pr-10 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                            />
                            <button
                                type="button"
                                onClick={() => toggleSecret('client_secret')}
                                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-secondary"
                            >
                                {showSecrets['client_secret'] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                            </button>
                        </div>
                    </div>
                </div>
            );
        }

        if (entry.auth_pattern === 'api_key') {
            const requiredEnvVars = (entry.env_vars || []).filter((ev) => ev.required);
            const optionalEnvVars = (entry.env_vars || []).filter((ev) => !ev.required);
            const allEnvVars = [...requiredEnvVars, ...optionalEnvVars];

            return (
                <div className="space-y-4">
                    <div className="flex items-center space-x-2 p-3 bg-yellow-50 border border-yellow-200 rounded-md">
                        <Key className="w-4 h-4 text-yellow-600 flex-shrink-0" />
                        <span className="text-sm text-yellow-800">This server requires API key credentials. Fill in the required values.</span>
                    </div>
                    {allEnvVars.length === 0 && (
                        <p className="text-sm text-muted">No environment variables defined. You may proceed.</p>
                    )}
                    {allEnvVars.map((ev) => (
                        <div key={ev.key}>
                            <label className="block text-sm font-medium text-default mb-1">
                                {ev.name || ev.key}
                                {ev.required && <span className="text-red-500 ml-1">*</span>}
                            </label>
                            {ev.description && (
                                <p className="text-xs text-muted mb-1">{ev.description}</p>
                            )}
                            <div className="relative">
                                <input
                                    type={ev.sensitive && !showSecrets[ev.key] ? 'password' : 'text'}
                                    value={envValues[ev.key] || ''}
                                    onChange={(e) => setEnvValues({ ...envValues, [ev.key]: e.target.value })}
                                    placeholder={`Enter ${ev.name || ev.key}`}
                                    className="w-full px-3 py-2 pr-10 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                                {ev.sensitive && (
                                    <button
                                        type="button"
                                        onClick={() => toggleSecret(ev.key)}
                                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-secondary"
                                    >
                                        {showSecrets[ev.key] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            );
        }

        if (entry.auth_pattern === 'header') {
            const headers = entry.remote_config?.headers || [];

            return (
                <div className="space-y-4">
                    <div className="flex items-center space-x-2 p-3 bg-orange-50 border border-orange-200 rounded-md">
                        <Key className="w-4 h-4 text-orange-600 flex-shrink-0" />
                        <span className="text-sm text-orange-800">This server requires header-based authentication. Provide the header values.</span>
                    </div>
                    {headers.length === 0 && (
                        <p className="text-sm text-muted">No headers defined. You may proceed.</p>
                    )}
                    {headers.map((h) => (
                        <div key={h.key}>
                            <label className="block text-sm font-medium text-default mb-1">
                                {h.name || h.key}
                                {h.required && <span className="text-red-500 ml-1">*</span>}
                            </label>
                            {h.description && (
                                <p className="text-xs text-muted mb-1">{h.description}</p>
                            )}
                            <div className="relative">
                                <input
                                    type={h.sensitive && !showSecrets[h.key] ? 'password' : 'text'}
                                    value={headerValues[h.key] || ''}
                                    onChange={(e) => setHeaderValues({ ...headerValues, [h.key]: e.target.value })}
                                    placeholder={`Enter ${h.name || h.key}`}
                                    className="w-full px-3 py-2 pr-10 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                                {h.sensitive && (
                                    <button
                                        type="button"
                                        onClick={() => toggleSecret(h.key)}
                                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-secondary"
                                    >
                                        {showSecrets[h.key] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            );
        }

        // Fallback: no auth but has env vars (containerized servers like Redis, PostgreSQL)
        if (hasEnvVars) {
            const allEnvVars = entry.env_vars || [];
            return (
                <div className="space-y-4">
                    <div className="flex items-center space-x-2 p-3 bg-blue-50 border border-blue-200 rounded-md">
                        <Key className="w-4 h-4 text-blue-600 flex-shrink-0" />
                        <span className="text-sm text-blue-800">This server requires configuration. Fill in the environment variables below.</span>
                    </div>
                    {allEnvVars.map((ev) => (
                        <div key={ev.key}>
                            <label className="block text-sm font-medium text-default mb-1">
                                {ev.name || ev.key}
                                {ev.required && <span className="text-red-500 ml-1">*</span>}
                            </label>
                            {ev.description && (
                                <p className="text-xs text-muted mb-1">{ev.description}</p>
                            )}
                            <div className="relative">
                                <input
                                    type={ev.sensitive && !showSecrets[ev.key] ? 'password' : 'text'}
                                    value={envValues[ev.key] || ''}
                                    onChange={(e) => setEnvValues({ ...envValues, [ev.key]: e.target.value })}
                                    placeholder={`Enter ${ev.name || ev.key}`}
                                    className="w-full px-3 py-2 pr-10 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                                {ev.sensitive && (
                                    <button
                                        type="button"
                                        onClick={() => toggleSecret(ev.key)}
                                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-secondary"
                                    >
                                        {showSecrets[ev.key] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            );
        }

        return null;
    };

    const renderConfirmStep = () => (
        <div className="space-y-4">
            <div className="p-4 bg-surface-alt rounded-lg border border-strong">
                <h3 className="text-sm font-semibold text-default mb-3">Import Summary</h3>
                <div className="space-y-2 text-sm">
                    <div className="flex justify-between">
                        <span className="text-secondary">Server Name:</span>
                        <span className="text-default font-medium">{nameOverride.trim() || entry.name}</span>
                    </div>
                    <div className="flex justify-between">
                        <span className="text-secondary">Runtime:</span>
                        <span>{runtimeBadge(entry.runtime)}</span>
                    </div>
                    <div className="flex justify-between">
                        <span className="text-secondary">Auth Pattern:</span>
                        <span>{authBadge(entry.auth_pattern)}</span>
                    </div>
                    {entry.tool_preview && entry.tool_preview.length > 0 && (
                        <div className="flex justify-between">
                            <span className="text-secondary">Tools:</span>
                            <span className="text-default">{entry.tool_preview.length}</span>
                        </div>
                    )}
                    {installer && (
                        <div className="flex justify-between">
                            <span className="text-secondary">Deploy Container:</span>
                            <span className="text-default">Yes</span>
                        </div>
                    )}
                </div>
            </div>

            {entry.auth_pattern === 'oauth' && (clientId || clientSecret) && (
                <div className="flex items-center space-x-2 p-3 bg-green-50 border border-green-200 rounded-md">
                    <CheckCircle className="w-4 h-4 text-green-600 flex-shrink-0" />
                    <span className="text-sm text-green-800">OAuth credentials configured</span>
                </div>
            )}

            {entry.auth_pattern === 'api_key' && Object.keys(envValues).some((k) => envValues[k]) && (
                <div className="flex items-center space-x-2 p-3 bg-green-50 border border-green-200 rounded-md">
                    <CheckCircle className="w-4 h-4 text-green-600 flex-shrink-0" />
                    <span className="text-sm text-green-800">API key credentials configured</span>
                </div>
            )}

            {entry.auth_pattern === 'header' && Object.keys(headerValues).some((k) => headerValues[k]) && (
                <div className="flex items-center space-x-2 p-3 bg-green-50 border border-green-200 rounded-md">
                    <CheckCircle className="w-4 h-4 text-green-600 flex-shrink-0" />
                    <span className="text-sm text-green-800">Header credentials configured</span>
                </div>
            )}
        </div>
    );

    const renderCurrentStep = () => {
        switch (currentStepLabel) {
            case 'Review':
                return renderReviewStep();
            case 'Configure Credentials':
                return (
                    <div>
                        {renderUrlVarsSection()}
                        {renderCredentialsStep()}
                    </div>
                );
            case 'Confirm & Import':
                return renderConfirmStep();
            default:
                return null;
        }
    };

    const isLastStep = currentStep === totalSteps - 1;

    const wizard = (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-lg max-h-[85vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-strong">
                    <div className="flex items-center space-x-2">
                        <Download className="w-5 h-5 text-primary-500" />
                        <h2 className="text-lg font-semibold text-default">Import MCP Server</h2>
                    </div>
                    <button
                        onClick={onClose}
                        disabled={importing}
                        className="p-2 hover:bg-surface-alt rounded-md transition-colors disabled:opacity-50"
                    >
                        <X className="w-5 h-5 text-secondary" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region px-6 py-4">
                    {renderStepIndicator()}

                    {/* In-flight stage display — only shown while a
                        containerised import is running. The stages
                        are time-based heuristics, not real backend
                        events, because the import endpoint is one
                        synchronous HTTP call. They give the user
                        something to read instead of staring at a
                        spinner for 30–90s during a first-pull. */}
                    {importing && installer && (
                        <div className="mb-4 p-4 bg-blue-50 border border-blue-200 rounded-md">
                            <div className="flex items-center space-x-2 mb-2">
                                <Loader className="w-4 h-4 animate-spin text-blue-600" />
                                <span className="text-sm font-medium text-blue-900">
                                    {importElapsedSecs < 10
                                        ? 'Validating import request…'
                                        : importElapsedSecs < 60
                                            ? 'Pulling Docker image…'
                                            : importElapsedSecs < 90
                                                ? 'Starting container…'
                                                : 'Probing capabilities…'}
                                </span>
                                <span className="ml-auto text-xs text-blue-700 tabular-nums">
                                    {importElapsedSecs}s
                                </span>
                            </div>
                            <p className="text-xs text-blue-800">
                                First-time imports pull the image from the registry; this can take 1–3 minutes on a fresh host. Subsequent imports of the same image are near-instant.
                            </p>
                        </div>
                    )}

                    {timedOut && (
                        <div className="mb-4 p-4 bg-amber-50 border border-amber-200 rounded-md">
                            <div className="flex items-start space-x-2">
                                <AlertCircle className="w-4 h-4 mt-0.5 text-amber-600 flex-shrink-0" />
                                <div className="flex-1 text-sm min-h-0 min-w-0">
                                    <p className="font-medium text-amber-900">
                                        Import is still running on the server
                                    </p>
                                    <p className="text-amber-800 mt-1">
                                        The request exceeded the UI's wait window, but the deploy may have completed in the background. Check the MCP Servers list in a moment — if {entry.name} shows up there, the import succeeded. Otherwise the Control Plane logs will show the failure reason.
                                    </p>
                                </div>
                            </div>
                        </div>
                    )}

                    {probeWarnings.length > 0 && (
                        <div className="mb-4 p-4 bg-amber-50 border border-amber-200 rounded-md">
                            <div className="flex items-start space-x-2">
                                <AlertCircle className="w-4 h-4 mt-0.5 text-amber-600 flex-shrink-0" />
                                <div className="flex-1 text-sm min-h-0 min-w-0">
                                    <p className="font-medium text-amber-900">
                                        Imported, but the MCP server isn't healthy
                                    </p>
                                    <p className="text-amber-800 mt-1">
                                        The container deployed and the network is reachable, but the post-deploy MCP probe failed. The wrapped server isn't speaking JSON-RPC correctly.
                                    </p>
                                    <ul className="list-disc pl-5 mt-2 space-y-1 text-amber-800">
                                        {probeWarnings.map((w, i) => (
                                            <li key={i}><code className="text-xs">{w}</code></li>
                                        ))}
                                    </ul>
                                    {probeLogExcerpt && (
                                        <details className="mt-2">
                                            <summary className="cursor-pointer text-amber-800 font-medium">View container logs (last 50 lines)</summary>
                                            <pre className="mt-2 p-2 bg-white border border-amber-200 rounded text-xs overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap">{probeLogExcerpt}</pre>
                                        </details>
                                    )}
                                    <p className="text-amber-800 mt-2 text-xs">
                                        The MCP server row was created either way — you can edit its config in <strong>Resources → MCP Servers</strong> after fixing the underlying issue (e.g. correcting credentials or catalog args).
                                    </p>
                                </div>
                            </div>
                        </div>
                    )}

                    {error && (
                        <div className="flex items-center space-x-2 p-3 mb-4 bg-red-50 border border-red-200 rounded-md">
                            <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0" />
                            <span className="text-sm text-red-700">{error}</span>
                        </div>
                    )}

                    {renderCurrentStep()}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between px-6 py-4 border-t border-strong">
                    <button
                        onClick={currentStep === 0 ? onClose : goBack}
                        disabled={importing}
                        className="flex items-center space-x-1 px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        {currentStep === 0 ? (
                            <span>Cancel</span>
                        ) : (
                            <>
                                <ChevronLeft className="w-4 h-4" />
                                <span>Back</span>
                            </>
                        )}
                    </button>

                    {isLastStep ? (
                        <button
                            onClick={asyncHandler(handleImport, 'click')}
                            disabled={importing}
                            className="flex items-center space-x-2 px-4 py-2 text-sm bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {importing ? (
                                <>
                                    <Loader className="w-4 h-4 animate-spin" />
                                    <span>Importing...</span>
                                </>
                            ) : (
                                <>
                                    <Download className="w-4 h-4" />
                                    <span>Import Server</span>
                                </>
                            )}
                        </button>
                    ) : (
                        <button
                            onClick={goNext}
                            className="flex items-center space-x-1 px-4 py-2 text-sm bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors"
                        >
                            <span>Next</span>
                            <ChevronRight className="w-4 h-4" />
                        </button>
                    )}
                </div>
            </div>
        </div>
    );

    // The installer decides whether this installation can install the entry,
    // and says why not in place of the wizard.
    return installer ? (
        <installer.Gate entry={entry} onClose={onClose}>
            {wizard}
        </installer.Gate>
    ) : (
        wizard
    );
};

export default MCPCatalogImportModal;
