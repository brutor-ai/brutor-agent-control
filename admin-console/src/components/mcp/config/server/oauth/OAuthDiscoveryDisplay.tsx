/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Shield, Download, Info, Key, Globe, CheckCircle, XCircle, AlertTriangle } from 'lucide-react';
import { errorKind, errorMessage as messageOf } from '../../../../../lib/narrow';
import { asyncHandler } from '../../../../../lib/asyncSafe';

interface OAuthDiscoveryInfo {
    auth_scheme: string;
    error?: string;
    error_description?: string;
    resource_metadata?: string;
    realm?: string;
    scope?: string;
    issuer?: string;
    authorization_endpoint?: string;
    token_endpoint?: string;
    well_known_url?: string;
    resource_name?: string;
    authorization_servers?: string[];
    scopes_supported?: string[];
}

interface SuggestedIdPConfig {
    id: string;
    name: string;
    issuer?: string;
    type: string;
    well_known_url?: string;
    authorization_endpoint?: string;
    token_endpoint?: string;
    jwks_uri?: string;
    userinfo_endpoint?: string;
    scopes_supported?: string[];
    supports_pkce?: boolean;
    enabled?: boolean;
    verified?: boolean;
    description?: string;
    oidc_discovered?: boolean;
}

interface ExistingIdPInfo {
    id: string;
    name: string;
    enabled: boolean;
    issuer: string;
}

interface OAuthDiscoveryDisplayProps {
    oauthDiscovery: OAuthDiscoveryInfo;
    suggestedIdPConfig?: SuggestedIdPConfig;
    existingIdP?: ExistingIdPInfo; // Already checked by backend
    onImportIdP?: (idpConfig: SuggestedIdPConfig) => Promise<void>;
    importing?: boolean;
    imported?: boolean;
}

// Helper function to validate IdP endpoints
const validateIdPEndpoints = (idpConfig: SuggestedIdPConfig): {
    isValid: boolean;
    missingEndpoints: string[];
    canBeEnabled: boolean;
} => {
    const missingEndpoints: string[] = [];

    // Check for required OAuth/OIDC endpoints
    if (!idpConfig.authorization_endpoint) {
        missingEndpoints.push('Authorization Endpoint');
    }

    if (!idpConfig.token_endpoint) {
        missingEndpoints.push('Token Endpoint');
    }

    const canBeEnabled = missingEndpoints.length === 0;

    return {
        isValid: canBeEnabled,
        missingEndpoints,
        canBeEnabled
    };
};

const OAuthDiscoveryDisplay: React.FC<OAuthDiscoveryDisplayProps> = ({
                                                                         oauthDiscovery,
                                                                         suggestedIdPConfig,
                                                                         existingIdP, // Comes directly from the backend
                                                                         onImportIdP,
                                                                         importing: externalImporting,
                                                                         imported: externalImported
                                                                     }) => {
    const [showDetails, setShowDetails] = useState(false);
    const [importing, setImporting] = useState(false);
    const [importSuccess, setImportSuccess] = useState(false);
    const [importError, setImportError] = useState<string | null>(null);

    // Use external state if provided, otherwise use internal state
    const isImporting = externalImporting !== undefined ? externalImporting : importing;
    const isImported = externalImported !== undefined ? externalImported : importSuccess;

    const handleImportIdP = async () => {
        if (!suggestedIdPConfig || !onImportIdP) return;

        setImporting(true);
        setImportError(null);

        try {
            // Validate endpoints before importing
            const validation = validateIdPEndpoints(suggestedIdPConfig);

            // Prepare IdP config with enabled state based on validation
            const idpToImport = {
                ...suggestedIdPConfig,
                enabled: validation.canBeEnabled, // Disable if missing required endpoints
            };

            await onImportIdP(idpToImport);
            setImportSuccess(true);
            setImportError(null);
        } catch (error) {
            console.error('Failed to import IdP:', errorKind(error));

            let errorMessage = 'Failed to import Identity Provider';

            if (error instanceof Error || typeof error === 'object') {
                errorMessage = messageOf(error, errorMessage);
            } else if (typeof error === 'string') {
                errorMessage = error;
            }

            if (errorMessage.includes('already exists') || errorMessage.includes('409')) {
                errorMessage = 'This Identity Provider already exists';
                setImportSuccess(true);
            }

            setImportError(errorMessage);
        } finally {
            setImporting(false);
        }
    };

    const hasDiscoveredEndpoints = suggestedIdPConfig && (
        suggestedIdPConfig.authorization_endpoint ||
        suggestedIdPConfig.token_endpoint
    );

    const renderOidcDiscoveryBadge = () => {
        if (!suggestedIdPConfig) return null;

        if (suggestedIdPConfig.oidc_discovered) {
            return (
                <span className="inline-flex items-center px-2 py-1 bg-green-100 text-green-800 text-xs font-medium rounded">
                    <CheckCircle className="w-3 h-3 mr-1" />
                    OIDC Auto-configured
                </span>
            );
        } else if (hasDiscoveredEndpoints) {
            return (
                <span className="inline-flex items-center px-2 py-1 bg-blue-100 text-blue-800 text-xs font-medium rounded">
                    <Info className="w-3 h-3 mr-1" />
                    Partial Discovery
                </span>
            );
        }

        return null;
    };

    // Validation warning component
    const ValidationWarning: React.FC<{
        validation: { isValid: boolean; missingEndpoints: string[]; canBeEnabled: boolean };
    }> = ({ validation }) => {
        if (validation.isValid) return null;

        return (
            <div className="mt-3 p-3 bg-amber-50 border border-amber-200 rounded-md">
                <div className="flex items-start space-x-2">
                    <AlertTriangle className="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-amber-900 mb-1">
                            Incomplete Configuration Detected
                        </p>
                        <p className="text-xs text-amber-800 mb-2">
                            The following required endpoints are missing:
                        </p>
                        <ul className="text-xs text-amber-800 list-disc list-inside space-y-1 mb-2">
                            {validation.missingEndpoints.map((endpoint, idx) => (
                                <li key={idx}>{endpoint}</li>
                            ))}
                        </ul>
                        <p className="text-xs text-amber-900 font-medium">
                            ⚠️ This Identity Provider will be created in a <strong>disabled</strong> state
                            and cannot be enabled until these endpoints are configured manually.
                        </p>
                    </div>
                </div>
            </div>
        );
    };

    return (
        <div className="mt-4 space-y-4">
            {/* Main OAuth Discovery Card */}
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                <div className="flex items-start space-x-3">
                    <Shield className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <div className="flex items-center justify-between mb-2">
                            <h4 className="font-medium text-blue-900">
                                OAuth Authentication Required
                            </h4>
                            <div className="flex items-center space-x-2">
                                {renderOidcDiscoveryBadge()}
                            </div>
                        </div>

                        <div className="space-y-2 text-sm text-blue-800">
                            <div className="flex items-start space-x-2">
                                <Key className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                <div>
                                    <span className="font-medium">Authentication Scheme:</span>{' '}
                                    <code className="bg-blue-100 px-1 py-0.5 rounded text-xs">
                                        {oauthDiscovery.auth_scheme}
                                    </code>
                                </div>
                            </div>

                            {oauthDiscovery.issuer && (
                                <div className="flex items-start space-x-2">
                                    <Globe className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                    <div>
                                        <span className="font-medium">Issuer:</span>{' '}
                                        <code className="bg-blue-100 px-1 py-0.5 rounded text-xs break-all">
                                            {oauthDiscovery.issuer}
                                        </code>
                                    </div>
                                </div>
                            )}

                            {oauthDiscovery.error_description && (
                                <div className="flex items-start space-x-2">
                                    <Info className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                    <div>
                                        <span className="font-medium">Server Message:</span>{' '}
                                        {oauthDiscovery.error_description}
                                    </div>
                                </div>
                            )}
                        </div>

                        {hasDiscoveredEndpoints && (
                            <button
                                onClick={() => setShowDetails(!showDetails)}
                                className="mt-3 text-xs text-blue-600 hover:text-blue-800 font-medium"
                            >
                                {showDetails ? '▼ Hide' : '▶ Show'} Discovered Endpoints
                            </button>
                        )}
                    </div>
                </div>
            </div>

            {/* Detailed Endpoint Information */}
            {showDetails && hasDiscoveredEndpoints && suggestedIdPConfig && (
                <div className="bg-surface-alt border border-default rounded-lg p-4">
                    <div className="flex items-center justify-between mb-3">
                        <h5 className="font-medium text-default">Discovered OAuth Endpoints</h5>
                        {suggestedIdPConfig.oidc_discovered && (
                            <span className="text-xs text-green-600 flex items-center space-x-1">
                                <CheckCircle className="w-3 h-3" />
                                <span>From OIDC Discovery</span>
                            </span>
                        )}
                    </div>
                    <div className="space-y-2 text-sm">
                        {oauthDiscovery.issuer && (
                            <div>
                                <span className="font-medium text-secondary">Issuer:</span>
                                <div className="mt-1 p-2 bg-surface rounded border text-xs font-mono break-all">
                                    {oauthDiscovery.issuer}
                                </div>
                            </div>
                        )}

                        {suggestedIdPConfig.authorization_endpoint && (
                            <div>
                                <span className="font-medium text-secondary">Authorization Endpoint:</span>
                                <div className="mt-1 p-2 bg-surface rounded border text-xs font-mono break-all">
                                    {suggestedIdPConfig.authorization_endpoint}
                                </div>
                            </div>
                        )}

                        {suggestedIdPConfig.token_endpoint && (
                            <div>
                                <span className="font-medium text-secondary">Token Endpoint:</span>
                                <div className="mt-1 p-2 bg-surface rounded border text-xs font-mono break-all">
                                    {suggestedIdPConfig.token_endpoint}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            )}

            {/* Existing IdP Info Card */}
            {existingIdP && (
                <div className="bg-green-50 border border-green-200 rounded-lg p-4">
                    <div className="flex items-start space-x-3">
                        <CheckCircle className="w-5 h-5 text-green-600 mt-0.5 flex-shrink-0" />
                        <div className="flex-1 min-h-0 min-w-0">
                            <h4 className="font-medium text-green-900 mb-2">
                                Identity Provider Already Registered
                            </h4>
                            <p className="text-sm text-green-800 mb-3">
                                An Identity Provider with issuer <code className="bg-green-100 px-1 py-0.5 rounded text-xs">{existingIdP.issuer}</code> already exists in your system.
                            </p>

                            <div className="bg-surface border border-green-200 rounded p-3 mb-3">
                                <div className="text-xs space-y-1">
                                    <div><span className="font-medium">IdP ID:</span> {existingIdP.id}</div>
                                    <div><span className="font-medium">Name:</span> {existingIdP.name}</div>
                                    <div><span className="font-medium">Issuer:</span> <code className="text-xs">{existingIdP.issuer}</code></div>
                                    <div>
                                        <span className="font-medium">Status:</span>{' '}
                                        <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs ${
                                            existingIdP.enabled
                                                ? 'bg-green-100 text-green-800'
                                                : 'bg-surface-raised text-default'
                                        }`}>
                                            {existingIdP.enabled ? 'Enabled' : 'Disabled'}
                                        </span>
                                    </div>
                                </div>
                            </div>

                            <p className="text-xs text-green-700">
                                ✓ You can proceed to register an OAuth client and assign it to this server using the existing IdP
                            </p>
                        </div>
                    </div>
                </div>
            )}

            {/* Import IdP Action Card - Only show if no existing IdP */}
            {!existingIdP && suggestedIdPConfig && onImportIdP && (
                <div className={`border rounded-lg p-4 ${
                    isImported
                        ? 'bg-green-50 border-green-200'
                        : importError
                            ? 'bg-red-50 border-red-200'
                            : 'bg-green-50 border-green-200'
                }`}>
                    <div className="flex items-start space-x-3">
                        {isImported ? (
                            <CheckCircle className="w-5 h-5 text-green-600 mt-0.5 flex-shrink-0" />
                        ) : importError ? (
                            <XCircle className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                        ) : (
                            <Download className="w-5 h-5 text-green-600 mt-0.5 flex-shrink-0" />
                        )}
                        <div className="flex-1 min-h-0 min-w-0">
                            <h4 className={`font-medium mb-2 ${
                                isImported
                                    ? 'text-green-900'
                                    : importError
                                        ? 'text-red-900'
                                        : 'text-green-900'
                            }`}>
                                {isImported
                                    ? 'Identity Provider Created'
                                    : importError
                                        ? 'Import Failed'
                                        : 'Ready to Import Identity Provider'
                                }
                            </h4>

                            {!isImported && !importError && (
                                <div className="text-sm text-green-800 mb-3">
                                    <p className="mb-2">
                                        {suggestedIdPConfig.oidc_discovered
                                            ? '✨ We successfully discovered the complete OIDC configuration for this authorization server.'
                                            : 'We discovered OAuth configuration for this server.'}
                                    </p>
                                    <p>
                                        Click below to create an Identity Provider entry with these settings.
                                    </p>
                                </div>
                            )}

                            {isImported && (
                                <>
                                    {(() => {
                                        const validation = validateIdPEndpoints(suggestedIdPConfig);
                                        return (
                                            <div className="text-sm text-green-800 mb-3">
                                                {validation.canBeEnabled ? (
                                                    <p>
                                                        The Identity Provider has been successfully created and is ready to use.
                                                        You can now register OAuth clients and assign them to this server.
                                                    </p>
                                                ) : (
                                                    <div className="space-y-2">
                                                        <p>
                                                            The Identity Provider has been created but is <strong>disabled</strong> due to
                                                            missing required endpoints.
                                                        </p>
                                                        <p className="text-amber-700 font-medium">
                                                            ⚠️ You must configure the following endpoints before enabling:
                                                        </p>
                                                        <ul className="list-disc list-inside text-amber-700 text-xs space-y-1 ml-2">
                                                            {validation.missingEndpoints.map((endpoint, idx) => (
                                                                <li key={idx}>{endpoint}</li>
                                                            ))}
                                                        </ul>
                                                        <p className="text-xs text-green-700 mt-2">
                                                            Go to the Identity Providers tab to edit and complete the configuration.
                                                        </p>
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })()}
                                </>
                            )}

                            {importError && (
                                <p className="text-sm text-red-800 mb-3">
                                    {importError}
                                </p>
                            )}

                            <div className="bg-surface border border-green-200 rounded p-3 mb-3">
                                <div className="text-xs space-y-1">
                                    <div><span className="font-medium">Name:</span> {suggestedIdPConfig.name}</div>
                                    <div><span className="font-medium">Type:</span> {suggestedIdPConfig.type}</div>
                                    <div><span className="font-medium">Issuer:</span> <code className="text-xs">{suggestedIdPConfig.issuer}</code></div>

                                    {/* Show endpoint status */}
                                    <div className="mt-2 pt-2 border-t border-green-200">
                                        <div className="font-medium mb-1">Endpoint Status:</div>
                                        <div className="space-y-1">
                                            <div className="flex items-center space-x-2">
                                                {suggestedIdPConfig.authorization_endpoint ? (
                                                    <CheckCircle className="w-3 h-3 text-green-600" />
                                                ) : (
                                                    <XCircle className="w-3 h-3 text-red-600" />
                                                )}
                                                <span className={suggestedIdPConfig.authorization_endpoint ? 'text-green-700' : 'text-red-700'}>
                                                    Authorization Endpoint
                                                </span>
                                            </div>
                                            <div className="flex items-center space-x-2">
                                                {suggestedIdPConfig.token_endpoint ? (
                                                    <CheckCircle className="w-3 h-3 text-green-600" />
                                                ) : (
                                                    <XCircle className="w-3 h-3 text-red-600" />
                                                )}
                                                <span className={suggestedIdPConfig.token_endpoint ? 'text-green-700' : 'text-red-700'}>
                                                    Token Endpoint
                                                </span>
                                            </div>
                                        </div>
                                    </div>

                                    {suggestedIdPConfig.oidc_discovered && (
                                        <div className="mt-2 p-2 bg-green-50 border border-green-200 rounded">
                                            <div className="flex items-start space-x-2">
                                                <CheckCircle className="w-3 h-3 text-green-600 mt-0.5 flex-shrink-0" />
                                                <span className="text-green-800">
                                                    <strong>OIDC Discovery Successful:</strong> All endpoints automatically configured
                                                </span>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </div>

                            {/* Show validation warning before import */}
                            {!isImported && !importError && (() => {
                                const validation = validateIdPEndpoints(suggestedIdPConfig);
                                return <ValidationWarning validation={validation} />;
                            })()}

                            {!isImported && (
                                <button
                                    onClick={asyncHandler(handleImportIdP, 'click')}
                                    disabled={isImporting}
                                    className={`flex items-center space-x-2 px-4 py-2 rounded-md transition-colors ${
                                        isImporting
                                            ? 'bg-surface-inset text-muted cursor-not-allowed'
                                            : 'bg-green-600 text-white hover:bg-green-700'
                                    }`}
                                >
                                    {isImporting ? (
                                        <>
                                            <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                                            <span>Importing IdP...</span>
                                        </>
                                    ) : (
                                        <>
                                            <Download className="w-4 h-4" />
                                            <span>Import Identity Provider</span>
                                        </>
                                    )}
                                </button>
                            )}

                            {isImported && (
                                <div className="flex items-center space-x-2 text-sm text-green-700">
                                    <CheckCircle className="w-4 h-4" />
                                    <span>Successfully imported</span>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default OAuthDiscoveryDisplay;