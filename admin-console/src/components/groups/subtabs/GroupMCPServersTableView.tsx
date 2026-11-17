/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import { createPortal } from 'react-dom';
import {
    Trash2,
    CheckCircle,
    XCircle,
    ArrowUpRight,
    Link as LinkIcon,
    Loader,
    ShieldCheck,
    Filter,
    Wrench,
    FolderOpen,
    MessageSquare,
    FileText,
    AlertTriangle,
    Gauge,
    Info
} from 'lucide-react';
import { ServerConfigInfo } from '../../../lib/groupTypes';
import ServerHealthBadge from '../../mcp/ServerHealthBadge';
import { env } from '../../../env';

interface ServerIconDisplayProps {
    server: ServerConfigInfo;
}

const ServerIconDisplay: React.FC<ServerIconDisplayProps> = ({ server }) => {
    const [imageLoading, setImageLoading] = useState(true);
    const [iconError, setIconError] = useState(false);

    const getIconUrl = (): string | null => {
        // Check if icon exists in mcp_server
        if (!server.mcp_server?.icon?.url) return null;

        let baseUrl = server.mcp_server.icon.url;

        // If URL is relative, prepend the gateway server URL
        if (baseUrl.startsWith('/')) {
            // Get gateway URL from environment or construct from window.location

            const gatewayUrl = env.BRUTOR_CONTROL_PLANE_PUBLIC_URL;
            baseUrl = `${gatewayUrl}${baseUrl}`;
        }

        // Add cache busting
        const timestamp = server.mcp_server.icon.uploaded_at
            ? new Date(server.mcp_server.icon.uploaded_at).getTime()
            : Date.now();
        const cacheKey = `${timestamp}_${Math.random().toString(36).substr(2, 9)}`;

        return `${baseUrl}?t=${cacheKey}`;
    };

    const iconUrl = getIconUrl();

    // No icon URL available - show fallback
    if (!iconUrl) {
        return (
            <div className="w-10 h-10 rounded-lg bg-primary-50 flex items-center justify-center border border-primary-200">
                <LinkIcon className="w-5 h-5 text-primary-600" />
            </div>
        );
    }

    // Icon URL exists but failed to load - show fallback
    if (iconError) {
        return (
            <div className="w-10 h-10 rounded-lg bg-primary-50 flex items-center justify-center border border-primary-200">
                <LinkIcon className="w-5 h-5 text-primary-600" />
            </div>
        );
    }

    // Icon URL exists - show image with loading overlay
    return (
        <div className="relative w-10 h-10">
            {/* Loading spinner overlay */}
            {imageLoading && (
                <div className="absolute inset-0 rounded-lg bg-surface-raised flex items-center justify-center border border-default z-10">
                    <Loader className="w-4 h-4 text-muted animate-spin" />
                </div>
            )}

            {/* Actual image */}
            <img
                src={iconUrl}
                alt={server.name}
                className="w-10 h-10 rounded-lg object-cover border border-default"
                onLoad={() => setImageLoading(false)}
                onError={() => {
                    setImageLoading(false);
                    setIconError(true);
                }}
            />
        </div>
    );
};

interface GroupMCPServersTableViewProps {
    servers: ServerConfigInfo[];
    onRemove: (serverId: string, serverName: string) => void;
    onConfigureCapabilities: (server: ServerConfigInfo) => void;
    onConfigureLimits?: (server: ServerConfigInfo) => void;
    onConfigureToolLimits?: (server: ServerConfigInfo) => void;
    removingId: string | null;
}

const GroupMCPServersTableView: React.FC<GroupMCPServersTableViewProps> = ({
                                                                               servers,
                                                                               onRemove,
                                                                               onConfigureCapabilities,
                                                                               onConfigureLimits,
                                                                               onConfigureToolLimits,
                                                                               removingId
                                                                           }) => {
    const [showEnabledTooltip, setShowEnabledTooltip] = useState(false);
    const enabledInfoRef = useRef<HTMLButtonElement>(null);
    const [tooltipPos, setTooltipPos] = useState({ top: 0, left: 0 });

    const handleToggleTooltip = useCallback(() => {
        if (!showEnabledTooltip && enabledInfoRef.current) {
            const rect = enabledInfoRef.current.getBoundingClientRect();
            setTooltipPos({
                top: rect.top - 8,
                left: rect.left + rect.width / 2,
            });
        }
        setShowEnabledTooltip(prev => !prev);
    }, [showEnabledTooltip]);

    // Helper function to render health status badge
    // Helper to render capability filter summary
    const renderCapabilityFilterSummary = (server: ServerConfigInfo) => {
        const caps = server.mcp_server?.capabilities;
        if (!caps) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-raised rounded-full">
                    <AlertTriangle className="w-3 h-3 text-muted" />
                    <span className="text-xs font-medium text-muted">Not discovered</span>
                </div>
            );
        }

        const tools = Array.isArray(caps.tools) ? caps.tools.length : 0;
        const resources = Array.isArray(caps.resources) ? caps.resources.length : 0;
        const prompts = Array.isArray(caps.prompts) ? caps.prompts.length : 0;
        const templates = Array.isArray(caps.resource_templates) ? caps.resource_templates.length : 0;
        const total = tools + resources + prompts + templates;

        if (total === 0) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-raised rounded-full">
                    <AlertTriangle className="w-3 h-3 text-muted" />
                    <span className="text-xs font-medium text-muted">Not discovered</span>
                </div>
            );
        }

        const filters = server.capability_filters;
        const hasDisabled = filters?.has_filters && filters.disabled > 0;
        const hasApproval = filters?.has_filters && (filters.approval_required ?? 0) > 0;

        // Build per-type disabled counts from filter data
        // We don't have per-type filter breakdown, so show a combined indicator
        const parts: { icon: React.FC<any>; count: number; label: string; color: string }[] = [];
        if (tools > 0) parts.push({ icon: Wrench, count: tools, label: 'tools', color: 'text-amber-600' });
        if (resources > 0) parts.push({ icon: FolderOpen, count: resources, label: 'resources', color: 'text-green-600' });
        if (prompts > 0) parts.push({ icon: MessageSquare, count: prompts, label: 'prompts', color: 'text-muted' });
        if (templates > 0) parts.push({ icon: FileText, count: templates, label: 'templates', color: 'text-primary' });

        return (
            <div className="flex items-center space-x-2">
                {parts.map(({ icon: Icon, count, label, color }, i) => (
                    <div key={i} className="inline-flex items-center space-x-0.5" title={`${count} ${label}`}>
                        <Icon className={`w-3 h-3 ${color}`} />
                        <span className="text-xs font-medium text-secondary">{count}</span>
                    </div>
                ))}
                {hasApproval && (
                    <div className="inline-flex items-center space-x-1 px-1.5 py-0.5 bg-surface-alt rounded-full" title={`${filters.approval_required} capabilities require approval`}>
                        <Filter className="w-3 h-3 text-amber-600" />
                        <span className="text-xs font-medium text-amber-700">{filters.approval_required} approval</span>
                    </div>
                )}
                {hasDisabled && (
                    <div className="inline-flex items-center space-x-1 px-1.5 py-0.5 bg-surface-alt rounded-full" title={`${filters.disabled} capabilities disabled`}>
                        <Filter className="w-3 h-3 text-red-600" />
                        <span className="text-xs font-medium text-red-700">{filters.disabled} off</span>
                    </div>
                )}
            </div>
        );
    };

    // Helper function to determine server type badge
    const getServerTypeBadge = (server: ServerConfigInfo) => {
        if (server.is_inherited) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                    <ArrowUpRight className="w-3 h-3 text-muted" />
                    <span className="text-xs font-medium text-secondary">Inherited</span>
                </div>
            );
        } else {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-primary-pale rounded-full">
                    <CheckCircle className="w-3 h-3 text-primary" />
                    <span className="text-xs font-medium text-primary">Direct</span>
                </div>
            );
        }
    };

    return (
        <div className="bg-surface rounded-lg border border-default overflow-hidden">
            <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-default">
                    <thead className="bg-surface-alt">
                    <tr>
                        <th scope="col" className={`${TABLE_TH_TYPE} w-16 px-4 py-3 text-left`}>
                            Icon
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Name
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Description
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Type
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Health
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            <div className="flex items-center space-x-1">
                                <span>Enabled</span>
                                <button
                                    ref={enabledInfoRef}
                                    onClick={handleToggleTooltip}
                                    className="text-muted hover:text-secondary transition-colors"
                                >
                                    <Info className="w-3.5 h-3.5" />
                                </button>
                                {showEnabledTooltip && createPortal(
                                    <>
                                        <div className="fixed inset-0 z-[9998]" onClick={() => setShowEnabledTooltip(false)} />
                                        <div
                                            className="fixed z-[9999] w-72 p-3 bg-gray-900 text-white text-xs rounded-lg shadow-lg"
                                            style={{ top: tooltipPos.top, left: tooltipPos.left, transform: 'translate(-50%, -100%)' }}
                                        >
                                            <p className="font-semibold mb-1.5">Two-level enable/disable</p>
                                            <p className="mb-1">This status reflects both the <strong>MCP Server</strong> and its <strong>Server Configuration</strong> enabled settings.</p>
                                            <p className="mb-1">If the MCP Server is disabled globally, all its configurations are effectively disabled — shown as <em>"Disabled (by MCP Server)"</em>.</p>
                                            <p>The Server Configuration's own enabled setting only takes effect when the parent MCP Server is enabled.</p>
                                            <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
                                        </div>
                                    </>,
                                    document.body
                                )}
                            </div>
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Capabilities
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Limits
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-right`}>
                            Actions
                        </th>
                    </tr>
                    </thead>
                    <tbody className="bg-surface divide-y divide-default">
                    {servers.map((server) => {
                        const isRemoving = removingId === server.id;
                        const isInherited = server.is_inherited;
                        const isDirect = !isInherited;

                        return (
                            <tr
                                key={server.id}
                                className={`transition-colors ${
                                    isInherited
                                        ? 'bg-surface-alt'
                                        : 'hover:bg-surface-alt'
                                }`}
                            >
                                {/* Icon */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <ServerIconDisplay server={server} />
                                </td>

                                {/* Name & ID Combined */}
                                <td className="px-4 py-4">
                                    <div className="flex flex-col">
                                        <div className="text-sm font-medium text-default">
                                            {server.name}
                                        </div>
                                        <code className="text-xs text-muted font-mono mt-0.5">
                                            {server.id}
                                        </code>
                                    </div>
                                </td>

                                {/* Description */}
                                <td className="px-4 py-4">
                                    <div className="text-sm text-secondary max-w-xs">
                                        {server.description ? (
                                            <span className="line-clamp-2">{server.description}</span>
                                        ) : (
                                            <span className="text-muted italic">No description</span>
                                        )}
                                    </div>
                                </td>

                                {/* Type (Direct/Inherited/Global) */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {getServerTypeBadge(server)}
                                </td>

                                {/* Health Status */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <ServerHealthBadge shape="pill" health={server.mcp_server?.health_status} />
                                </td>

                                {/* Enabled Status */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {server.enabled ? (
                                        <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                                            <CheckCircle className="w-3 h-3 text-green-600" />
                                            <span className="text-xs font-medium text-green-700">Enabled</span>
                                        </div>
                                    ) : (
                                        <div>
                                            <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-raised rounded-full">
                                                <XCircle className="w-3 h-3 text-muted" />
                                                <span className="text-xs font-medium text-secondary">Disabled</span>
                                            </div>
                                            {server.mcp_server && server.mcp_server.enabled === false && (
                                                <div className="text-xs text-amber-600 mt-1">(by MCP Server)</div>
                                            )}
                                        </div>
                                    )}
                                </td>

                                {/* Capabilities */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <button
                                        onClick={() => onConfigureCapabilities(server)}
                                        className="hover:bg-surface-raised rounded-lg px-1.5 py-1 -mx-1.5 -my-1 transition-colors"
                                        title="Configure governance"
                                    >
                                        {renderCapabilityFilterSummary(server)}
                                    </button>
                                </td>

                                {/* Limits (per-tool rate limits) */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <button
                                        onClick={() => onConfigureToolLimits?.(server)}
                                        className="inline-flex items-center space-x-1 px-2 py-1 rounded-full bg-surface-raised hover:bg-surface-alt transition-colors"
                                        title="Configure per-tool rate limits"
                                    >
                                        <Gauge className="w-3 h-3 text-muted" />
                                        <span className="text-xs font-medium text-secondary">Tool limits</span>
                                    </button>
                                </td>

                                {/* Actions */}
                                <td className="px-4 py-4 whitespace-nowrap text-right">
                                    <div className="flex items-center justify-end space-x-1">
                                        <button
                                            onClick={() => onConfigureCapabilities(server)}
                                            className="p-1.5 text-muted hover:text-primary-600 hover:bg-primary-50 rounded transition-colors"
                                            title="Configure governance"
                                        >
                                            <ShieldCheck className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={() => onConfigureLimits?.(server)}
                                            className={`relative p-1.5 rounded transition-colors ${
                                                server.has_limits
                                                    ? 'text-amber-600 hover:text-amber-700 hover:bg-surface-alt'
                                                    : 'text-muted hover:text-secondary hover:bg-surface-alt'
                                            }`}
                                            title={server.has_limits ? "Per-server limits active — click to configure" : "Configure per-server limits"}
                                        >
                                            <Gauge className="w-4 h-4" />
                                            {server.has_limits && (
                                                <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-amber-500 rounded-full ring-2 ring-white" />
                                            )}
                                        </button>
                                        {isDirect && (
                                            <button
                                                onClick={() => onRemove(server.id, server.name)}
                                                disabled={isRemoving}
                                                className="p-1.5 text-red-600 hover:text-red-700 hover:bg-surface-alt rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                                title="Remove server"
                                            >
                                                {isRemoving ? (
                                                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-red-600"></div>
                                                ) : (
                                                    <Trash2 className="w-4 h-4" />
                                                )}
                                            </button>
                                        )}
                                    </div>
                                </td>
                            </tr>
                        );
                    })}
                    </tbody>
                </table>
            </div>

            {/* Footer with count */}
            <div className="px-4 py-3 bg-surface-alt border-t border-default">
                <div className="text-sm text-secondary">
                    Showing <span className="font-medium">{servers.length}</span> server{servers.length !== 1 ? 's' : ''}
                </div>
            </div>
        </div>
    );
};

export default GroupMCPServersTableView;