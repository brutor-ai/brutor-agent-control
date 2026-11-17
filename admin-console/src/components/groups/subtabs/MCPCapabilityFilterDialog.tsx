/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    X,
    Loader,
    AlertCircle,
    Settings2,
    Wrench,
    FolderOpen,
    MessageSquare,
    FileText,
    ToggleLeft,
    ToggleRight,
    Info,
    AlertTriangle,
    Lock,
    Shield,
    Clock
} from 'lucide-react';
import { ServerConfigInfo, MCPCapabilityInfo, MCPDiscoveredCapability, CapabilityAccess } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import ArgumentPolicyGateBadge from './ArgumentPolicyGateBadge';
import ReadFailureNotice from '../../common/ReadFailureNotice';
import { useReadFailures } from '../../../hooks/useRead';
import type { ArgumentPolicy } from '../../../lib/argumentPolicyTypes';
import { useToast } from '../../common/Toast';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { APPROVAL_TIMEOUT_MAX_SECONDS, APPROVAL_TIMEOUT_MIN_SECONDS, approvalTimeoutProblem } from '../../../lib/approvalTimeout';

type CapabilityType = 'tool' | 'resource' | 'prompt' | 'resource_template';

const ACCESS_STRICTNESS: Record<CapabilityAccess, number> = {
    enabled: 0,
    approval_required: 1,
    disabled: 2,
};

interface Props {
    isOpen: boolean;
    onClose: () => void;
    groupId: string;
    groupName: string;
    server: ServerConfigInfo;
}

const CAPABILITY_TABS: { type: CapabilityType; label: string; icon: React.FC<any>; color: string }[] = [
    { type: 'tool', label: 'Tools', icon: Wrench, color: 'amber' },
    { type: 'resource', label: 'Resources', icon: FolderOpen, color: 'green' },
    { type: 'prompt', label: 'Prompts', icon: MessageSquare, color: 'purple' },
    { type: 'resource_template', label: 'Templates', icon: FileText, color: 'blue' },
];

const ACCESS_LABELS: { value: CapabilityAccess; label: string; short: string }[] = [
    { value: 'enabled', label: 'Enabled', short: 'E' },
    { value: 'approval_required', label: 'Approval', short: 'A' },
    { value: 'disabled', label: 'Disabled', short: 'D' },
];

/** Three-state segmented control for a single capability */
const AccessControl: React.FC<{
    access: CapabilityAccess;
    minAccess?: CapabilityAccess;
    onChange: (access: CapabilityAccess) => void;
}> = ({ access, minAccess, onChange }) => {
    const minStrictness = minAccess ? ACCESS_STRICTNESS[minAccess] : 0;

    return (
        <div className="flex rounded-md overflow-hidden border border-default text-xs font-medium">
            {ACCESS_LABELS.map(({ value, short }) => {
                const isActive = access === value;
                const isLocked = ACCESS_STRICTNESS[value] < minStrictness;

                let bg = 'bg-surface text-muted hover:bg-surface-alt';
                if (isActive) {
                    if (value === 'enabled') bg = 'bg-surface-alt text-green-700 border-default';
                    else if (value === 'approval_required') bg = 'bg-surface-alt text-amber-700 border-default';
                    else bg = 'bg-surface-alt text-red-700 border-default';
                }
                if (isLocked) bg = 'bg-surface-alt text-disabled cursor-not-allowed';

                return (
                    <button
                        key={value}
                        onClick={() => !isLocked && onChange(value)}
                        disabled={isLocked}
                        className={`px-2 py-1 transition-colors ${bg} ${!isActive && !isLocked ? 'hover:bg-surface-raised' : ''}`}
                        title={isLocked ? `Cannot relax — parent requires ${minAccess?.replace('_', ' ')}` : value.replace('_', ' ')}
                    >
                        {short}
                    </button>
                );
            })}
        </div>
    );
};

const MCPCapabilityFilterDialog: React.FC<Props> = ({
    isOpen,
    onClose,
    groupId,
    groupName,
    server,
}) => {
    const { getMCPCapabilityFilters, updateMCPCapabilityFilters, clearMCPCapabilityFilters, listArgumentPolicies } = useGateway();
    const toast = useToast();

    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [activeTab, setActiveTab] = useState<CapabilityType>('tool');
    const [capabilities, setCapabilities] = useState<MCPCapabilityInfo[]>([]);
    const [discoveredCaps, setDiscoveredCaps] = useState<MCPDiscoveredCapability[]>([]);
    const [hasFilters, setHasFilters] = useState(false);
    const [newCapName, setNewCapName] = useState('');

    const hasDiscovered = discoveredCaps.length > 0;
    const isOAuth = server.mcp_server?.auth_type === 'oauth';

    const loadFilters = useCallback(async () => {
        try {
            setLoading(true);
            setError('');

            const response = await getMCPCapabilityFilters(groupId, server.id);
            const existingFilters = response.capabilities;
            const discovered = response.discovered_capabilities || [];
            setHasFilters(response.has_filters);
            setDiscoveredCaps(discovered);

            if (existingFilters.length > 0) {
                const filterSet = new Set(
                    existingFilters.map(f => `${f.capability_type}:${f.capability_name}`)
                );
                const merged = [...existingFilters];

                for (const cap of discovered) {
                    const key = `${cap.capability_type}:${cap.name}`;
                    if (!filterSet.has(key)) {
                        merged.push({
                            capability_type: cap.capability_type,
                            capability_name: cap.name,
                            access: 'enabled',
                        });
                        filterSet.add(key);
                    }
                }

                setCapabilities(merged);
            } else {
                setCapabilities(discovered.map(cap => ({
                    capability_type: cap.capability_type,
                    capability_name: cap.name,
                    access: 'enabled',
                })));
            }
        } catch (err) {
            console.error('Failed to load capability filters:', errorKind(err));
            setError(errorMessage(err, 'Failed to load capability filters'));
        } finally {
            setLoading(false);
        }
    }, [groupId, server.id, getMCPCapabilityFilters]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadFilters(), 'filters');
        }
    }, [isOpen, loadFilters]);

    // The argument policies of the tool-input surface, read once for every
    // row: each row shows the ones that also gate its capability. Null while
    // they are not known; a read that failed is said once, above the rows.
    const [argumentPolicies, setArgumentPolicies] = useState<ArgumentPolicy[] | null>(null);
    const [readFailure, reads] = useReadFailures<'argumentPolicies'>();
    const loadArgumentPolicies = useCallback(async () => {
        try {
            const answer = await listArgumentPolicies({ surface: 'mcp_input' });
            setArgumentPolicies(answer?.policies ?? []);
            reads.loaded('argumentPolicies');
        } catch (err) {
            // No badge would say that no argument policy gates a capability.
            setArgumentPolicies(null);
            reads.lost('argumentPolicies', 'Failed to load the argument policies that also gate these capabilities', err);
        }
    }, [listArgumentPolicies, reads]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadArgumentPolicies(), 'argument policies');
        }
    }, [isOpen, loadArgumentPolicies]);

    const handleAccessChange = (capType: CapabilityType, capName: string, newAccess: CapabilityAccess) => {
        setCapabilities(prev =>
            prev.map(c => {
                if (c.capability_type !== capType || c.capability_name !== capName) return c;
                // Cannot relax below inherited access level
                if (c.inherited_access && ACCESS_STRICTNESS[newAccess] < ACCESS_STRICTNESS[c.inherited_access]) return c;
                return { ...c, access: newAccess };
            })
        );
    };

    const handleTimeoutChange = (capType: CapabilityType, capName: string, timeout: number | undefined) => {
        setCapabilities(prev =>
            prev.map(c => {
                if (c.capability_type !== capType || c.capability_name !== capName) return c;
                return { ...c, approval_timeout_seconds: timeout };
            })
        );
    };

    const handleSetAllAccess = (capType: CapabilityType, access: CapabilityAccess) => {
        setCapabilities(prev =>
            prev.map(c => {
                if (c.capability_type !== capType) return c;
                if (c.inherited_access && ACCESS_STRICTNESS[access] < ACCESS_STRICTNESS[c.inherited_access]) return c;
                return { ...c, access };
            })
        );
    };

    const handleAddCapability = () => {
        const name = newCapName.trim();
        if (!name) return;

        if (capabilities.some(c => c.capability_type === activeTab && c.capability_name === name)) {
            toast.error(`Capability "${name}" already exists`);
            return;
        }

        setCapabilities(prev => [
            ...prev,
            { capability_type: activeTab, capability_name: name, access: 'enabled' }
        ]);
        setNewCapName('');
    };

    const handleRemoveCapability = (capType: CapabilityType, capName: string) => {
        setCapabilities(prev =>
            prev.filter(c => !(c.capability_type === capType && c.capability_name === capName))
        );
    };

    const handleSave = async () => {
        try {
            setSaving(true);
            setError('');

            const hasAnyNonEnabled = capabilities.some(c => c.access !== 'enabled');
            if (!hasAnyNonEnabled) {
                await clearMCPCapabilityFilters(groupId, server.id);
                setHasFilters(false);
                toast.success('All capabilities enabled — filters cleared');
            } else {
                const response = await updateMCPCapabilityFilters(groupId, server.id, capabilities);
                setHasFilters(response.has_filters);
                const enabled = capabilities.filter(c => c.access === 'enabled').length;
                const approval = capabilities.filter(c => c.access === 'approval_required').length;
                const disabled = capabilities.filter(c => c.access === 'disabled').length;
                const parts = [];
                if (enabled) parts.push(`${enabled} enabled`);
                if (approval) parts.push(`${approval} approval`);
                if (disabled) parts.push(`${disabled} disabled`);
                toast.success(`Capability filters saved (${parts.join(', ')})`);
            }
            onClose();
        } catch (err) {
            console.error('Failed to save capability filters:', errorKind(err));
            setError(errorMessage(err, 'Failed to save capability filters'));
        } finally {
            setSaving(false);
        }
    };

    const handleClearAll = () => {
        setCapabilities(prev => prev.map(c => {
            if (c.inherited_access && ACCESS_STRICTNESS['enabled'] < ACCESS_STRICTNESS[c.inherited_access]) return c;
            return { ...c, access: 'enabled' };
        }));
    };

    if (!isOpen) return null;

    // A held capability's timeout the Control Plane would refuse: nothing is saved while one stands.
    const timeoutOutOfRange = capabilities.some(c => c.access === 'approval_required' && approvalTimeoutProblem(c.approval_timeout_seconds) !== null);
    const currentCaps = capabilities.filter(c => c.capability_type === activeTab);
    const inheritedCount = currentCaps.filter(c => c.inherited_access).length;
    const enabledCount = currentCaps.filter(c => c.access === 'enabled').length;
    const approvalCount = currentCaps.filter(c => c.access === 'approval_required').length;
    const disabledCount = currentCaps.filter(c => c.access === 'disabled').length;
    const totalCount = currentCaps.length;

    const discoveredForTab = discoveredCaps.filter(d => d.capability_type === activeTab);
    const discoveredDescMap = new Map(discoveredForTab.map(d => [d.name, d.description]));

    const tabColors: Record<string, { bg: string; text: string }> = {
        amber: { bg: 'bg-surface-alt', text: 'text-amber-700' },
        green: { bg: 'bg-surface-alt', text: 'text-green-700' },
        purple: { bg: 'bg-surface-alt', text: 'text-secondary' },
        blue: { bg: 'bg-primary-pale', text: 'text-primary' },
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[85vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <div className="p-2 bg-primary-100 rounded-lg">
                            <Settings2 className="w-5 h-5 text-primary-600" />
                        </div>
                        <div>
                            <h2 className="text-lg font-semibold text-default">
                                Capability Filters
                            </h2>
                            <p className="text-sm text-muted">
                                {server.name} &middot; {groupName}
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-6 space-y-4">
                    {error && (
                        <div className="flex items-start space-x-3 p-3 bg-surface-alt border border-default rounded-lg">
                            <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
                            <p className="text-sm text-red-700">{error}</p>
                        </div>
                    )}

                    {loading ? (
                        <div className="flex items-center justify-center h-48">
                            <Loader className="w-8 h-8 text-primary-600 animate-spin" />
                        </div>
                    ) : !hasDiscovered ? (
                        <div className="space-y-4">
                            <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                                <AlertTriangle className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
                                <div className="text-sm text-amber-800">
                                    <p className="font-semibold mb-2">No capabilities discovered yet</p>
                                    <p>
                                        This MCP server&apos;s capabilities (tools, resources, prompts, templates) have not been discovered yet.
                                        Capability filters require discovered capabilities to configure.
                                    </p>
                                </div>
                            </div>

                            <div className="p-4 bg-surface-alt border border-default rounded-lg space-y-3">
                                <h3 className="text-sm font-semibold text-default">How to discover capabilities</h3>
                                {isOAuth ? (
                                    <div className="space-y-2 text-sm text-secondary">
                                        <p>
                                            This server requires <strong>OAuth authentication</strong>. Capabilities are automatically
                                            discovered when a user connects to the server for the first time via the User Portal.
                                        </p>
                                        <ol className="list-decimal list-inside space-y-1 ml-2">
                                            <li>A user connects to <strong>{server.name}</strong> in the User Portal</li>
                                            <li>They complete the OAuth login flow with the identity provider</li>
                                            <li>The proxy discovers tools, resources, and prompts using the access token</li>
                                            <li>Come back here to configure capability filters</li>
                                        </ol>
                                    </div>
                                ) : (
                                    <div className="space-y-2 text-sm text-secondary">
                                        <p>
                                            For non-OAuth servers, capabilities are normally discovered when the server is added to the system.
                                            If discovery failed or the server was offline:
                                        </p>
                                        <ol className="list-decimal list-inside space-y-1 ml-2">
                                            <li>Ensure the MCP server is running and reachable</li>
                                            <li>Go to <strong>Resources &rarr; MCP Servers</strong> and edit the server</li>
                                            <li>Use <strong>Test Connection</strong> to verify connectivity</li>
                                            <li>Re-save the server to trigger capability discovery</li>
                                            <li>Come back here to configure capability filters</li>
                                        </ol>
                                    </div>
                                )}
                            </div>

                            <div className="flex items-start space-x-3 p-3 bg-primary-pale border border-primary rounded-lg">
                                <Info className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                <div className="text-sm text-primary">
                                    You can still manually add capability names below if you know them,
                                    but using discovered capabilities is recommended.
                                </div>
                            </div>
                        </div>
                    ) : (
                        <>
                            {/* Governance inheritance banner */}
                            {capabilities.some(c => c.inherited_access) && (
                                <div className="flex items-start space-x-3 p-3 bg-primary-pale border border-primary rounded-lg">
                                    <Shield className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                    <div className="text-sm text-primary">
                                        Capabilities marked with <Lock className="w-3 h-3 inline" /> have restrictions inherited from a parent group.
                                        You cannot relax them below the inherited access level.
                                    </div>
                                </div>
                            )}
                            {/* Access level legend */}
                            {!hasFilters && !capabilities.some(c => c.inherited_access) && (
                                <div className="flex items-start space-x-3 p-3 bg-primary-pale border border-primary rounded-lg">
                                    <Info className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                    <div className="text-sm text-primary">
                                        <strong>No filters active.</strong> All capabilities are currently enabled.
                                        Use the three-state controls: <strong>E</strong> = Enabled,{' '}
                                        <strong>A</strong> = Approval Required,{' '}
                                        <strong>D</strong> = Disabled.
                                    </div>
                                </div>
                            )}
                        </>
                    )}

                    {/* Tabs + capability list */}
                    {!loading && (
                        <>
                            <ReadFailureNotice
                                failure={readFailure('argumentPolicies')}
                                onRetry={() => runBackground(loadArgumentPolicies(), 'argument policies')}
                                data-testid="argument-policies-failed"
                            />
                            {/* Capability type tabs */}
                            <div className="flex space-x-1 bg-surface-raised rounded-lg p-1">
                                {CAPABILITY_TABS.map(tab => {
                                    const Icon = tab.icon;
                                    const tabCaps = capabilities.filter(c => c.capability_type === tab.type);
                                    const count = tabCaps.length;
                                    const tabDisabled = tabCaps.filter(c => c.access === 'disabled').length;
                                    const tabApproval = tabCaps.filter(c => c.access === 'approval_required').length;
                                    const tabLocked = tabCaps.filter(c => c.inherited_access).length;
                                    const colors = tabColors[tab.color];
                                    const isActive = activeTab === tab.type;

                                    let badgeClass = isActive ? colors.bg : 'bg-surface-inset';
                                    if (tabLocked > 0) badgeClass = 'bg-primary-pale text-primary';
                                    else if (tabDisabled > 0) badgeClass = 'bg-surface-alt text-red-700';
                                    else if (tabApproval > 0) badgeClass = 'bg-surface-alt text-amber-700';

                                    const enabledInTab = count - tabDisabled - tabApproval;

                                    return (
                                        <button
                                            key={tab.type}
                                            onClick={() => setActiveTab(tab.type)}
                                            className={`flex items-center space-x-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors flex-1 justify-center ${
                                                isActive
                                                    ? `bg-surface shadow-sm ${colors.text}`
                                                    : `text-muted hover:text-secondary`
                                            }`}
                                        >
                                            <Icon className="w-3.5 h-3.5" />
                                            <span>{tab.label}</span>
                                            {count > 0 && (
                                                <span className={`text-xs px-1.5 py-0.5 rounded-full ${badgeClass}`}>
                                                    {(tabDisabled > 0 || tabApproval > 0) ? `${enabledInTab}/${count}` : count}
                                                </span>
                                            )}
                                        </button>
                                    );
                                })}
                            </div>

                            {/* Bulk actions */}
                            {currentCaps.length > 0 && (
                                <div className="flex items-center justify-between">
                                    <span className="text-sm text-muted">
                                        {enabledCount} enabled
                                        {approvalCount > 0 && <span className="text-amber-600"> / {approvalCount} approval</span>}
                                        {disabledCount > 0 && <span className="text-red-600"> / {disabledCount} disabled</span>}
                                        {' '}of {totalCount}
                                        {inheritedCount > 0 && (
                                            <span className="ml-1 text-primary">
                                                ({inheritedCount} inherited)
                                            </span>
                                        )}
                                    </span>
                                    <div className="flex items-center space-x-2">
                                        <button
                                            onClick={() => handleSetAllAccess(activeTab, 'enabled')}
                                            className="flex items-center space-x-1 px-2.5 py-1.5 text-xs font-medium text-green-700 bg-surface-alt border border-default rounded-md hover:bg-surface-alt transition-colors"
                                        >
                                            <ToggleRight className="w-3.5 h-3.5" />
                                            <span>Enable All</span>
                                        </button>
                                        <button
                                            onClick={() => handleSetAllAccess(activeTab, 'disabled')}
                                            className="flex items-center space-x-1 px-2.5 py-1.5 text-xs font-medium text-secondary bg-surface-alt border border-default rounded-md hover:bg-surface-raised transition-colors"
                                        >
                                            <ToggleLeft className="w-3.5 h-3.5" />
                                            <span>Disable All</span>
                                        </button>
                                    </div>
                                </div>
                            )}

                            {/* Add capability input (manual entry) */}
                            <div className="flex items-center space-x-2">
                                <input
                                    type="text"
                                    value={newCapName}
                                    onChange={(e) => setNewCapName(e.target.value)}
                                    onKeyDown={(e) => e.key === 'Enter' && handleAddCapability()}
                                    placeholder={`Add ${activeTab === 'resource_template' ? 'template URI' : activeTab === 'resource' ? 'resource URI' : `${activeTab} name`} manually...`}
                                    className="flex-1 px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent min-h-0 min-w-0"
                                />
                                <button
                                    onClick={handleAddCapability}
                                    disabled={!newCapName.trim()}
                                    className="px-3 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    Add
                                </button>
                            </div>

                            {/* Capability list */}
                            {currentCaps.length === 0 ? (
                                <div className="text-center py-8 text-muted text-sm">
                                    No {CAPABILITY_TABS.find(t => t.type === activeTab)?.label.toLowerCase()} discovered or configured.
                                </div>
                            ) : (
                                <div className="space-y-1 max-h-64 overflow-y-auto">
                                    {currentCaps.map((cap) => {
                                        const description = discoveredDescMap.get(cap.capability_name);
                                        const isInherited = !!cap.inherited_access;
                                        const rowBg =
                                            cap.access === 'disabled'
                                                ? 'bg-surface-alt'
                                                : cap.access === 'approval_required'
                                                    ? 'bg-surface-alt/50'
                                                    : 'bg-surface-alt';

                                        return (
                                            <div
                                                key={`${cap.capability_type}-${cap.capability_name}`}
                                                className={`flex items-center justify-between px-3 py-2 rounded-lg group ${
                                                    isInherited ? 'border border-primary ' + rowBg : rowBg
                                                }`}
                                            >
                                                <div className="flex-1 min-w-0 min-h-0">
                                                    <div className="flex items-center gap-1.5">
                                                        <span className={`text-sm font-mono truncate ${
                                                            cap.access === 'disabled'
                                                                ? 'text-red-700 line-through'
                                                                : cap.access === 'approval_required'
                                                                    ? 'text-amber-800'
                                                                    : 'text-default'
                                                        }`}>
                                                            {cap.capability_name}
                                                        </span>
                                                        {/* An operator setting an
                                                            unconditional gate must be able to see
                                                            the conditional rules that also apply,
                                                            or the two surfaces disagree silently. */}
                                                        <ArgumentPolicyGateBadge
                                                            capabilityName={cap.capability_name}
                                                            groupId={groupId}
                                                            policies={argumentPolicies}
                                                        />
                                                        {isInherited && (
                                                            <span
                                                                className="inline-flex items-center gap-0.5 text-xs text-primary"
                                                                title={cap.source_group_name
                                                                    ? `${cap.inherited_access?.replace('_', ' ')} by ${cap.source_group_name}`
                                                                    : `${cap.inherited_access?.replace('_', ' ')} by parent group`}
                                                            >
                                                                <Lock className="w-3 h-3" />
                                                                {cap.source_group_name && <span>{cap.source_group_name}</span>}
                                                            </span>
                                                        )}
                                                        {cap.access === 'approval_required' && (
                                                            <span title="Requires approval"><Clock className="w-3 h-3 text-amber-500" /></span>
                                                        )}
                                                    </div>
                                                    {description && (
                                                        <span className="text-xs text-muted truncate block mt-0.5">
                                                            {description}
                                                        </span>
                                                    )}
                                                    {/* Timeout input for approval_required */}
                                                    {cap.access === 'approval_required' && (
                                                        <div className="flex items-center gap-1.5 mt-1">
                                                            <Clock className="w-3 h-3 text-amber-500" />
                                                            <input
                                                                type="number"
                                                                min={APPROVAL_TIMEOUT_MIN_SECONDS}
                                                                max={APPROVAL_TIMEOUT_MAX_SECONDS}
                                                                placeholder="300"
                                                                value={cap.approval_timeout_seconds ?? ''}
                                                                onChange={(e) => {
                                                                    const val = e.target.value ? parseInt(e.target.value, 10) : undefined;
                                                                    handleTimeoutChange(cap.capability_type, cap.capability_name, val);
                                                                }}
                                                                className="w-20 px-1.5 py-0.5 text-xs border border-default rounded focus:ring-1 focus:ring-amber-400 focus:border-transparent"
                                                            />
                                                            <span className="text-xs text-muted">sec timeout</span>
                                                        </div>
                                                    )}
                                                    {cap.access === 'approval_required' && approvalTimeoutProblem(cap.approval_timeout_seconds) && (
                                                        <p role="alert" className="text-xs text-red-600 mt-1">{approvalTimeoutProblem(cap.approval_timeout_seconds)}</p>
                                                    )}
                                                </div>
                                                <div className="flex items-center space-x-2 ml-3">
                                                    <AccessControl
                                                        access={cap.access}
                                                        minAccess={cap.inherited_access}
                                                        onChange={(newAccess) => handleAccessChange(cap.capability_type, cap.capability_name, newAccess)}
                                                    />
                                                    {!isInherited && (
                                                        <button
                                                            onClick={() => handleRemoveCapability(cap.capability_type, cap.capability_name)}
                                                            className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center text-disabled hover:text-red-500 hover:bg-surface-alt transition-colors opacity-0 group-hover:opacity-100"
                                                            title="Remove"
                                                        >
                                                            <X className="w-3.5 h-3.5" />
                                                        </button>
                                                    )}
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </>
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between p-6 border-t border-default bg-surface-alt">
                    <div>
                        {capabilities.some(c => c.access !== 'enabled') && (
                            <button
                                onClick={handleClearAll}
                                className="text-sm text-muted hover:text-green-600 transition-colors"
                            >
                                Enable all (clear filters)
                            </button>
                        )}
                    </div>
                    <div className="flex items-center space-x-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleSave, 'click')}
                            disabled={saving || loading || timeoutOutOfRange}
                            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50"
                        >
                            {saving ? (
                                <span className="flex items-center space-x-2">
                                    <Loader className="w-4 h-4 animate-spin" />
                                    <span>Saving...</span>
                                </span>
                            ) : (
                                'Save Filters'
                            )}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default MCPCapabilityFilterDialog;
