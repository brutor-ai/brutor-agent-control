/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useRef, useState } from 'react';
import { AlertCircle, ChevronDown, Info, KeyRound, Layers, ShieldOff, X } from 'lucide-react';
import { useGateway } from '../../../../../hooks/useGateway';
import { ServerConfig } from '../../../../../lib/types';
import { GroupHierarchyTree } from '../../../../../lib/groupTypes';
import HierarchicalGroupSelector from '../../../../common/HierarchicalGroupSelector';
import InfoBanner from '../../../../common/InfoBanner';
import AuditInfoTab from '../../../../common/AuditInfoTab';
import FormTabBar from '../../../../common/FormTabBar';
import { errorMessage } from '../../../../../lib/narrow';
import { asyncHandler } from '../../../../../lib/asyncSafe';
import { serverIdpPairing } from '../../../../../lib/oauthPairing';

/**
 * What the form hands to `onSave`. The backend (one of the two server ids)
 * and the OAuth client are the creation's alone: the Control Plane's update
 * of a server config names none of the three.
 */
export interface ServerConfigPayload {
    mcp_server_id?: string;
    virtual_server_id?: string;
    oauth_client_id?: string;
    group_ids: string[];
    name: string;
    description?: string;
    priority: number;
    is_default: boolean;
    enabled: boolean;
    display_name?: string;
    display_description?: string;
    icon_url?: string;
}

interface ServerConfigFormProps {
    config: ServerConfig | null;
    servers: any[];
    // VMCP list for the "Backend type: VMCP" branch.
    // The VMCP option is hidden when there are none.
    virtualServers?: any[];
    oauthClients: any[];
    groupTree: GroupHierarchyTree[];
    onSave: (data: ServerConfigPayload) => Promise<void>;
    onCancel: () => void;
}

const ServerConfigForm: React.FC<ServerConfigFormProps> = ({
                                                               config,
                                                               servers,
                                                               virtualServers = [],
                                                               oauthClients,
                                                               groupTree,
                                                               onSave,
                                                               onCancel
                                                           }) => {
    const { getServerIconUrl: getGatewayIconUrl } = useGateway();

    // `backend_kind` discriminates the form between
    // a real-MCP-backed config and a VMCP-backed config. Locked
    // on edit (operators clone + re-create rather than flipping
    // backends in place).
    const [formData, setFormData] = useState({
        backend_kind: 'mcp' as 'mcp' | 'vmcp',
        mcp_server_id: '',
        virtual_server_id: '',
        oauth_client_id: '',
        group_ids: [] as string[],
        name: '',
        description: '',
        priority: 100,
        is_default: false,
        enabled: true,
        display_name: '',
        display_description: '',
        icon_url: '',
        requires_oauth: false
    });
    const [error, setError] = useState('');

    // A server config has no version of its own — it inherits the one on the
    // MCP server it configures — so this tab is authorship only.
    const [activeTab, setActiveTab] = useState<'general' | 'audit'>('general');
    const [saving, setSaving] = useState(false);
    const [selectedServer, setSelectedServer] = useState<any>(null);
    // Whether the chosen server is paired with an identity provider: by its
    // own setting, or through the OAuth client chosen here.
    const pairedServer = selectedServer as { idp_id?: string | null; idp_name?: string | null } | null;
    const chosenClient =
        (oauthClients as Array<{ id: string; name: string; idp_id?: string | null; is_active?: boolean | null }>).find(
            (client) => client.id === formData.oauth_client_id,
        ) ?? null;
    const pairing = serverIdpPairing({ serverIdpId: pairedServer?.idp_id, client: chosenClient }, (id) =>
        id === pairedServer?.idp_id ? (pairedServer.idp_name ?? undefined) : undefined,
    );

    useEffect(() => {
        if (config) {
            // Determine backend_kind from the loaded
            // config. `server_kind` field is authoritative; fall
            // back to "vmcp" if only virtual_server_id is set.
            const kind: 'mcp' | 'vmcp' =
                config.server_kind === 'vmcp' || !!config.virtual_server_id
                    ? 'vmcp' : 'mcp';
            setFormData({
                backend_kind: kind,
                mcp_server_id: config.mcp_server_id || '',
                virtual_server_id: config.virtual_server_id || '',
                oauth_client_id: config.oauth_client_id || '',
                group_ids: config.group_ids || [],
                name: config.name,
                description: config.description || '',
                priority: config.priority,
                is_default: config.is_default,
                enabled: config.enabled,
                display_name: config.display_name || '',
                display_description: config.display_description || '',
                icon_url: config.icon_url || '',
                requires_oauth: kind === 'mcp' && !!config.oauth_client_id
            });

            // Set selected server for icon preview (MCP branch only;
            // VMCPs render their own sky-blue badge instead).
            if (kind === 'mcp') {
                const server = servers.find(s => s.id === config.mcp_server_id);
                setSelectedServer(server);
            } else {
                setSelectedServer(null);
            }
        }
    }, [config, servers]);

    useEffect(() => {
        // Update selected server when mcp_server_id changes
        if (formData.mcp_server_id) {
            const server = servers.find(s => s.id === formData.mcp_server_id);
            setSelectedServer(server);

            // Check for both 'oauth' and 'oauth2'
            if (server) {
                const requiresOAuth = server.auth_type === 'oauth2' ||
                    server.auth_type === 'oauth' ||
                    (server.auth_type && server.auth_type !== 'none');

                setFormData(prev => ({
                    ...prev,
                    requires_oauth: requiresOAuth,
                    oauth_client_id: requiresOAuth ? prev.oauth_client_id : ''
                }));
            }
        } else {
            setSelectedServer(null);
        }
    }, [formData.mcp_server_id, servers]);

    const handleGroupSelectionChange = (selectedIds: string[]) => {
        setFormData(prev => ({ ...prev, group_ids: selectedIds }));
    };

    const handleSubmit = async () => {
        setError('');

        // XOR backend validation. The Pydantic model
        // also enforces this server-side, but catching it here
        // gives a clearer error.
        if (formData.backend_kind === 'mcp' && !formData.mcp_server_id) {
            setError('Please select an MCP server');
            return;
        }
        if (formData.backend_kind === 'vmcp' && !formData.virtual_server_id) {
            setError('Please select a Virtual MCP server');
            return;
        }
        if (!formData.name) {
            setError('Configuration name is required');
            return;
        }
        if (formData.backend_kind === 'mcp'
            && formData.requires_oauth
            && !formData.oauth_client_id) {
            setError('OAuth client is required for OAuth-protected MCP servers');
            return;
        }

        setSaving(true);
        try {
            // Send only the backend-id matching the chosen kind so
            // the server-side XOR validator passes cleanly.
            const backendIds = formData.backend_kind === 'vmcp'
                ? { virtual_server_id: formData.virtual_server_id }
                : { mcp_server_id: formData.mcp_server_id };

            const payload: ServerConfigPayload = {
                ...backendIds,
                // VMCP-backed configs ignore oauth_client_id (VMCPs
                // delegate per-call auth to the underlying MCPs).
                ...(formData.backend_kind === 'mcp'
                    && formData.requires_oauth
                    && formData.oauth_client_id
                        ? { oauth_client_id: formData.oauth_client_id }
                        : {}),
                group_ids: formData.group_ids,
                name: formData.name,
                description: formData.description || undefined,
                priority: formData.priority,
                is_default: formData.is_default,
                enabled: formData.enabled,
                display_name: formData.display_name || undefined,
                display_description: formData.display_description || undefined,
                icon_url: formData.icon_url || undefined
            };

            await onSave(payload);
        } catch (err) {
            setError(errorMessage(err, 'Failed to save server configuration'));
            setSaving(false);
        }
    };

    // Use gateway URL helper
    const getServerIconUrl = (server: any): string | null => {
        if (!server || !server.icon?.url) return null;

        const timestamp = server.icon.uploaded_at
            ? new Date(server.icon.uploaded_at).getTime()
            : Date.now();
        const cacheKey = `${timestamp}_${Math.random().toString(36).substr(2, 9)}`;
        const baseUrl = getGatewayIconUrl(server.id);

        return `${baseUrl}?t=${cacheKey}`;
    };

    const serverIconUrl = getServerIconUrl(selectedServer);

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
                <div className="flex items-center justify-between px-6 py-4 border-b">
                    <h3 className="text-lg font-semibold text-default">
                        {config ? 'Edit Server Configuration' : 'Create Server Configuration'}
                    </h3>
                    <button onClick={onCancel} className="text-muted hover:text-secondary">
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <FormTabBar
                    tabs={[
                        { id: 'general', label: 'General' },
                        { id: 'audit', label: 'Audit Info' },
                    ]}
                    active={activeTab}
                    onChange={setActiveTab}
                />

                <div className="px-6 py-4">
                    {/* Outside the tab switch: Save sits in the footer and is
                        reachable from either tab, so a save error has to be
                        visible from either tab too. */}
                    {error && (
                        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md">
                            <div className="flex items-start space-x-2">
                                <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                                <span className="text-sm text-red-700">{error}</span>
                            </div>
                        </div>
                    )}

                    {activeTab === 'audit' ? (
                        <AuditInfoTab
                            versionHidden
                            versionDerivedFrom="MCP server"
                            meta={{
                                created_by: config?.created_by,
                                created_at: config?.created_at,
                                updated_by: config?.updated_by,
                                updated_at: config?.updated_at,
                            }}
                            isNew={!config}
                        />
                    ) : (
                    <>

                    <div className="space-y-4">
                        {/* Backend type selector. Locked
                            on edit (operators clone + re-create to
                            change backends, since changing the
                            backend invalidates the proxy URL the
                            portal has cached). */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Backend type *
                            </label>
                            <select
                                value={formData.backend_kind}
                                disabled={!!config}
                                onChange={(e) => {
                                    const nextKind = e.target.value as 'mcp' | 'vmcp';
                                    setFormData(prev => ({
                                        ...prev,
                                        backend_kind: nextKind,
                                        // Reset the OTHER backend's id when switching
                                        ...(nextKind === 'vmcp'
                                            ? { mcp_server_id: '', oauth_client_id: '', requires_oauth: false }
                                            : { virtual_server_id: '' }),
                                    }));
                                    if (nextKind === 'vmcp') setSelectedServer(null);
                                }}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                            >
                                <option value="mcp">MCP Server (real upstream)</option>
                                <option value="vmcp" disabled={virtualServers.length === 0}>
                                    Virtual MCP (VMCP — composed tools across servers)
                                    {virtualServers.length === 0 ? ' — none defined yet' : ''}
                                </option>
                            </select>
                            <p className="mt-1 text-xs text-muted">
                                {formData.backend_kind === 'vmcp'
                                    ? 'VMCP-backed configs delegate per-call auth to the underlying MCPs. No per-config OAuth client is required at this scope.'
                                    : 'Real MCP servers carry their own OAuth context (or run open).'}
                                {!!config && ' Backend type is locked once a config exists — clone + re-create to switch.'}
                            </p>
                        </div>

                        {/* MCP Server Selection — custom listbox so we can
                            render provider icons and the OAuth / none-auth
                            badge inline. Native `<select>` strips markup
                            from `<option>`, which is why the icons went
                            missing the last time this got simplified. */}
                        {formData.backend_kind === 'mcp' && (
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                MCP Server *
                            </label>
                            {/* The server is fixed when the config is created. */}
                            <fieldset disabled={!!config} className="contents">
                                <McpServerPicker
                                    servers={servers}
                                    value={formData.mcp_server_id}
                                    onChange={(id) => setFormData({ ...formData, mcp_server_id: id })}
                                    getIconUrl={getGatewayIconUrl}
                                />
                            </fieldset>
                            {!!config && (
                                <p className="mt-1 text-xs text-muted" data-testid="server-config-fixed-backend">
                                    The server and the OAuth client are set when the config is created. An update does not change them.
                                </p>
                            )}
                        </div>
                        )}

                        {/* VMCP picker. Plain <select>
                            (VMCPs don't have icons today; the
                            sky-blue badge in the list view is enough
                            visual differentiation). */}
                        {formData.backend_kind === 'vmcp' && (
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Virtual MCP Server *
                                </label>
                                <select
                                    value={formData.virtual_server_id}
                                    disabled={!!config}
                                    onChange={(e) => setFormData({ ...formData, virtual_server_id: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                >
                                    <option value="">— select a VMCP —</option>
                                    {virtualServers.map((v: any) => (
                                        <option key={v.id} value={v.id}>
                                            {v.name}
                                            {v.capabilities?.tools?.length
                                                ? ` (${v.capabilities.tools.length} tool${v.capabilities.tools.length === 1 ? '' : 's'})`
                                                : ''}
                                        </option>
                                    ))}
                                </select>
                                {formData.virtual_server_id && (
                                    <InfoBanner
                                        title="VMCP-backed configuration"
                                        icon={<Layers className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />}
                                        className="mt-3 mb-0"
                                    >
                                        <p>
                                            The portal will route tool invocations through{' '}
                                            <code className="px-1 py-0.5 bg-blue-100 rounded text-xs">
                                                /v1/proxy/vmcp/{formData.virtual_server_id}
                                            </code>.
                                            The VMCP applies per-tool rename / subset rules and delegates each
                                            call to the appropriate upstream MCP server.
                                        </p>
                                    </InfoBanner>
                                )}
                            </div>
                        )}

                        {/* Server Icon Preview */}
                        {formData.backend_kind === 'mcp' && selectedServer && serverIconUrl && (
                            <div className="p-4 bg-surface-alt border border-default rounded-md">
                                <div className="flex items-center space-x-3">
                                    <img
                                        src={serverIconUrl}
                                        alt={selectedServer.name}
                                        className="w-12 h-12 rounded object-contain bg-surface-alt"
                                        onError={(e) => {
                                            const target = e.target as HTMLImageElement;
                                            target.style.display = 'none';
                                        }}
                                    />
                                    <div>
                                        <p className="text-sm font-medium text-default">{selectedServer.name}</p>
                                        <p className="text-xs text-muted">{selectedServer.description}</p>
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* OAuth Requirement Notice */}
                        {formData.requires_oauth && (
                            <div className="p-4 bg-blue-50 border border-blue-200 rounded-md">
                                <div className="flex items-start space-x-2">
                                    <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                    <div>
                                        <p className="text-sm font-medium text-blue-900 mb-1">OAuth Required</p>
                                        <p className="text-sm text-blue-700">
                                            This server requires OAuth authentication. You must select an OAuth client.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* OAuth Client Selection */}
                        {formData.requires_oauth && (
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    OAuth Client *
                                </label>
                                <select
                                    value={formData.oauth_client_id}
                                    disabled={!!config}
                                    onChange={(e) => setFormData({ ...formData, oauth_client_id: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                    required={formData.requires_oauth}
                                >
                                    <option value="">Select an OAuth client...</option>
                                    {oauthClients
                                        .filter(client => {
                                            // Filter by IdP match if server has IdP
                                            if (selectedServer?.idp_id) {
                                                return client.idp_id === selectedServer.idp_id;
                                            }
                                            return true;
                                        })
                                        .map((client) => (
                                            <option key={client.id} value={client.id}>
                                                {client.name} ({client.app_type})
                                            </option>
                                        ))}
                                </select>
                                {selectedServer?.idp_id && (
                                    <p className="text-xs text-muted mt-1">
                                        Showing only clients for the same Identity Provider
                                    </p>
                                )}
                                {/* What is configured, as the gateway reads it when a
                                    sign-in arrives. Nothing is refused here. */}
                                {selectedServer && (
                                    <p
                                        data-testid="server-idp-pairing"
                                        data-state={pairing.state}
                                        className={`text-xs mt-2 ${pairing.state === 'not_paired' ? 'text-amber-700' : 'text-secondary'}`}
                                    >
                                        {pairing.text}
                                    </p>
                                )}
                            </div>
                        )}

                        {/* Auth Type Display for Non-OAuth */}
                        {!formData.requires_oauth && selectedServer && (
                            <div className="p-4 bg-surface-alt border border-default rounded-md">
                                <div className="flex items-start space-x-2">
                                    <ShieldOff className="w-5 h-5 text-secondary flex-shrink-0 mt-0.5" />
                                    <div>
                                        <p className="text-sm font-medium text-default mb-1">No OAuth Required</p>
                                        <p className="text-sm text-secondary">
                                            This server does not require OAuth authentication. Users can access it directly.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* Configuration Name */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Configuration Name *
                            </label>
                            <input
                                type="text"
                                value={formData.name}
                                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                placeholder="e.g., GitHub for Web App"
                            />
                            <p className="text-xs text-muted mt-1">
                                Internal name to identify this configuration
                            </p>
                        </div>

                        {/* Description */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Description
                            </label>
                            <textarea
                                value={formData.description}
                                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                rows={2}
                                placeholder="Optional description for administrators"
                            />
                        </div>

                        {/* Display Name */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Display Name (for Users)
                            </label>
                            <input
                                type="text"
                                value={formData.display_name}
                                onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                placeholder="Leave empty to use server name"
                            />
                        </div>

                        {/* Display Description */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Display Description (for Users)
                            </label>
                            <textarea
                                value={formData.display_description}
                                onChange={(e) => setFormData({ ...formData, display_description: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                rows={2}
                                placeholder="Leave empty to use server description"
                            />
                        </div>

                        {/* Priority */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">
                                Priority
                            </label>
                            <input
                                type="number"
                                value={formData.priority}
                                onChange={(e) => setFormData({ ...formData, priority: parseInt(e.target.value) })}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                min="0"
                            />
                            <p className="text-xs text-muted mt-1">
                                Lower numbers appear first (0 = highest priority)
                            </p>
                        </div>

                        {/* ✨ Hierarchical User Groups Selector */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-2">
                                Access Control (Resource Groups)
                            </label>
                            <HierarchicalGroupSelector
                                groupTree={groupTree}
                                selectedGroupIds={formData.group_ids}
                                onSelectionChange={handleGroupSelectionChange}
                            />
                            <p className="text-xs text-muted mt-2">
                                🌳 <strong>Hierarchical Selection:</strong> Selecting a parent group automatically includes all child groups.
                                Leave empty for global access (all users).
                            </p>
                        </div>

                        {/* Toggles */}
                        <div className="space-y-3 pt-3 border-t border-default">
                            <label className="flex items-center space-x-2 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={formData.is_default}
                                    onChange={(e) => setFormData({ ...formData, is_default: e.target.checked })}
                                    className="rounded border-strong text-primary focus:ring-primary"
                                />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <span className="text-sm font-medium text-secondary">Set as Default</span>
                                    <p className="text-xs text-muted">
                                        This configuration will be used by default for this server
                                    </p>
                                </div>
                            </label>

                            <label className="flex items-center space-x-2 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={formData.enabled}
                                    onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                                    className="rounded border-strong text-primary focus:ring-primary"
                                />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <span className="text-sm font-medium text-secondary">Enabled</span>
                                    <p className="text-xs text-muted">
                                        Users can access this server configuration
                                    </p>
                                </div>
                            </label>
                        </div>
                    </div>
                    </>
                    )}
                </div>

                {/* Footer */}
                <div className="px-6 py-4 bg-surface-alt border-t flex items-center justify-end space-x-3">
                    <button
                        onClick={onCancel}
                        disabled={saving}
                        className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={asyncHandler(handleSubmit, 'click')}
                        disabled={saving}
                        className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-md hover:bg-primary-700 disabled:opacity-50"
                    >
                        {saving ? 'Saving...' : config ? 'Update Configuration' : 'Create Configuration'}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default ServerConfigForm;

// ---------------------------------------------------------------------------
// McpServerPicker — custom listbox replacement for a native <select>.
//
// Why not <select>: the admin needs to see whether each MCP server
// requires OAuth at a glance. Native `<option>` elements are plain
// text; browsers strip any markup inside them, so we can't put an
// icon there. This component keeps the visual indicator while staying
// keyboard-accessible (Arrow keys + Enter + Esc).
// ---------------------------------------------------------------------------

interface McpServerPickerProps {
    servers: any[];
    value: string;
    onChange: (id: string) => void;
    getIconUrl: (server: any) => string | undefined;
}

const authBadge = (authType: string | undefined) => {
    const requiresOAuth =
        authType === 'oauth' || authType === 'oauth2' || (authType && authType !== 'none');
    if (requiresOAuth) {
        return (
            <span className="inline-flex items-center gap-1 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2 py-0.5">
                <KeyRound className="w-3 h-3" />
                OAuth
            </span>
        );
    }
    return (
        <span className="inline-flex items-center gap-1 text-xs text-secondary bg-surface-alt border border-default rounded-full px-2 py-0.5">
            <ShieldOff className="w-3 h-3" />
            No auth
        </span>
    );
};

const McpServerPicker: React.FC<McpServerPickerProps> = ({
    servers,
    value,
    onChange,
    getIconUrl,
}) => {
    const [open, setOpen] = useState(false);
    const [highlight, setHighlight] = useState<number>(-1);
    const rootRef = useRef<HTMLDivElement>(null);

    const selected = servers.find((s) => s.id === value) || null;

    // Close on outside click.
    useEffect(() => {
        if (!open) return;
        const onDoc = (e: MouseEvent) => {
            if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener('mousedown', onDoc);
        return () => document.removeEventListener('mousedown', onDoc);
    }, [open]);

    // Seed the highlight to the currently-selected row when opening.
    useEffect(() => {
        if (!open) return;
        const idx = servers.findIndex((s) => s.id === value);
        setHighlight(idx >= 0 ? idx : 0);
    }, [open, servers, value]);

    const commit = (id: string) => {
        onChange(id);
        setOpen(false);
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if (!open) {
            if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
                e.preventDefault();
                setOpen(true);
            }
            return;
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            setOpen(false);
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            setHighlight((h) => Math.min(servers.length - 1, h + 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setHighlight((h) => Math.max(0, h - 1));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (highlight >= 0 && highlight < servers.length) {
                commit(servers[highlight].id);
            }
        }
    };

    const renderRow = (server: any) => {
        const url = getIconUrl(server);
        return (
            <>
                {url ? (
                    <img
                        src={url}
                        alt=""
                        className="w-5 h-5 rounded object-contain flex-shrink-0 bg-surface-alt"
                        onError={(e) => {
                            (e.target as HTMLImageElement).style.visibility = 'hidden';
                        }}
                    />
                ) : (
                    <span className="w-5 h-5 rounded bg-surface-alt flex-shrink-0" />
                )}
                <span className="flex-1 min-w-0 truncate text-default min-h-0">{server.name}</span>
                {authBadge(server.auth_type)}
            </>
        );
    };

    return (
        <div
            ref={rootRef}
            className="relative"
            tabIndex={0}
            onKeyDown={onKeyDown}
            role="combobox"
            aria-haspopup="listbox"
            aria-expanded={open}
        >
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                className="w-full flex items-center gap-2 px-3 py-2 border border-strong rounded-md bg-surface text-sm hover:border-primary-300 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
                {selected ? (
                    renderRow(selected)
                ) : (
                    <span className="flex-1 text-left text-muted min-h-0 min-w-0">Select an MCP server…</span>
                )}
                <ChevronDown
                    className={`w-4 h-4 flex-shrink-0 text-muted transition-transform ${
                        open ? 'rotate-180' : ''
                    }`}
                />
            </button>

            {open && (
                <ul
                    role="listbox"
                    className="absolute z-50 mt-1 w-full max-h-64 overflow-y-auto bg-surface-raised border border-strong rounded-md shadow-lg"
                >
                    {servers.length === 0 && (
                        <li className="px-3 py-2 text-sm text-muted">No MCP servers available.</li>
                    )}
                    {servers.map((server, idx) => {
                        const isSelected = server.id === value;
                        const isHighlight = idx === highlight;
                        return (
                            <li
                                key={server.id}
                                role="option"
                                aria-selected={isSelected}
                                onMouseEnter={() => setHighlight(idx)}
                                onClick={() => commit(server.id)}
                                className={`flex items-center gap-2 px-3 py-2 cursor-pointer ${
                                    isHighlight ? 'bg-primary-50' : ''
                                } ${isSelected ? 'font-medium' : ''}`}
                            >
                                {renderRow(server)}
                            </li>
                        );
                    })}
                </ul>
            )}
        </div>
    );
};