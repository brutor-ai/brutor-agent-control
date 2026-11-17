/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import {
    Edit,
    Trash2,
    Power,
    PowerOff,
    AlertCircle,
    CheckCircle,
    Globe,
    Key,
    Shield,
    Settings,
    Clock,
    Info,
    AlertTriangle,
    MoreVertical,
    Activity,
    Link,
    RefreshCw
} from 'lucide-react';

import { env } from '../../../../env.ts';

import ServerIcon from './ServerIcon.tsx';
import EndpointModal from './EndpointModal.tsx';
import CapabilityViewerDialog from './CapabilityViewerDialog.tsx';
import type { Capabilities } from './CapabilityViewerDialog.tsx';
import { runBackground } from '../../../../lib/asyncSafe';
import { oauthPairingOf } from '../../../../lib/oauthPairing';
import type { ServerHealthStatus } from '../../../../lib/serverHealth';
import ServerHealthBadge from '../../ServerHealthBadge';
import { MAX_CONCURRENT_REQUESTS_DEFAULT, MAX_CONCURRENT_REQUESTS_LABEL } from '../../../../lib/mcpServerConcurrency';

interface ServerRowProps {
    server: {
        id: string;
        name: string;
        description?: string;
        base_url: string;
        mcp_endpoint_path: string;
        auth_type: string;
        idp_id?: string;           // OAuth IdP ID
        idp_name?: string;         // OAuth IdP name for display
        /** What the Control Plane states of an OAuth-protected server as stored; null for any other. */
        oauth_pairing?: { paired: boolean; idp_ids?: string[]; warning?: string | null } | null;
        timeout: number;
        /** Requests in flight at once on one gateway instance; null for the default of 10. */
        max_concurrent_requests?: number | null;
        enabled: boolean;
        /* Shared with the viewer dialog rather than re-declared, so the
           row and the dialog can never drift on what a capability is. */
        capabilities?: Capabilities;
        discovered_at?: string;
        /** Negotiated MCP protocol version, e.g. "2026-07-28". */
        protocol_version?: string | null;
        /** MCP wire dialect: 'handshake' | 'stateless' | 'dual' (null until discovered). */
        dialect?: string | null;
        health_status?: ServerHealthStatus;
        icon?: {
            type: string;
            url: string;
            mime_type: string;
            size: number;
            uploaded_at: string;
            available_sizes?: string[];
        };
    };
    isSelected: boolean;
    loading: boolean;
    onSelect: (selected: boolean) => void;
    onToggle: () => void;
    onEdit: () => void;
    onDelete: () => void;
    onHealthCheck?: (serverId: string) => Promise<void>;
    onRefreshCapabilities?: (serverId: string) => Promise<void>;
    onShowOAuthInfo?: (serverId: string) => void;
    gatewayBaseUrl?: string;
}

const ServerRow: React.FC<ServerRowProps> = ({
                                                 server,
                                                 isSelected,
                                                 loading,
                                                 onSelect,
                                                 onToggle,
                                                 onEdit,
                                                 onDelete,
                                                 onHealthCheck,
                                                 onRefreshCapabilities,
                                                 onShowOAuthInfo,
                                                 // The Core Gateway's address: the MCP endpoint is a
                                                 // route of the gateway, not of the Control Plane.
                                                 // ServersTab passes it; this default is for a
                                                 // caller that does not.
                                                 gatewayBaseUrl = env.BRUTOR_GATEWAY_PUBLIC_URL
                                             }) => {
    const pairing = oauthPairingOf(server);
    const [showEndpoint, setShowEndpoint] = useState(false);
    const [showCapabilities, setShowCapabilities] = useState(false);
    const [showMenu, setShowMenu] = useState(false);
    const [menuPos, setMenuPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
    const buttonRef = useRef<HTMLButtonElement>(null);
    const menuRef = useRef<HTMLDivElement>(null);

    const updateMenuPosition = useCallback(() => {
        if (buttonRef.current) {
            const rect = buttonRef.current.getBoundingClientRect();
            const menuHeight = 220; // approximate height of the 5-item menu
            const spaceBelow = window.innerHeight - rect.bottom;
            const top = spaceBelow < menuHeight
                ? rect.top - menuHeight - 4   // flip upward
                : rect.bottom + 4;            // open downward
            setMenuPos({
                top,
                left: rect.right - 192, // 192px = w-48
            });
        }
    }, []);

    // Close menu on outside click or scroll
    useEffect(() => {
        if (!showMenu) return;

        const handleClickOutside = (e: MouseEvent) => {
            if (
                menuRef.current && !menuRef.current.contains(e.target as Node) &&
                buttonRef.current && !buttonRef.current.contains(e.target as Node)
            ) {
                setShowMenu(false);
            }
        };
        const handleScroll = () => setShowMenu(false);

        document.addEventListener('mousedown', handleClickOutside);
        window.addEventListener('scroll', handleScroll, true);
        return () => {
            document.removeEventListener('mousedown', handleClickOutside);
            window.removeEventListener('scroll', handleScroll, true);
        };
    }, [showMenu]);

    // The server's address: its base URL and its MCP endpoint path.
    const getCompleteMcpUrl = () => {
        if (!server.base_url) return 'Not configured';
        return `${server.base_url.replace(/\/$/, '')}${server.mcp_endpoint_path || ''}`;
    };

    const getAuthIcon = (authType: string) => {
        switch (authType) {
            case 'oauth':
                return <Shield className="w-4 h-4 text-primary-600" />;
            case 'bearer':
                return <Key className="w-4 h-4 text-primary-600" />;
            default:
                return <Globe className="w-4 h-4 text-primary-600" />;
        }
    };

    const getCapabilitiesStatus = () => {
        if (!server.capabilities) {
            return { status: 'none', label: 'No capabilities discovered', color: 'gray' };
        }

        const totalCapabilities =
            (server.capabilities.tools?.length || 0) +
            (server.capabilities.resources?.length || 0) +
            (server.capabilities.prompts?.length || 0);

        if (totalCapabilities === 0) {
            return { status: 'empty', label: 'No capabilities found', color: 'yellow' };
        }

        return {
            status: 'discovered',
            label: `${totalCapabilities} capabilities`,
            color: 'green',
            discovered_at: server.discovered_at
        };
    };

    const formatDiscoveryTime = (timestamp?: string) => {
        if (!timestamp) return '';
        const date = new Date(timestamp);
        const now = new Date();
        const diffMs = now.getTime() - date.getTime();
        const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
        const diffDays = Math.floor(diffHours / 24);

        if (diffDays > 0) {
            return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
        } else if (diffHours > 0) {
            return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
        } else {
            return 'Recently';
        }
    };

    const capabilitiesStatus = getCapabilitiesStatus();
    const completeMcpUrl = getCompleteMcpUrl();

    return (
        <>
            <tr className="hover:bg-surface-alt transition-colors">
                {/* Checkbox */}
                <td className="px-6 py-4 whitespace-nowrap">
                    <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={(e) => onSelect(e.target.checked)}
                        className="rounded border-strong text-primary focus:ring-primary"
                    />
                </td>

                {/* Server: health + name + enabled badge + description */}
                <td className="px-6 py-4">
                    <div className="flex items-center">
                        <div className="flex-shrink-0 h-8 w-8 mr-3">
                            <ServerIcon
                                key={`${server.id}-${server.icon?.uploaded_at || 'no-icon'}`}
                                serverId={server.id}
                                serverName={server.name}
                                iconInfo={server.icon}
                                size="small"
                                editable={false}
                            />
                        </div>
                        <div className="ml-1 min-w-0">
                            <div className="flex items-center space-x-2">
                                <span className="text-sm font-medium text-default truncate">{server.name}</span>
                                <ServerHealthBadge shape="tag" health={server.health_status} />
                                <button
                                    onClick={() => setShowEndpoint(true)}
                                    className="text-xs text-primary hover:text-primary-800 transition-colors flex-shrink-0"
                                >
                                    Endpoint
                                </button>
                            </div>
                            <div className="text-xs text-muted mt-0.5 font-mono truncate max-w-xs" title={server.id}>
                                {server.id}
                            </div>
                            {server.description && (
                                <div className="text-xs text-muted mt-0.5 max-w-xs truncate" title={server.description}>
                                    {server.description}
                                </div>
                            )}
                        </div>
                    </div>
                </td>

                {/* Enabled/Disabled Status */}
                <td className="px-6 py-4 whitespace-nowrap">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        server.enabled
                            ? 'bg-green-100 text-green-700'
                            : 'bg-surface-raised text-muted'
                    }`}>
                        {server.enabled
                            ? <><Power className="w-3 h-3 mr-1" />Enabled</>
                            : <><PowerOff className="w-3 h-3 mr-1" />Disabled</>
                        }
                    </span>
                </td>

                {/* MCP Endpoint URL only */}
                <td className="px-6 py-4">
                    <div className="text-sm text-primary-700 font-mono truncate max-w-xs" title={completeMcpUrl}>
                        {completeMcpUrl}
                    </div>
                    <div className="text-xs text-muted mt-1" data-testid="server-max-concurrent-requests">
                        {MAX_CONCURRENT_REQUESTS_LABEL}:{' '}
                        {server.max_concurrent_requests ?? `${MAX_CONCURRENT_REQUESTS_DEFAULT} (default)`}
                    </div>
                </td>

                {/* Auth Type */}
                <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex flex-col space-y-1">
                        <div className="flex items-center space-x-2">
                            {getAuthIcon(server.auth_type)}
                            <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                                server.auth_type === 'oauth' ? 'bg-green-100 text-primary-800' :
                                    server.auth_type === 'bearer' ? 'bg-yellow-100 text-primary-800' :
                                        'bg-surface-raised text-default'
                            }`}>
                                {server.auth_type}
                            </span>
                        </div>
                        {server.auth_type === 'oauth' && server.idp_name && (
                            <div className="flex items-center space-x-1 text-xs text-secondary">
                                <Shield className="w-3 h-3 text-green-600" />
                                <span title={`Identity Provider: ${server.idp_name} (${server.idp_id})`}>
                                    {server.idp_name}
                                </span>
                            </div>
                        )}
                        {/* What the Control Plane states of the stored server comes first:
                            its warning when no identity provider is paired, and that a
                            server naming none is paired through an OAuth client. Without
                            its statement the row says only what it sees. */}
                        {server.auth_type === 'oauth' && !server.idp_name && !pairing && (
                            <div className="flex items-center space-x-1 text-xs text-amber-600" title="No Identity Provider linked">
                                <AlertTriangle className="w-3 h-3" />
                                <span>No IdP linked</span>
                            </div>
                        )}
                        {server.auth_type === 'oauth' && !server.idp_name && pairing?.paired && (
                            <div
                                className="flex items-center space-x-1 text-xs text-secondary"
                                title={`The gateway accepts OAuth sign-ins for this server from: ${pairing.idpIds.join(', ')}`}
                            >
                                <Shield className="w-3 h-3 text-green-600" />
                                <span>Paired through an OAuth client</span>
                            </div>
                        )}
                        {server.auth_type === 'oauth' && pairing?.warning && (
                            <div
                                className="flex items-start space-x-1 text-xs text-amber-700 whitespace-normal max-w-xs"
                                data-testid="server-oauth-pairing-warning"
                            >
                                <AlertTriangle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                                <span>{pairing.warning}</span>
                            </div>
                        )}
                    </div>
                </td>

                {/* Capabilities */}
                <td
                    className={`px-6 py-4 whitespace-nowrap text-sm text-muted ${capabilitiesStatus.status === 'discovered' ? 'cursor-pointer hover:bg-surface-alt' : ''}`}
                    onClick={() => capabilitiesStatus.status === 'discovered' && setShowCapabilities(true)}
                    title={capabilitiesStatus.status === 'discovered' ? 'Click to view capabilities' : undefined}
                >
                    <div className="space-y-1">
                        <div className="flex items-center space-x-2">
                            <div className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                                capabilitiesStatus.color === 'green' ? 'bg-primary-100 text-green-800' :
                                    capabilitiesStatus.color === 'yellow' ? 'bg-primary-100 text-yellow-800' :
                                        'bg-surface-raised text-default'
                            }`}>
                                {capabilitiesStatus.status === 'discovered' && <CheckCircle className="w-3 h-3 mr-1" />}
                                {capabilitiesStatus.status === 'empty' && <AlertCircle className="w-3 h-3 mr-1" />}
                                {capabilitiesStatus.status === 'none' && <Settings className="w-3 h-3 mr-1" />}
                                {capabilitiesStatus.label}
                            </div>
                        </div>

                        {(capabilitiesStatus.status === 'none' || capabilitiesStatus.status === 'empty') &&
                            server.auth_type === 'oauth' && onShowOAuthInfo && (
                                <div>
                                    <button
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            onShowOAuthInfo(server.id);
                                        }}
                                        className="inline-flex items-center space-x-1 text-xs text-blue-600 hover:text-blue-800 font-medium group"
                                        title="Learn about capability discovery for OAuth servers"
                                    >
                                        <Info className="w-3 h-3 group-hover:scale-110 transition-transform" />
                                        <span className="underline">How to discover capabilities</span>
                                    </button>
                                </div>
                            )}

                        {server.capabilities && (
                            <div className="flex flex-wrap gap-1">
                                {server.capabilities.tools && server.capabilities.tools.length > 0 && (
                                    <span className="bg-primary-100 text-primary-800 text-xs px-2 py-1 rounded">
                                        {server.capabilities.tools.length} tools
                                    </span>
                                )}
                                {server.capabilities.resources && server.capabilities.resources.length > 0 && (
                                    <span className="bg-primary-100 text-primary-800 text-xs px-2 py-1 rounded">
                                        {server.capabilities.resources.length} resources
                                    </span>
                                )}
                                {server.capabilities.prompts && server.capabilities.prompts.length > 0 && (
                                    <span className="bg-primary-100 text-primary-800 text-xs px-2 py-1 rounded">
                                        {server.capabilities.prompts.length} prompts
                                    </span>
                                )}
                            </div>
                        )}

                        {capabilitiesStatus.discovered_at && (
                            <div className="flex items-center text-xs text-muted">
                                <Clock className="w-3 h-3 mr-1" />
                                <span>{formatDiscoveryTime(capabilitiesStatus.discovered_at)}</span>
                            </div>
                        )}

                        {/* Negotiated MCP wire contract badge */}
                        {(server.protocol_version || server.dialect) && (
                            <div className="flex items-center gap-1 mt-1">
                                <span
                                    className={`text-xs px-2 py-0.5 rounded font-mono ${
                                        server.dialect === 'stateless'
                                            ? 'bg-emerald-100 text-emerald-800'
                                            : server.dialect === 'dual'
                                                ? 'bg-sky-100 text-sky-800'
                                                : 'bg-slate-100 text-slate-700'
                                    }`}
                                    title={
                                        server.dialect === 'dual'
                                            ? "Dual-era: this server answers both the legacy 'initialize' handshake and the stateless 2026-07-28 'server/discover'"
                                            : 'MCP protocol version and wire dialect negotiated at capability discovery'
                                    }
                                >
                                    MCP {server.protocol_version ?? '?'}
                                    {server.dialect ? ` · ${server.dialect}` : ''}
                                </span>
                            </div>
                        )}
                    </div>
                </td>

                {/* Actions: three-dots dropdown */}
                <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                    <div className="relative inline-block">
                        <button
                            ref={buttonRef}
                            onClick={() => {
                                if (!showMenu) updateMenuPosition();
                                setShowMenu(!showMenu);
                            }}
                            className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                        >
                            <MoreVertical className="w-4 h-4" />
                        </button>

                        {showMenu && createPortal(
                            <div
                                ref={menuRef}
                                className="w-48 bg-surface rounded-lg shadow-lg border border-default py-1"
                                style={{
                                    position: 'fixed',
                                    top: menuPos.top,
                                    left: menuPos.left,
                                    zIndex: 9999,
                                }}
                            >
                                <button
                                    onClick={() => { setShowMenu(false); onEdit(); }}
                                    disabled={loading}
                                    className="w-full flex items-center space-x-2 px-4 py-2 text-sm text-secondary hover:bg-surface-alt disabled:opacity-50"
                                >
                                    <Edit className="w-4 h-4" />
                                    <span>Edit</span>
                                </button>
                                <button
                                    onClick={() => { setShowMenu(false); onToggle(); }}
                                    disabled={loading}
                                    className={`w-full flex items-center space-x-2 px-4 py-2 text-sm hover:bg-surface-alt disabled:opacity-50 ${
                                        server.enabled ? 'text-amber-600' : 'text-green-600'
                                    }`}
                                >
                                    {server.enabled ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                                    <span>{server.enabled ? 'Disable' : 'Enable'}</span>
                                </button>
                                {onHealthCheck && (
                                    <button
                                        onClick={() => { setShowMenu(false); runBackground(onHealthCheck(server.id), 'health check'); }}
                                        className="w-full flex items-center space-x-2 px-4 py-2 text-sm text-secondary hover:bg-surface-alt"
                                    >
                                        <Activity className="w-4 h-4" />
                                        <span>Health Check</span>
                                    </button>
                                )}
                                {onRefreshCapabilities && (
                                    <button
                                        onClick={() => { setShowMenu(false); runBackground(onRefreshCapabilities(server.id), 'refresh capabilities'); }}
                                        disabled={loading}
                                        title="Re-fetch tools/list from the upstream MCP server"
                                        className="w-full flex items-center space-x-2 px-4 py-2 text-sm text-secondary hover:bg-surface-alt disabled:opacity-50"
                                    >
                                        <RefreshCw className="w-4 h-4" />
                                        <span>Refresh Capabilities</span>
                                    </button>
                                )}
                                <button
                                    onClick={() => { setShowMenu(false); setShowEndpoint(true); }}
                                    className="w-full flex items-center space-x-2 px-4 py-2 text-sm text-secondary hover:bg-surface-alt"
                                >
                                    <Link className="w-4 h-4" />
                                    <span>Endpoint Info</span>
                                </button>
                                <div className="border-t border-default my-1" />
                                <button
                                    onClick={() => { setShowMenu(false); onDelete(); }}
                                    disabled={loading}
                                    className="w-full flex items-center space-x-2 px-4 py-2 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50"
                                >
                                    <Trash2 className="w-4 h-4" />
                                    <span>Delete</span>
                                </button>
                            </div>,
                            document.body
                        )}
                    </div>
                </td>
            </tr>

            {showEndpoint && (
                <EndpointModal
                    serverId={server.id}
                    serverName={server.name}
                    gatewayBaseUrl={gatewayBaseUrl}
                    onClose={() => setShowEndpoint(false)}
                />
            )}

            <CapabilityViewerDialog
                isOpen={showCapabilities}
                onClose={() => setShowCapabilities(false)}
                serverName={server.name}
                serverId={server.id}
                capabilities={server.capabilities || null}
            />
        </>
    );
};

export default ServerRow;