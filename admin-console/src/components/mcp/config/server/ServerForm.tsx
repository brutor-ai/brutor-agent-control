/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    X,
    AlertCircle,
    CheckCircle,
    Globe,
    Key,
    Shield,
    Settings,
    Info,
    Wifi,
    WifiOff,
    AlertTriangle,
    ImageIcon,
    Sparkles,
    RefreshCw,
    Sliders,
    Gauge,
    FileClock,
} from 'lucide-react';
import { oauthPairingOf, pairedIdpsInWords, serverIdpPairing } from '../../../../lib/oauthPairing';
import { MCPServerConfig } from '../../../../lib/types.ts';
import { useGateway } from '../../../../hooks/useGateway.ts';
import { useToast } from '../../../common/Toast.tsx';
import ServerIcon from './ServerIcon.tsx';
import OAuthDiscoveryDisplay from './oauth/OAuthDiscoveryDisplay.tsx';
// Tenant-wide per-server Limits/Policies reuse the classic RG MCP forms (DRY).
import MCPLimitsForm from '../../../groups/subtabs/MCPLimitsForm.tsx';
import MCPGovernanceForm from '../../../groups/subtabs/MCPGovernanceForm.tsx';
import AuditInfoTab from '../../../common/AuditInfoTab';
import { asyncHandler } from '../../../../lib/asyncSafe';
import { useSlot, type ImportedServer } from '../../../../app/slots';
import StoredTokenField from '../../../common/StoredTokenField';
import { serverTokenForCreate, serverTokenForUpdate } from '../../../../lib/mcpServerToken';
import { errorKind } from '../../../../lib/narrow';
import { governanceApprovalTimeoutProblem } from '../../../../lib/approvalTimeout';
import { withoutRefused } from '../../../../lib/refusedSettings';
import {
    MAX_CONCURRENT_REQUESTS_DEFAULT,
    MAX_CONCURRENT_REQUESTS_LABEL,
    MAX_CONCURRENT_REQUESTS_PROBLEM,
    maxConcurrentRequestsFromText,
    maxConcurrentRequestsText,
} from '../../../../lib/mcpServerConcurrency';

interface ServerFormProps {
    server: MCPServerConfig | null;
    onSave: (formData: MCPServerConfig, discoverCapabilities?: boolean) => Promise<any>;
    onCancel: () => void;
    onIconChange?: () => Promise<void>;
}

interface FormErrors {
    name?: string;
    base_url?: string;
    mcp_endpoint_path?: string;
    timeout?: string;
    max_concurrent_requests?: string;
}

interface ConnectionTestResult {
    status: 'success' | 'partial_success' | 'error' | 'warning';
    message?: string;
    error_type?: string;
    server_info?: {
        name: string;
        version: string;
        protocol_version?: string;
    };
    capabilities_summary?: {
        tools: number;
        resources: number;
        prompts: number;
        resource_templates: number;
    };
    session_established?: boolean;
    duration_ms: number;
    http_status?: number;
    content_type?: string;
    suggestions?: string[];
    /** Negotiated wire contract from the connection test. */
    protocol_version?: string;
    dialect?: 'handshake' | 'stateless' | 'dual' | string;

    // OAuth discovery fields
    oauth_discovery?: {
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
        discovery_method?: string;          // "www_authenticate" | "body_analysis"
        oauth_docs_url?: string;            // Provider's OAuth documentation URL
    };
    suggested_idp_config?: {
        id: string;
        name: string;
        issuer?: string;                    // Optional - null for skeleton IdPs
        type: string;
        well_known_url?: string;
        authorization_endpoint?: string;
        token_endpoint?: string;
        jwks_uri?: string;
        userinfo_endpoint?: string;
        scopes_supported?: string[];
        supports_pkce?: boolean;
        description?: string;
        oidc_discovered?: boolean;
        skeleton?: boolean;                 // Whether this is a skeleton needing manual setup
        notes?: string;                     // Setup instructions/notes
    };
    existing_idp?: {
        id: string;
        name: string;
        enabled: boolean;
        issuer: string;
    };
    idp_id?: string;                        // IdP ID (existing or suggested)
}

const ServerForm: React.FC<ServerFormProps> = ({
                                                   server,
                                                   onSave,
                                                   onCancel,
                                                   onIconChange
                                               }) => {
    const toast = useToast();

    const [formData, setFormData] = useState<MCPServerConfig>(() => {
        if (server) {
            // Editing mode - use existing server data
            return server;
        }
        // Create mode - provide defaults for all required fields
        return {
            name: '',
            description: '',
            version: '1.0',
            base_url: '',
            mcp_endpoint_path: '',
            auth_type: 'none',
            idp_id: undefined,
            timeout: 30.0,
            enabled: true,
            // MCP Apps governance defaults to true so newly-registered
            // servers can render bundled UIs out of the box. Operators
            // flip it off for compliance-bound tenants.
            mcp_apps_enabled: true,
            // SSE listener affinity defaults to true on create —
            // the value the gateway will write after the first
            // `initialize` round-trip is the authoritative one.
            // Operators rarely need to flip this from the form;
            // it's exposed so they CAN override a server that
            // declares listener capabilities but doesn't honour
            // the GET stream in practice (or vice-versa).
            wants_listener: true,
            // Region defaults to empty
            // (unspecified). Operator sets explicitly if their
            // tenants need region pinning.
            region: '',
            enable_health_check: false,
        } as MCPServerConfig;
    });

    const [errors, setErrors] = useState<FormErrors>({});
    // The static token of a bearer server. The stored token is never
    // returned: the field holds only what is typed now, `removeStoredToken`
    // is the request to remove the stored one, and `tokenRefusal` is what
    // the Control Plane said when a save that named a token was refused.
    const [typedToken, setTypedToken] = useState('');
    const [removeStoredToken, setRemoveStoredToken] = useState(false);
    const [tokenRefusal, setTokenRefusal] = useState<string | null>(null);
    // What the Control Plane said when the last save was refused.
    const [saveRefusal, setSaveRefusal] = useState<string | null>(null);
    // `max_concurrent_requests` as typed: empty is the default, anything
    // else must be a positive whole number before the save sends it.
    const [maxConcurrentText, setMaxConcurrentText] = useState(() => maxConcurrentRequestsText(server?.max_concurrent_requests));
    const maxConcurrent = maxConcurrentRequestsFromText(maxConcurrentText);
    const hasStoredToken = !!server?.has_auth_token;
    const [isSubmitting, setIsSubmitting] = useState(false);
    // Left-nav tab (mirrors the LLM model form's layout).
    const [activeTab, setActiveTab] = useState<'general' | 'audit' | 'advanced' | 'limits' | 'policies'>('general');
    const [discoverCapabilities, setDiscoverCapabilities] = useState(true);
    // Import sources come from the `mcp-server-form.import` slot; this is
    // the id of the one whose dialog is open.
    const importSources = useSlot('mcp-server-form.import');
    const [openImport, setOpenImport] = useState<string | null>(null);

    // Connection test state
    const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
    const [isTestingConnection, setIsTestingConnection] = useState(false);
    const [connectionTestPassed, setConnectionTestPassed] = useState(false);
    const [importingIdP, setImportingIdP] = useState(false);
    const [idpImported, setIdpImported] = useState(false);

    // Track if auth_type was auto-detected
    const [authTypeAutoDetected, setAuthTypeAutoDetected] = useState(false);

    // IdP information state
    const [linkedIdPInfo, setLinkedIdPInfo] = useState<{
        id: string;
        name: string;
        issuer?: string;
        enabled: boolean;
    } | null>(null);
    const [loadingIdP] = useState(false);

    const {
        gatewayClient,
        createIdPConfig,
    } = useGateway();

    // What the Control Plane states of the server as it is stored
    // (`oauth_pairing`). It is about the stored server: it stops applying
    // once the identity provider or the authentication chosen here differ
    // from what is stored, and the form says what it knows itself.
    const storedPairing =
        server && formData.auth_type === 'oauth' && server.auth_type === 'oauth' && (formData.idp_id ?? null) === (server.idp_id ?? null)
            ? oauthPairingOf(server)
            : null;

    // Helper function to safely render test result messages
    const renderTestResultMessage = (message: string | undefined) => {
        if (!message) {
            return <span className="text-muted">No additional details available</span>;
        }

        return message.split('\n').map((line, index) => (
            <React.Fragment key={index}>
                {line}
                {index < message.split('\n').length - 1 && <br/>}
            </React.Fragment>
        ));
    };

    // Reset connection test when URL changes
    useEffect(() => {
        setConnectionTestPassed(false);
        setTestResult(null);
        setAuthTypeAutoDetected(false); // Reset auto-detection flag
    }, [formData.base_url, formData.mcp_endpoint_path]);

    // Auto-set auth_type to 'oauth' when OAuth is discovered
    useEffect(() => {
        if (testResult?.oauth_discovery && testResult.oauth_discovery.auth_scheme) {
            // Only auto-set if:
            // 1. Not editing existing server
            // 2. Current auth_type is 'none' (user hasn't manually changed it)
            if (!server && formData.auth_type === 'none') {
                setFormData(prev => ({ ...prev, auth_type: 'oauth' }));
                setAuthTypeAutoDetected(true);
            }
        }
    }, [testResult, server, formData.auth_type]);

    // Reset form when server prop changes
    useEffect(() => {
        setMaxConcurrentText(maxConcurrentRequestsText(server?.max_concurrent_requests));
        setSaveRefusal(null);
        if (server) {
            setFormData(server);
            setDiscoverCapabilities(false);
            setConnectionTestPassed(true);

            // Set IdP info from server data if available
            if (server.idp_id && server.idp_name && server.auth_type === 'oauth') {
                setLinkedIdPInfo({
                    id: server.idp_id,
                    name: server.idp_name,
                    issuer: '', // We don't have issuer in the list response, but that's okay
                    enabled: true // Assume enabled if it's returned
                });
            } else if (server.idp_id && server.auth_type === 'oauth') {
                // Has idp_id but no name - show as unknown
                setLinkedIdPInfo({
                    id: server.idp_id,
                    name: 'Unknown IdP',
                    issuer: '',
                    enabled: false
                });
            } else {
                setLinkedIdPInfo(null);
            }
        } else {
            setFormData({
                id: '',
                name: '',
                description: '',
                base_url: '',
                mcp_endpoint_path: '',
                auth_type: 'none',
                timeout: 30.0,
                enabled: true
            });
            setDiscoverCapabilities(true);
            setConnectionTestPassed(false);
            setLinkedIdPInfo(null);
        }
        setErrors({});
        setAuthTypeAutoDetected(false);
        setTypedToken('');
        setRemoveStoredToken(false);
        setTokenRefusal(null);
    }, [server]);

    const handleImportIdP = async (idpConfig: any) => {
        setImportingIdP(true);
        try {
            const isSkeleton = idpConfig.skeleton === true;

            // Import the IdP and get the created object back
            const response = await createIdPConfig({
                name: idpConfig.name,
                issuer: idpConfig.issuer || undefined,
                type: isSkeleton ? 'skeleton' : (idpConfig.type || 'oidc_discoverable'),
                well_known_url: idpConfig.well_known_url,
                authorization_endpoint: idpConfig.authorization_endpoint,
                token_endpoint: idpConfig.token_endpoint,
                jwks_uri: idpConfig.jwks_uri,
                userinfo_endpoint: idpConfig.userinfo_endpoint,
                revocation_endpoint: idpConfig.revocation_endpoint,
                introspection_endpoint: idpConfig.introspection_endpoint,
                scopes_supported: idpConfig.scopes_supported || [],
                response_types_supported: idpConfig.response_types_supported || [],
                grant_types_supported: idpConfig.grant_types_supported || [],
                token_endpoint_auth_methods: idpConfig.token_endpoint_auth_methods || [],
                supports_pkce: idpConfig.supports_pkce ?? true,
                supports_refresh_tokens: idpConfig.refreshToken,
                supports_client_credentials: false,
                enabled: idpConfig.enabled ?? true,
                verified: false,
                description: isSkeleton
                    ? idpConfig.notes || `Skeleton IdP - requires manual endpoint configuration`
                    : (idpConfig.description || `Auto-discovered OAuth provider`)
            });

            // Handle IdPDetailResponse format: {idp: {...}, is_reachable: ..., ...}
            let createdIdP = response?.idp;

            // Fallback: if response doesn't have idp structure, maybe it IS the idp
            if (!createdIdP && response?.id) {
                createdIdP = response;
            }

            // Set the IdP ID in formData so the MCP server is linked to the IdP
            if (createdIdP && createdIdP.id) {
                setFormData(prev => ({
                    ...prev,
                    idp_id: createdIdP.id,
                    auth_type: 'oauth'  // Also ensure auth_type is set to oauth
                }));

                // Also update the linked IdP info display
                setLinkedIdPInfo({
                    id: createdIdP.id,
                    name: createdIdP.name,
                    issuer: createdIdP.issuer,
                    enabled: createdIdP.enabled ?? true
                });
            } else {
                console.error('Created IdP does not have an ID');
                throw new Error(
                    'The IdP was saved but no ID came back for it. ' +
                    'The IdP was created but cannot be linked to this MCP server automatically. ' +
                    'Please link it manually after creating the server, or check the Control Plane logs.'
                );
            }

            setIdpImported(true);

            if (isSkeleton) {
                toast.warning(
                    'Skeleton Identity Provider Created',
                    `${idpConfig.name} has been created and linked. ` +
                    `You will need to manually configure the OAuth endpoints (authorization, token, etc.) ` +
                    `in the Identity Providers section before authentication will work.`
                );
            } else {
                toast.success(
                    'Identity Provider Imported',
                    `${idpConfig.name} has been successfully imported and linked to this server.`
                );
            }

        } catch (error) {
            console.error('Failed to import IdP:', errorKind(error));

            const errorMessage = error instanceof Error
                ? error.message
                : 'Unknown error occurred';

            toast.error(
                'Failed to Import Identity Provider',
                `${errorMessage}. You can manually create the IdP in the Identity Providers section.`
            );
        } finally {
            setImportingIdP(false);
        }
    };

    const testConnection = async () => {
        if (!formData.base_url?.trim()) {
            toast.warning('Base URL Required', 'Please enter a server base URL first');
            setTestResult({
                status: 'error',
                message: 'Please enter a server base URL first',
                duration_ms: 0
            });
            return;
        }

        setIsTestingConnection(true);
        setConnectionTestPassed(false);
        setTestResult(null);

        try {
            const completeMcpUrl = `${formData.base_url.replace(/\/$/, '')}${formData.mcp_endpoint_path || ''}`;

            const result = await gatewayClient.testServerConnection({
                url: completeMcpUrl,
                auth_type: formData.auth_type,
                timeout: 10.0
            });

            setTestResult(result);

            if (result.status === 'success') {
                setConnectionTestPassed(true);
                toast.success('Connection Successful', 'Server is reachable and responding correctly.');
            } else if (result.status === 'partial_success') {
                setConnectionTestPassed(true);
                toast.info('Connection Partial Success', result.message || 'Server is reachable but some features may be limited.');
            } else if (result.status === 'warning') {
                setConnectionTestPassed(true);
                toast.warning('Connection Warning', result.message || 'Server is reachable but there are warnings.');
            } else {
                setConnectionTestPassed(false);
                toast.error('Connection Failed', result.message || 'Unable to connect to the server.');
            }

            // Auto-detect OAuth and set auth_type
            if (result.oauth_discovery && !authTypeAutoDetected) {
                setFormData(prev => ({
                    ...prev,
                    auth_type: 'oauth'
                }));
                setAuthTypeAutoDetected(true);
            }

            // Auto-link existing IdP if found
            const existingIdp = result.existing_idp;
            if (existingIdp) {
                setFormData(prev => ({
                    ...prev,
                    idp_id: existingIdp.id,
                    auth_type: 'oauth'
                }));

                // Also update the linked IdP info display
                setLinkedIdPInfo({
                    id: existingIdp.id,
                    name: existingIdp.name,
                    issuer: existingIdp.issuer,
                    enabled: existingIdp.enabled
                });

                toast.info(
                    'Identity Provider Found',
                    `Automatically linked to existing IdP: ${existingIdp.name}`
                );
            }

        } catch (error) {
            const errorMessage = `Network error: ${error instanceof Error ? error.message : 'Unknown error'}`;
            toast.error('Connection Test Failed', errorMessage);
            setTestResult({
                status: 'error',
                message: `${errorMessage}\n\nCheck your internet connection and server URL.`,
                duration_ms: 0
            });
            setConnectionTestPassed(false);
        } finally {
            setIsTestingConnection(false);
        }
    };

    const handleImportedServer = (serverInfo: ImportedServer) => {
        const cleanName = serverInfo.name.split('/').pop() || serverInfo.name;

        let base_url = '';
        let mcp_endpoint_path = '';

        if (serverInfo.url) {
            try {
                const url = new URL(serverInfo.url);
                base_url = `${url.protocol}//${url.host}`;
                mcp_endpoint_path = url.pathname;
            } catch {
                base_url = serverInfo.url;
                mcp_endpoint_path = '';
            }
        }

        setFormData(prev => ({
            ...prev,
            name: cleanName,
            description: serverInfo.description,
            base_url,
            mcp_endpoint_path,
        }));

        setOpenImport(null);
        setConnectionTestPassed(false);
        setTestResult(null);
    };

    const validateForm = (): boolean => {
        const newErrors: FormErrors = {};

        if (!formData.name.trim()) {
            newErrors.name = 'Server name is required';
        }

        if (!formData.base_url.trim()) {
            newErrors.base_url = 'Server base URL is required';
        } else {
            try {
                new URL(formData.base_url);
            } catch {
                newErrors.base_url = 'Please enter a valid URL';
            }
        }

        if (formData.mcp_endpoint_path && !formData.mcp_endpoint_path.startsWith('/')) {
            newErrors.mcp_endpoint_path = 'MCP endpoint path must start with / or be empty';
        }

        if (formData.timeout < 1 || formData.timeout > 300) {
            newErrors.timeout = 'Timeout must be between 1 and 300 seconds';
        }

        if (maxConcurrent === undefined) {
            newErrors.max_concurrent_requests = MAX_CONCURRENT_REQUESTS_PROBLEM;
        }

        setErrors(newErrors);
        return Object.keys(newErrors).length === 0;
    };

    const isFormValid = () => {
        return formData.name?.trim() &&
            formData.base_url?.trim() &&
            formData.timeout >= 1 &&
            formData.timeout <= 300 &&
            maxConcurrent !== undefined &&
            (!formData.mcp_endpoint_path || formData.mcp_endpoint_path.startsWith('/'));
    };

    // An approval timeout the Control Plane refuses is not sent: Save stays off while there is one.
    const timeoutProblem = governanceApprovalTimeoutProblem(formData.tenant_governance);

    const canSubmit = isFormValid() &&
        timeoutProblem === null &&
        (connectionTestPassed ||
            (testResult?.status === 'warning') ||
            !!server) &&
        !isSubmitting;

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();

        if (!validateForm()) {
            toast.warning('Form Validation Failed', 'Please check the form for errors and try again.');
            return;
        }

        if (!server &&
            !connectionTestPassed &&
            testResult?.status !== 'warning') {
            toast.warning('Connection Test Required', 'Please test the connection before creating the server.');
            setTestResult({
                status: 'error',
                message: 'Please test the connection before creating the server',
                duration_ms: 0
            });
            return;
        }

        // The token is named only as far as this save changes it.
        const { auth_token: _neverHeld, ...withoutToken } = formData;
        void _neverHeld;
        const token = server
            ? serverTokenForUpdate({
                  authType: formData.auth_type,
                  typed: typedToken,
                  removeStored: removeStoredToken,
                  stored: hasStoredToken,
              })
            : serverTokenForCreate({ authType: formData.auth_type, typed: typedToken });
        // A stored tenant-wide limit the Control Plane now refuses is not sent again.
        const withLimits: MCPServerConfig =
            withoutToken.tenant_limits == null
                ? withoutToken
                : { ...withoutToken, tenant_limits: withoutRefused('mcp_limits', withoutToken.tenant_limits) };
        // Empty is sent as null: the Control Plane's default.
        const sendable: MCPServerConfig = { ...withLimits, max_concurrent_requests: maxConcurrent ?? null };
        const toSave: MCPServerConfig = token === undefined ? sendable : { ...sendable, auth_token: token };

        setIsSubmitting(true);
        setTokenRefusal(null);
        setSaveRefusal(null);
        try {
            await onSave(toSave, discoverCapabilities);
            toast.success(
                server ? 'Server Updated' : 'Server Created',
                server ? `${formData.name} has been updated successfully.` : `${formData.name} has been created successfully.`
            );
            // Form will be closed by parent component after successful save
        } catch (error) {
            console.error('Failed to save server:', errorKind(error));
            const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred';
            // A save that named a token: the refusal is also shown at the field.
            if (token) setTokenRefusal(errorMessage);
            setSaveRefusal(errorMessage);
            toast.error(
                server ? 'Failed to Update Server' : 'Failed to Create Server',
                errorMessage
            );
            // Only reset isSubmitting if there was an error
            // This allows the form to remain open so user can fix issues
            setIsSubmitting(false);
        }
    };

    const handleInputChange = (field: keyof MCPServerConfig, value: any) => {
        // Reset auto-detection flag when user manually changes auth_type
        if (field === 'auth_type') {
            setAuthTypeAutoDetected(false);
        }

        setFormData(prev => ({ ...prev, [field]: value }));

        if (errors[field as keyof FormErrors]) {
            setErrors(prev => ({ ...prev, [field]: undefined }));
        }
    };

    const getAuthIcon = (authType: string) => {
        switch (authType) {
            case 'oauth':
                return <Shield className="w-4 h-4 text-green-500" />;
            case 'bearer':
                return <Key className="w-4 h-4 text-yellow-500" />;
            default:
                return <Globe className="w-4 h-4 text-muted" />;
        }
    };

    const getAuthDescription = (authType: string) => {
        switch (authType) {
            case 'oauth':
                return 'OAuth 2.0 authentication with automatic token management';
            case 'bearer':
                return 'Bearer token authentication (API key required)';
            default:
                return 'No authentication required';
        }
    };

    // Compute display icon once for icon info display
    const displayIcon = formData.icon || server?.icon;

    const NavItem: React.FC<{
        id: 'general' | 'audit' | 'advanced' | 'limits' | 'policies';
        label: string;
        icon: React.ReactNode;
    }> = ({ id, label, icon }) => (
        <button
            type="button"
            onClick={() => setActiveTab(id)}
            className={`flex items-center space-x-2.5 w-full px-3 py-2.5 text-sm font-medium rounded-lg transition-colors text-left ${
                activeTab === id
                    ? 'bg-primary-50 text-primary-700 border-l-3 border-primary-500'
                    : 'text-secondary hover:text-default hover:bg-surface-alt'
            }`}
        >
            {icon}
            <span>{label}</span>
        </button>
    );

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-5xl w-full max-h-[90vh] flex flex-col">
                <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="flex flex-col flex-1 min-h-0 overflow-hidden min-w-0">
                    {/* Header */}
                    <div className="flex items-center justify-between px-6 py-4 border-b flex-shrink-0">
                        <div>
                            <h3 className="text-lg font-semibold text-default">
                                {server ? 'Edit Server' : 'Add New Server'}
                            </h3>
                            <p className="text-sm text-secondary mt-1">
                                {server ? 'Update server configuration' : 'Configure a new MCP server connection'}
                            </p>
                        </div>
                        <button
                            type="button"
                            onClick={onCancel}
                            className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                        >
                            <X className="w-5 h-5 text-muted" />
                        </button>
                    </div>

                    {/* Sidebar + content (matches the LLM model form layout) */}
                    <div className="flex-1 flex overflow-hidden min-h-0 min-w-0">
                        <nav className="w-48 flex-shrink-0 border-r border-default bg-surface-alt py-3 px-2 space-y-0.5 overflow-y-auto">
                            <NavItem id="general" label="General" icon={<Settings className="w-4 h-4" />} />
                            <NavItem id="audit" label="Audit Info" icon={<FileClock className="w-4 h-4" />} />
                            <NavItem id="advanced" label="Advanced" icon={<Sliders className="w-4 h-4" />} />
                            <NavItem id="limits" label="Limits" icon={<Gauge className="w-4 h-4" />} />
                            <NavItem id="policies" label="Policies" icon={<Shield className="w-4 h-4" />} />
                        </nav>

                        <div className="scroll-region px-6 py-4 space-y-6">
                        {activeTab === 'general' && (<>
                        {/* Ways to import a server, contributed to the
                            `mcp-server-form.import` slot (the registry search is). */}
                        {!server && importSources.map((source) => (
                            <source.Section key={source.id} onOpen={() => setOpenImport(source.id)} />
                        ))}

                        {/* Basic Information */}
                        <div className="space-y-4">
                            <h4 className="text-md font-medium text-default border-b pb-2">Basic Information</h4>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                {/* Only show Server ID when editing (read-only) */}
                                {server && (
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-2">
                                            Server ID
                                        </label>
                                        <div className="w-full px-3 py-2 bg-surface-alt border border-strong rounded-md text-secondary font-mono text-sm">
                                            {formData.id || 'N/A'}
                                        </div>
                                        <p className="mt-1 text-xs text-muted">
                                            Generated automatically, cannot be changed
                                        </p>
                                    </div>
                                )}

                                {/* Display Name - full width when creating, half width when editing */}
                                <div className={server ? '' : 'md:col-span-2'}>
                                    <label className="block text-sm font-medium text-secondary mb-2">
                                        Display Name *
                                    </label>
                                    <input
                                        type="text"
                                        value={formData.name}
                                        onChange={(e) => handleInputChange('name', e.target.value)}
                                        className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-primary ${
                                            errors.name ? 'border-red-300 focus:ring-red-500' : 'border-strong'
                                        }`}
                                        placeholder="e.g., Todo Management Server"
                                    />
                                    {errors.name && (
                                        <div className="flex items-center mt-1 text-sm text-red-600">
                                            <AlertCircle className="w-4 h-4 mr-1" />
                                            {errors.name}
                                        </div>
                                    )}
                                </div>
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Description
                                </label>
                                <textarea
                                    value={formData.description}
                                    onChange={(e) => handleInputChange('description', e.target.value)}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                    rows={2}
                                    placeholder="Brief description of what this server provides..."
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Server Base URL *
                                </label>
                                <input
                                    type="url"
                                    value={formData.base_url || ''}
                                    onChange={(e) => handleInputChange('base_url', e.target.value)}
                                    className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-primary ${
                                        errors.base_url ? 'border-red-300 focus:ring-red-500' : 'border-strong'
                                    }`}
                                    placeholder="http://localhost:3000"
                                />
                                {errors.base_url && (
                                    <div className="flex items-center mt-1 text-sm text-red-600">
                                        <AlertCircle className="w-4 h-4 mr-1" />
                                        {errors.base_url}
                                    </div>
                                )}
                                <div className="text-xs text-muted mt-1">
                                    Base URL of your server (without path)
                                </div>
                            </div>

                            <div>
                                <label className="flex items-center space-x-2 mb-2">
                                    <input
                                        type="checkbox"
                                        checked={formData.mcp_endpoint_path === ''}
                                        onChange={(e) => handleInputChange('mcp_endpoint_path', e.target.checked ? '' : '/mcp')}
                                        className="rounded border-strong text-primary focus:ring-primary"
                                    />
                                    <span className="text-sm font-medium text-secondary">
                                        MCP endpoint is at root (/)
                                    </span>
                                </label>

                                {formData.mcp_endpoint_path !== '' && (
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-2">
                                            MCP Endpoint Path
                                        </label>
                                        <input
                                            type="text"
                                            value={formData.mcp_endpoint_path || ''}
                                            onChange={(e) => handleInputChange('mcp_endpoint_path', e.target.value)}
                                            className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-primary ${
                                                errors.mcp_endpoint_path ? 'border-red-300 focus:ring-red-500' : 'border-strong'
                                            }`}
                                            placeholder="/mcp or /api/mcp"
                                        />
                                        {errors.mcp_endpoint_path && (
                                            <div className="flex items-center mt-1 text-sm text-red-600">
                                                <AlertCircle className="w-4 h-4 mr-1" />
                                                {errors.mcp_endpoint_path}
                                            </div>
                                        )}
                                        <div className="text-xs text-muted mt-1">
                                            Path to append to base URL (must start with /)
                                        </div>
                                    </div>
                                )}
                            </div>

                            {/* Display computed complete MCP URL */}
                            {formData.base_url && (
                                <div className="bg-surface-alt border border-default rounded-lg p-3">
                                    <div className="text-xs font-medium text-secondary mb-1">Complete MCP URL:</div>
                                    <code className="text-sm text-default font-mono break-all">
                                        {formData.base_url.replace(/\/$/, '')}{formData.mcp_endpoint_path || ''}
                                    </code>
                                </div>
                            )}

                            {/* Health Check Configuration */}
                            <div>
                                <label className="flex items-center space-x-2 mb-2">
                                    <input
                                        type="checkbox"
                                        checked={formData.enable_health_check !== false}
                                        onChange={(e) => handleInputChange('enable_health_check', e.target.checked)}
                                        className="rounded border-strong text-primary focus:ring-primary"
                                    />
                                    <span className="text-sm font-medium text-secondary">
                                        Enable health check (/health endpoint)
                                    </span>
                                </label>
                                <div className="text-xs text-muted ml-6">
                                    Will test server connectivity using /health endpoint
                                </div>
                            </div>

                            {/* Negotiated MCP wire contract (read-only,
                                written by the gateway at capability discovery) */}
                            {server && (server.protocol_version || server.dialect) && (
                                <div className="flex items-center gap-2 text-xs">
                                    <span className="text-muted">Negotiated protocol:</span>
                                    <span
                                        className={`px-2 py-0.5 rounded font-mono ${
                                            server.dialect === 'stateless'
                                                ? 'bg-emerald-100 text-emerald-800'
                                                : server.dialect === 'dual'
                                                    ? 'bg-sky-100 text-sky-800'
                                                    : 'bg-slate-100 text-slate-700'
                                        }`}
                                        title="Recorded when the gateway discovered this server's capabilities. 'stateless' = MCP 2026-07-28 (server/discover); 'handshake' = legacy initialize flow; 'dual' = answers both (Brutor's own system servers)."
                                    >
                                        MCP {server.protocol_version ?? 'unknown'}
                                        {server.dialect ? ` · ${server.dialect}` : ''}
                                    </span>
                                </div>
                            )}

                            {/* Connection Test Button */}
                            {!server && (
                                <div className="flex gap-2">
                                    <button
                                        type="button"
                                        onClick={asyncHandler(testConnection, 'click')}
                                        disabled={isTestingConnection || !formData.base_url?.trim()}
                                        className={`flex-1 px-4 py-2 border rounded-md font-medium text-sm transition-colors min-h-0 min-w-0 ${
                                            isTestingConnection
                                                ? 'bg-gray-400 text-white cursor-wait border-gray-400'
                                                : !formData.base_url?.trim()
                                                    ? 'bg-surface-inset text-muted border-strong cursor-not-allowed'
                                                    : connectionTestPassed
                                                        ? 'bg-green-50 text-green-700 border-green-300 hover:bg-green-100'
                                                        : 'bg-primary text-white border-primary hover:bg-primary-600'
                                        }`}
                                    >
                                        {isTestingConnection ? (
                                            <div className="flex items-center justify-center space-x-2">
                                                <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-primary"></div>
                                                <span>Testing Connection...</span>
                                            </div>
                                        ) : connectionTestPassed ? (
                                            <div className="flex items-center justify-center space-x-2">
                                                <Wifi className="w-4 h-4" />
                                                <span>Connection Successful</span>
                                            </div>
                                        ) : (
                                            <div className="flex items-center justify-center space-x-2">
                                                <WifiOff className="w-4 h-4" />
                                                <span>Test Connection and do OIDC discovery</span>
                                            </div>
                                        )}
                                    </button>
                                </div>
                            )}

                            {/* Enhanced Connection Test Result */}
                            {testResult && (
                                <>
                                    <div className={`p-3 rounded-md border ${
                                        testResult.status === 'success'
                                            ? 'bg-green-50 border-green-200'
                                            : testResult.status === 'partial_success'
                                                ? 'bg-yellow-50 border-yellow-200'
                                                : testResult.status === 'warning'
                                                    ? 'bg-yellow-50 border-yellow-200'
                                                    : 'bg-red-50 border-red-200'
                                    }`}>
                                        <div className="flex items-start space-x-2">
                                            {testResult.status === 'success' && <CheckCircle className="w-5 h-5 text-green-600 mt-0.5" />}
                                            {(testResult.status === 'warning' || testResult.status === 'partial_success') && (
                                                <AlertTriangle className="w-5 h-5 text-yellow-600 mt-0.5" />
                                            )}
                                            {testResult.status === 'error' && <AlertCircle className="w-5 h-5 text-red-600 mt-0.5" />}

                                            <div className="flex-1 min-h-0 min-w-0">
                                                <div className={`font-medium text-sm ${
                                                    testResult.status === 'success'
                                                        ? 'text-green-900'
                                                        : (testResult.status === 'warning' || testResult.status === 'partial_success')
                                                            ? 'text-yellow-900'
                                                            : 'text-red-900'
                                                }`}>
                                                    {testResult.status === 'success' && 'Connection Successful'}
                                                    {testResult.status === 'partial_success' && 'Partial Success'}
                                                    {testResult.status === 'warning' && 'Connection Warning'}
                                                    {testResult.status === 'error' && 'Connection Failed'}
                                                </div>

                                                <div className={`text-sm mt-1 ${
                                                    testResult.status === 'success'
                                                        ? 'text-green-700'
                                                        : (testResult.status === 'warning' || testResult.status === 'partial_success')
                                                            ? 'text-yellow-700'
                                                            : 'text-red-700'
                                                }`}>
                                                    {testResult.status === 'success' && testResult.server_info ? (
                                                        <>
                                                            <span className="font-medium">Server:</span> {testResult.server_info.name || 'Unknown'} v{testResult.server_info.version || 'Unknown'}<br/>
                                                            {testResult.server_info.protocol_version && (
                                                                <>
                                                                    <span className="font-medium">Protocol:</span>{' '}
                                                                    MCP {testResult.server_info.protocol_version}
                                                                    {testResult.dialect && (
                                                                        <span className={`ml-1.5 px-1.5 py-0.5 rounded text-xs font-mono ${
                                                                            testResult.dialect === 'stateless'
                                                                                ? 'bg-emerald-100 text-emerald-800'
                                                                                : 'bg-slate-100 text-slate-700'
                                                                        }`}>
                                                                            {testResult.dialect}
                                                                        </span>
                                                                    )}
                                                                    <br/>
                                                                </>
                                                            )}
                                                            <span className="font-medium">Response time:</span> {Math.round(testResult.duration_ms || 0)}ms
                                                            {testResult.capabilities_summary && (
                                                                <>
                                                                    <br/><span className="font-medium">Capabilities:</span> {testResult.capabilities_summary.tools || 0} tools, {testResult.capabilities_summary.resources || 0} resources, {testResult.capabilities_summary.prompts || 0} prompts
                                                                </>
                                                            )}
                                                        </>
                                                    ) : (
                                                        <>
                                                            {renderTestResultMessage(testResult.message)}
                                                            {testResult.suggestions && testResult.suggestions.length > 0 && (
                                                                <>
                                                                    <br/><br/>
                                                                    <span className="font-medium">Suggestions:</span>
                                                                    <ul className="list-disc list-inside mt-1">
                                                                        {testResult.suggestions.map((suggestion, index) => (
                                                                            <li key={index}>{suggestion}</li>
                                                                        ))}
                                                                    </ul>
                                                                </>
                                                            )}
                                                        </>
                                                    )}
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    {/* OAuth Discovery Display */}
                                    {testResult.oauth_discovery && (
                                        <OAuthDiscoveryDisplay
                                            oauthDiscovery={testResult.oauth_discovery}
                                            suggestedIdPConfig={testResult.suggested_idp_config}
                                            existingIdP={testResult.existing_idp}
                                            onImportIdP={handleImportIdP}
                                            importing={importingIdP}
                                            imported={idpImported}
                                        />
                                    )}
                                </>
                            )}
                        </div>

                        {/* Authentication - ENHANCED with auto-detection */}
                        <div className="space-y-4">
                            <div className="flex items-center justify-between">
                                <h4 className="text-md font-medium text-default border-b pb-2 flex-1 min-h-0 min-w-0">
                                    Authentication
                                </h4>
                                {/* Show auto-detection badge */}
                                {authTypeAutoDetected && (
                                    <div className="flex items-center space-x-1 text-xs text-green-600 mb-2">
                                        <Sparkles className="w-3 h-3" />
                                        <span>Auto-detected</span>
                                    </div>
                                )}
                            </div>

                            {/* Show info banner if OAuth was auto-detected */}
                            {testResult?.oauth_discovery && formData.auth_type === 'oauth' && !server && authTypeAutoDetected && (
                                <div className="p-3 bg-blue-50 border border-blue-200 rounded-lg">
                                    <div className="flex items-start space-x-2">
                                        <Info className="w-4 h-4 text-blue-600 mt-0.5 flex-shrink-0" />
                                        <div className="text-sm text-blue-800">
                                            {testResult.oauth_discovery.discovery_method === 'body_analysis' ? (
                                                <>
                                                    <strong>OAuth Detected:</strong> Server requires OAuth authentication (detected from response).
                                                    Standard OIDC discovery was not available — you'll need to configure the IdP endpoints manually.
                                                    {testResult.oauth_discovery.oauth_docs_url && (
                                                        <>
                                                            {' '}See provider docs:{' '}
                                                            <a
                                                                href={testResult.oauth_discovery.oauth_docs_url}
                                                                target="_blank"
                                                                rel="noopener noreferrer"
                                                                className="underline text-blue-700 hover:text-blue-900"
                                                            >
                                                                {testResult.oauth_discovery.oauth_docs_url}
                                                            </a>
                                                        </>
                                                    )}
                                                </>
                                            ) : (
                                                <>
                                                    <strong>OAuth Detected:</strong> Authentication type automatically set to OAuth based on server response.
                                                    You can change this if needed.
                                                </>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            )}

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Authentication Type
                                </label>
                                <div className="space-y-2">
                                    {(['none', 'bearer', 'oauth'] as const).map((authType) => (
                                        <label
                                            key={authType}
                                            className={`flex items-center space-x-3 p-3 border rounded-lg cursor-pointer transition-colors ${
                                                formData.auth_type === authType
                                                    ? 'border-primary bg-primary-50'
                                                    : 'border-default hover:bg-surface-alt'
                                            }`}
                                        >
                                            <input
                                                type="radio"
                                                name="auth_type"
                                                value={authType}
                                                checked={formData.auth_type === authType}
                                                onChange={(e) => handleInputChange('auth_type', e.target.value)}
                                                className="text-primary focus:ring-primary"
                                            />
                                            <div className="flex items-center space-x-2 flex-1 min-h-0 min-w-0">
                                                {getAuthIcon(authType)}
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <div className="font-medium capitalize flex items-center space-x-2">
                                                        <span>{authType === 'none' ? 'No Authentication' : authType}</span>
                                                        {/* Show badge for OAuth when discovered */}
                                                        {authType === 'oauth' && testResult?.oauth_discovery && (
                                                            <span className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded ${
                                                                testResult.oauth_discovery.discovery_method === 'body_analysis'
                                                                    ? 'bg-amber-100 text-amber-800'
                                                                    : 'bg-green-100 text-green-800'
                                                            }`}>
                                                                <Sparkles className="w-3 h-3 mr-1" />
                                                                {testResult.oauth_discovery.discovery_method === 'body_analysis'
                                                                    ? 'Detected (manual setup needed)'
                                                                    : 'Detected'}
                                                            </span>
                                                        )}
                                                    </div>
                                                    <div className="text-sm text-muted">{getAuthDescription(authType)}</div>
                                                </div>
                                            </div>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            {formData.auth_type === 'bearer' && (
                                <div>
                                    <StoredTokenField
                                        id="server-auth-token"
                                        label="Bearer Token"
                                        value={typedToken}
                                        onChange={setTypedToken}
                                        stored={server ? hasStoredToken : undefined}
                                        removeStored={removeStoredToken}
                                        onRemoveStoredChange={setRemoveStoredToken}
                                        owner="server"
                                        storedTestId="server-token-stored"
                                    />
                                    {tokenRefusal && (
                                        <div className="flex items-start mt-1 text-sm text-red-600" data-testid="server-token-refused">
                                            <AlertCircle className="w-4 h-4 mr-1 mt-0.5 flex-shrink-0" />
                                            <span>{tokenRefusal}</span>
                                        </div>
                                    )}
                                </div>
                            )}
                            {formData.auth_type !== 'bearer' && hasStoredToken && (
                                <p className="text-xs text-amber-700" data-testid="server-token-removed-on-save">
                                    A token is stored for this server. Saving the server without bearer authentication removes it.
                                </p>
                            )}

                            {/* Linked IdP Information - Show when OAuth is selected */}
                            {formData.auth_type === 'oauth' && (
                                <div className="mt-4 p-4 bg-surface-alt border border-default rounded-lg">
                                    <div className="flex items-start space-x-3">
                                        <Shield className="w-5 h-5 text-green-600 mt-0.5 flex-shrink-0" />
                                        <div className="flex-1 min-h-0 min-w-0">
                                            <h5 className="text-sm font-medium text-default mb-2">
                                                Linked Identity Provider
                                            </h5>

                                            {loadingIdP ? (
                                                <div className="flex items-center space-x-2 text-sm text-secondary">
                                                    <RefreshCw className="w-4 h-4 animate-spin" />
                                                    <span>Loading IdP information...</span>
                                                </div>
                                            ) : linkedIdPInfo ? (
                                                <div className="space-y-2">
                                                    <div className="flex items-center justify-between">
                                                        <div>
                                                            <div className="flex items-center space-x-2">
                                                                <span className="text-sm font-medium text-default">
                                                                    {linkedIdPInfo.name}
                                                                </span>
                                                                <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                                                                    linkedIdPInfo.enabled
                                                                        ? 'bg-green-100 text-green-800'
                                                                        : 'bg-surface-raised text-default'
                                                                }`}>
                                                                    {linkedIdPInfo.enabled ? 'Enabled' : 'Disabled'}
                                                                </span>
                                                            </div>
                                                            <div className="text-xs text-secondary mt-1">
                                                                <span className="font-medium">ID:</span> {linkedIdPInfo.id}
                                                            </div>
                                                            {linkedIdPInfo.issuer && (
                                                                <div className="text-xs text-secondary break-all">
                                                                    <span className="font-medium">Issuer:</span> {linkedIdPInfo.issuer}
                                                                </div>
                                                            )}
                                                        </div>
                                                    </div>
                                                    <div className="pt-2 border-t border-default">
                                                        {storedPairing?.warning ? (
                                                            <div className="flex items-start space-x-2 text-xs text-amber-700">
                                                                <AlertTriangle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                                                                <span data-testid="server-idp-pairing" data-state="not_paired">
                                                                    {storedPairing.warning}
                                                                </span>
                                                            </div>
                                                        ) : (
                                                            <div className="flex items-start space-x-2 text-xs text-secondary">
                                                                <CheckCircle className="w-3 h-3 text-green-600 mt-0.5 flex-shrink-0" />
                                                                {storedPairing ? (
                                                                    <span data-testid="server-idp-pairing" data-state="paired">
                                                                        The gateway accepts OAuth sign-ins for this server from{' '}
                                                                        {pairedIdpsInWords(storedPairing.idpIds, (id) =>
                                                                            id === linkedIdPInfo.id ? linkedIdPInfo.name : undefined,
                                                                        )}
                                                                        .
                                                                    </span>
                                                                ) : (
                                                                    <span data-testid="server-idp-pairing" data-state="paired">
                                                                        {serverIdpPairing({ serverIdpId: linkedIdPInfo.id }, () => linkedIdPInfo.name).text}{' '}
                                                                        It also accepts them from the identity provider of an active OAuth
                                                                        client attached to this server in a server configuration.
                                                                    </span>
                                                                )}
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                            ) : formData.idp_id ? (
                                                <div className="flex items-start space-x-2 text-sm text-amber-600">
                                                    <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                                    <div>
                                                        <p className="font-medium">IdP Not Found</p>
                                                        <p className="text-xs mt-1">
                                                            IdP ID: {formData.idp_id}
                                                        </p>
                                                        <p className="text-xs text-secondary mt-1">
                                                            The linked Identity Provider could not be found.
                                                            It may have been deleted or you may not have access to it.
                                                        </p>
                                                        {storedPairing?.warning && (
                                                            <p className="text-xs text-amber-700 mt-1" data-testid="server-idp-pairing" data-state="not_paired">
                                                                {storedPairing.warning}
                                                            </p>
                                                        )}
                                                    </div>
                                                </div>
                                            ) : storedPairing?.paired ? (
                                                <div className="flex items-start space-x-2 text-xs text-secondary">
                                                    <CheckCircle className="w-3 h-3 text-green-600 mt-0.5 flex-shrink-0" />
                                                    <span data-testid="server-idp-pairing" data-state="paired">
                                                        This MCP server names no identity provider of its own. It is paired through an
                                                        active OAuth client of a server configuration: the gateway accepts OAuth
                                                        sign-ins for this server from {pairedIdpsInWords(storedPairing.idpIds, () => undefined)}.
                                                    </span>
                                                </div>
                                            ) : (
                                                <div className="flex items-start space-x-2 text-sm text-amber-600">
                                                    <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                                    <div>
                                                        <p className="font-medium">No Identity Provider Linked</p>
                                                        {/* The Control Plane's own words for a stored server;
                                                            the form's for one it has stated nothing about. */}
                                                        <p className="text-xs text-secondary mt-1" data-testid="server-idp-pairing" data-state="not_paired">
                                                            {storedPairing?.warning ?? (
                                                                <>
                                                                    This MCP server names no identity provider. The gateway accepts OAuth
                                                                    sign-ins for it only from the identity provider of an active OAuth client
                                                                    attached to it in a server configuration, and refuses any other.
                                                                </>
                                                            )}
                                                        </p>
                                                        <p className="text-xs text-secondary mt-1">
                                                            Test the connection to automatically discover and link an Identity Provider,
                                                            or manually select one from the Identity Providers section.
                                                        </p>
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Server Icon */}
                        <div className="space-y-4">
                            <h4 className="text-md font-medium text-default border-b pb-2">Server Icon</h4>

                            {server ? (
                                <div className="flex items-start space-x-4">
                                    <div className="flex-shrink-0">
                                        <ServerIcon
                                            key={displayIcon?.uploaded_at || 'no-icon'}
                                            serverId={formData.id}
                                            serverName={formData.name || 'Server'}
                                            iconInfo={displayIcon}
                                            size="large"
                                            editable={true}
                                            onIconUpdate={asyncHandler(async () => {
                                                if (onIconChange) {
                                                    await onIconChange();
                                                }
                                            }, 'icon update')}
                                            onIconDelete={asyncHandler(async () => {
                                                if (onIconChange) {
                                                    await onIconChange();
                                                }
                                            }, 'icon delete')}
                                        />
                                    </div>

                                    <div className="flex-1 min-h-0 min-w-0">
                                        <div className="bg-primary-50 border border-sky-border rounded-lg p-4">
                                            <div className="flex items-start space-x-3">
                                                <ImageIcon className="w-5 h-5 text-primary mt-0.5" />
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <p className="font-medium text-primary-900 mb-1">Server Icon</p>
                                                    <p className="text-sm text-primary-700 mb-2">
                                                        Click the icon to upload a new image or change the existing one.
                                                    </p>

                                                    {displayIcon ? (
                                                        <div className="text-sm text-primary space-y-1">
                                                            <div>Size: {Math.round((displayIcon.size || 0) / 1024)}KB</div>
                                                            <div>Type: {displayIcon.mime_type}</div>
                                                            {displayIcon.uploaded_at && (
                                                                <div>Uploaded: {new Date(displayIcon.uploaded_at).toLocaleDateString()}</div>
                                                            )}
                                                        </div>
                                                    ) : (
                                                        <div className="text-sm text-primary">
                                                            No icon set. Click above to upload one.
                                                        </div>
                                                    )}

                                                    <div className="mt-2 text-xs text-primary-500">
                                                        <strong>Supported formats:</strong> PNG, JPG, SVG, WebP (max 2MB)<br/>
                                                        <strong>Recommended size:</strong> 128x128 pixels or larger
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            ) : (
                                <div className="flex items-start space-x-4">
                                    <div className="flex-shrink-0">
                                        <ServerIcon
                                            serverId="preview"
                                            serverName={formData.name || 'New Server'}
                                            iconInfo={undefined}
                                            size="large"
                                            editable={false}
                                            onIconUpdate={() => {}}
                                            onIconDelete={() => {}}
                                        />
                                    </div>

                                    <div className="flex-1 min-h-0 min-w-0">
                                        <div className="bg-surface-alt border border-default rounded-lg p-4">
                                            <div className="flex items-start space-x-3">
                                                <ImageIcon className="w-5 h-5 text-muted mt-0.5" />
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <p className="font-medium text-secondary mb-1">Server Icon</p>
                                                    <p className="text-sm text-secondary mb-2">
                                                        Icons can be added after the server is created. Save the server first, then edit it to upload an icon.
                                                    </p>

                                                    <div className="mt-2 p-3 bg-blue-50 border border-blue-200 rounded-md">
                                                        <div className="flex items-start space-x-2">
                                                            <Info className="w-4 h-4 text-blue-600 mt-0.5 flex-shrink-0" />
                                                            <div className="text-xs text-blue-700">
                                                                <strong>Why wait?</strong> The server must exist in the database before an icon can be stored.
                                                                After creating this server, you can immediately edit it to add an icon.
                                                            </div>
                                                        </div>
                                                    </div>

                                                    <div className="mt-3 text-xs text-muted">
                                                        <strong>Supported formats:</strong> PNG, JPG, SVG, WebP (max 2MB)<br/>
                                                        <strong>Recommended size:</strong> 128x128 pixels or larger
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Capability Discovery */}
                        {!server && (
                            <div className="space-y-4">
                                <h4 className="text-md font-medium text-default border-b pb-2">Capability Discovery</h4>

                                <div className="bg-primary-50 border border-sky-border rounded-lg p-4">
                                    <div className="flex items-start space-x-3">
                                        <Settings className="w-5 h-5 text-primary mt-0.5" />
                                        <div className="flex-1 min-h-0 min-w-0">
                                            <label className="flex items-center space-x-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={discoverCapabilities}
                                                    onChange={(e) => setDiscoverCapabilities(e.target.checked)}
                                                    className="rounded border-strong text-primary focus:ring-primary"
                                                />
                                                <span className="font-medium text-primary-900">Automatically discover server capabilities</span>
                                            </label>
                                            <p className="text-sm text-primary-700 mt-1">
                                                When enabled, the gateway will connect to the server after creation to discover available tools, resources, and prompts.
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        )}

                        </>)}

                        {activeTab === 'audit' && (
                            <AuditInfoTab
                                meta={{
                                    version: formData.version ?? '1.0',
                                    created_by: server?.created_by,
                                    created_at: server?.created_at,
                                    updated_by: server?.updated_by,
                                    updated_at: server?.updated_at,
                                }}
                                onVersionChange={(v) => handleInputChange('version', v)}
                                isNew={!server}
                            />
                        )}

                        {activeTab === 'advanced' && (
                                <div className="space-y-4 pl-4 border-l-2 border-default">
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Timeout (seconds)
                                            </label>
                                            <input
                                                type="number"
                                                value={formData.timeout}
                                                onChange={(e) => handleInputChange('timeout', parseFloat(e.target.value) || 30)}
                                                className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-primary ${
                                                    errors.timeout ? 'border-red-300 focus:ring-red-500' : 'border-strong'
                                                }`}
                                                min="1"
                                                max="300"
                                            />
                                            {errors.timeout && (
                                                <div className="flex items-center mt-1 text-sm text-red-600">
                                                    <AlertCircle className="w-4 h-4 mr-1" />
                                                    {errors.timeout}
                                                </div>
                                            )}
                                        </div>

                                        <div>
                                            <label htmlFor="server-max-concurrent-requests" className="block text-sm font-medium text-secondary mb-2">
                                                {MAX_CONCURRENT_REQUESTS_LABEL}
                                            </label>
                                            <input
                                                id="server-max-concurrent-requests"
                                                type="text"
                                                inputMode="numeric"
                                                value={maxConcurrentText}
                                                placeholder={String(MAX_CONCURRENT_REQUESTS_DEFAULT)}
                                                onChange={(e) => {
                                                    setMaxConcurrentText(e.target.value);
                                                    setErrors(prev => ({ ...prev, max_concurrent_requests: undefined }));
                                                }}
                                                aria-invalid={maxConcurrent === undefined}
                                                className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-primary ${
                                                    maxConcurrent === undefined ? 'border-red-300 focus:ring-red-500' : 'border-strong'
                                                }`}
                                            />
                                            <p className="text-xs text-muted mt-1" data-testid="server-max-concurrent-requests-help">
                                                Requests to this server in flight at once on each gateway instance; every MCP request counts. Empty: {MAX_CONCURRENT_REQUESTS_DEFAULT}. A ceiling across instances is a group's Max Concurrency (per group).
                                            </p>
                                            {maxConcurrent === undefined && (
                                                <div className="flex items-center mt-1 text-sm text-red-600" data-testid="server-max-concurrent-requests-error">
                                                    <AlertCircle className="w-4 h-4 mr-1" />
                                                    {MAX_CONCURRENT_REQUESTS_PROBLEM}
                                                </div>
                                            )}
                                        </div>

                                        <div className="flex items-center">
                                            <label className="flex items-center space-x-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={formData.enabled}
                                                    onChange={(e) => handleInputChange('enabled', e.target.checked)}
                                                    className="rounded border-strong text-primary focus:ring-primary"
                                                />
                                                <span className="text-sm font-medium text-secondary">Enable server immediately</span>
                                            </label>
                                        </div>

                                        {/*
                                         * MCP Apps governance toggle. Iframe-rendered tool
                                         * bundles (https://modelcontextprotocol.io/extensions/apps)
                                         * are useful for visual results — maps, charts, QR
                                         * codes — but some compliance-bound tenants need to
                                         * forbid them. When unchecked, the gateway suppresses
                                         * `_meta.ui.resourceUri` and refuses the
                                         * `/v1/portal/mcp-apps/.../resource` proxy for this
                                         * server, so the user portal never even attempts to
                                         * mount an iframe.
                                         */}
                                        <div className="flex items-start col-span-2">
                                            <label className="flex items-start space-x-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={formData.mcp_apps_enabled !== false}
                                                    onChange={(e) => handleInputChange('mcp_apps_enabled', e.target.checked)}
                                                    className="mt-0.5 rounded border-strong text-primary focus:ring-primary"
                                                />
                                                <span className="text-sm font-medium text-secondary">
                                                    Allow MCP Apps (iframe-rendered tool UIs)
                                                    <span className="block text-xs font-normal text-muted">
                                                        When off, the user portal will not render
                                                        <code className="mx-1 px-1 rounded bg-surface-alt">_meta.ui.resourceUri</code>
                                                        bundles from this server. Use for compliance-bound tenants.
                                                    </span>
                                                </span>
                                            </label>
                                        </div>

                                        {/*
                                         * SSE listener affinity. Auto-derived during
                                         * capability discovery from the server's
                                         * `initialize` response — true iff it declared
                                         * any push-flavoured capability (`listChanged`,
                                         * `subscribe`, `logging`). Exposed here as a
                                         * manual override for the rare case where the
                                         * upstream lies about its capabilities and the
                                         * operator wants to force the right answer
                                         * (e.g. force-off to stop a reconnect loop
                                         * BEFORE the gateway gets to probe, or
                                         * force-on if discovery happened mid-deploy
                                         * before the server was fully ready). Below
                                         * the apps toggle because it's even more
                                         * obscure than that one — almost no operator
                                         * needs to touch it.
                                         */}
                                        <div className="flex items-start col-span-2">
                                            <label className="flex items-start space-x-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={formData.wants_listener !== false}
                                                    onChange={(e) => handleInputChange('wants_listener', e.target.checked)}
                                                    className="mt-0.5 rounded border-strong text-primary focus:ring-primary"
                                                />
                                                <span className="text-sm font-medium text-secondary">
                                                    Open SSE listener stream
                                                    <span className="block text-xs font-normal text-muted mt-0.5">
                                                        MCP's <em>optional</em> server-to-client event channel — a
                                                        long-lived <code className="mx-1 px-1 rounded bg-surface-alt">GET text/event-stream</code>
                                                        the portal opens to receive notifications such as
                                                        <code className="mx-1 px-1 rounded bg-surface-alt">tools/list_changed</code>,
                                                        resource subscriptions, and server-emitted log messages.
                                                    </span>
                                                    <span className="block text-xs font-normal text-muted mt-1">
                                                        <strong className="text-secondary">Leave on by default.</strong> The gateway
                                                        auto-detects this from each server's <code className="mx-1 px-1 rounded bg-surface-alt">initialize</code>
                                                        response capabilities (<code className="mr-1 px-1 rounded bg-surface-alt">listChanged</code>
                                                        / <code className="mx-1 px-1 rounded bg-surface-alt">subscribe</code>
                                                        / <code className="mx-1 px-1 rounded bg-surface-alt">logging</code>). When the upstream
                                                        is pure request/response (returns HTML or 405 on
                                                        listener GETs — e.g. AWS Knowledge MCP), the gateway
                                                        flips this off automatically, the portal stops
                                                        opening the stream, and the SSE reconnect loop
                                                        that would otherwise inflate proxy logs disappears.
                                                    </span>
                                                    <span className="block text-xs font-normal text-muted mt-1">
                                                        <strong className="text-secondary">When to override:</strong> turn it
                                                        <strong> off</strong> to pre-empt the loop on a server whose <code className="mx-1 px-1 rounded bg-surface-alt">initialize</code>
                                                        falsely declares listener capabilities; turn it
                                                        <strong> on</strong> if a server that genuinely supports server-initiated
                                                        events was probed mid-deploy and got mis-classified.
                                                    </span>
                                                </span>
                                            </label>
                                        </div>

                                        {/* Data residency
                                          * region. Read by the Rust runtime's
                                          * residency check and intersected with
                                          * the tenant's data_residency profile.
                                          * Blank = unspecified; the tenant's
                                          * block_global flag decides whether
                                          * unspecified is allowed.
                                          */}
                                        <div className="col-span-2">
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Region (data residency)
                                            </label>
                                            <input
                                                type="text"
                                                value={formData.region ?? ''}
                                                onChange={(e) => handleInputChange('region', e.target.value)}
                                                placeholder="us-east-1"
                                                className="w-full px-3 py-2 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono"
                                            />
                                            <p className="text-xs text-muted mt-1">
                                                Geographic region this MCP server serves traffic from
                                                (e.g. <code className="bg-surface-raised px-1 rounded">us-east-1</code>,{' '}
                                                <code className="bg-surface-raised px-1 rounded">eu-west-1</code>,{' '}
                                                <code className="bg-surface-raised px-1 rounded">global</code>). Lowercase alnum
                                                plus hyphen / underscore. Blank means unspecified — the tenant's
                                                <code className="bg-surface-raised px-1 rounded">block_global</code> flag
                                                then decides whether unspecified is allowed.
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            )}

                        {activeTab === 'limits' && (
                            <div className="pl-4 border-l-2 border-default">
                                <MCPLimitsForm
                                    value={formData.tenant_limits || {}}
                                    onChange={(next) => setFormData({ ...formData, tenant_limits: next })}
                                />
                            </div>
                        )}

                        {activeTab === 'policies' && (
                            <div className="pl-4 border-l-2 border-default">
                                <MCPGovernanceForm
                                    value={formData.tenant_governance || {}}
                                    onChange={(next) => setFormData({ ...formData, tenant_governance: next })}
                                />
                            </div>
                        )}
                        </div>
                    </div>

                    {/* Footer */}
                    <div className="flex items-center justify-between px-6 py-4 border-t bg-surface-alt flex-shrink-0">
                        <div className="text-sm text-muted flex-1 mr-4 min-h-0 min-w-0">
                            <div>* Required fields</div>
                            {saveRefusal && (
                                <div
                                    role="alert"
                                    className="text-red-600 mt-2 flex items-start space-x-2 bg-red-50 border border-red-200 rounded-md p-2"
                                    data-testid="server-save-refusal"
                                >
                                    <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                    <span>{saveRefusal}</span>
                                </div>
                            )}
                            {!server && !connectionTestPassed && (
                                <div className="text-amber-600 mt-2 flex items-start space-x-2 bg-amber-50 border border-amber-200 rounded-md p-2">
                                    <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                    <div>
                                        <div className="font-medium">Connection test required</div>
                                        <div className="text-xs text-amber-700 mt-0.5">
                                            Please scroll up and click "Test Connection" to verify the server is reachable before creating.
                                        </div>
                                    </div>
                                </div>
                            )}
                            {!server && !isFormValid() && connectionTestPassed && (
                                <div className="text-red-600 mt-2 flex items-start space-x-2 bg-red-50 border border-red-200 rounded-md p-2">
                                    <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                    <div>
                                        <div className="font-medium">Please complete all required fields</div>
                                        <div className="text-xs text-red-700 mt-0.5">
                                            Check for validation errors above.
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>
                        <div className="flex space-x-3">
                            <button
                                type="button"
                                onClick={onCancel}
                                className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                                disabled={isSubmitting}
                            >
                                Cancel
                            </button>
                            <button
                                type="submit"
                                className={`px-4 py-2 rounded-md transition-colors flex items-center space-x-2 ${
                                    canSubmit
                                        ? 'bg-primary-500 hover:bg-primary-600 text-white cursor-pointer'
                                        : 'bg-surface-inset text-muted cursor-not-allowed'
                                }`}
                                disabled={!canSubmit}
                                title={timeoutProblem !== null
                                    ? timeoutProblem
                                    : maxConcurrent === undefined
                                    ? MAX_CONCURRENT_REQUESTS_PROBLEM
                                    : !canSubmit && !server && !connectionTestPassed
                                    ? 'Please test the connection first'
                                    : !canSubmit
                                        ? 'Please complete all required fields'
                                        : ''}
                            >
                                {isSubmitting ? (
                                    <>
                                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                                        <span>Creating...</span>
                                    </>
                                ) : (
                                    <>
                                        <CheckCircle className="w-4 h-4" />
                                        <span>{server ? 'Update' : 'Create'} Server</span>
                                    </>
                                )}
                            </button>
                        </div>
                    </div>
                </form>
            </div>

            {/* The dialogs of the import sources. Always mounted. */}
            {importSources.map((source) => (
                <source.Dialog
                    key={source.id}
                    creating={!server}
                    isOpen={openImport === source.id}
                    onClose={() => setOpenImport(null)}
                    onImport={handleImportedServer}
                />
            ))}
        </div>
    );
};

export default ServerForm;