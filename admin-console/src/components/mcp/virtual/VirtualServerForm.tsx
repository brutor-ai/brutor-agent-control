/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useCallback, useEffect } from 'react';
import {
    X,
    AlertCircle,
    CheckCircle,
    Plus,
    Trash2,
    Server,
    Settings,
    Info,
    UserCheck,
    Loader,
    Search,
    Check
} from 'lucide-react';
import { VirtualServerConfig, MCPServerConfig } from '../../../lib/types.ts';
import { useAuth } from '../../../hooks/useAuth.tsx';
import { useGateway } from '../../../hooks/useGateway.ts';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { errorKind } from '../../../lib/narrow';

interface VirtualServerFormProps {
    virtualServer: VirtualServerConfig | null;
    availableServers: MCPServerConfig[];
    onSave: (formData: Omit<VirtualServerConfig, 'created_at'>) => Promise<void>;
    onCancel: () => void;
}

interface FormErrors {
    id?: string;
    name?: string;
    capabilities?: string;
    owner?: string;
}

interface CapabilityMapping {
    server_id: string;
    capability_name: string;
    capability_type: 'tools' | 'resources' | 'prompts';
    // Operator overrides. Apply only to tools today;
    // resources/prompts can adopt the same shape in a later sprint.
    exposed_name?: string;
    description_override?: string;
}

const VirtualServerForm: React.FC<VirtualServerFormProps> = ({
                                                                 virtualServer,
                                                                 availableServers,
                                                                 onSave,
                                                                 onCancel
                                                             }) => {
    const { user: currentUser, hasPermission } = useAuth();
    const {
        users,
        usersLoaded,
        loadingUsers,
        fetchUsers,
        getUsernameById,
        getUserDisplayName
    } = useGateway();

    const [formData, setFormData] = useState<{
        id?: string;
        name: string;
        description: string;
        owner: string;
        enabled: boolean;
    }>({
        id: '',
        name: '',
        description: '',
        owner: currentUser?.id || '',
        enabled: true
    });

    const [showOwnerChange, setShowOwnerChange] = useState(false);
    const [capabilityMappings, setCapabilityMappings] = useState<CapabilityMapping[]>([]);
    const [errors, setErrors] = useState<FormErrors>({});
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [selectedServer, setSelectedServer] = useState<string>('');
    const [selectedCapabilityType, setSelectedCapabilityType] = useState<'tools' | 'resources' | 'prompts'>('tools');
    const [searchTerm, setSearchTerm] = useState('');
    // VMCP-wide tool prefix (e.g. "acme_" → exposed
    // tool names become "acme_search"). Applied by the backend
    // normalizer to any tool entry that doesn't have an explicit
    // exposed_name.
    const [toolNamePrefix, setToolNamePrefix] = useState<string>('');
    // Which tool mapping (index) is currently being customised
    // (rename / description override). One at a time keeps the UI
    // calm for VMCPs with many tools.
    const [editingMappingIdx, setEditingMappingIdx] = useState<number | null>(null);

    // The Control Plane takes a new owner with any update of the virtual
    // server (PATCH, `virtual:server:update`). No role name is asked: a
    // system administrator answers `roles: ['super_admin']`, not a role name.
    const canChangeOwner = useCallback((): boolean => {
        if (!currentUser) return false;
        return hasPermission('virtual:server:update');
    }, [currentUser, hasPermission]);

    useEffect(() => {
        if (canChangeOwner() && showOwnerChange && !usersLoaded && users.length === 0) {
            runBackground(fetchUsers(), 'users');
        }
    }, [showOwnerChange, usersLoaded, users.length, fetchUsers, canChangeOwner]);

    const extractCapabilityNames = (capabilities: any[], type: string): string[] => {
        if (!capabilities || !Array.isArray(capabilities)) {
            return [];
        }

        const extracted = capabilities.map(cap => {
            if (typeof cap === 'string') {
                return cap;
            }

            if (typeof cap === 'object' && cap !== null) {
                let capName = '';
                switch (type) {
                    case 'tools':
                        capName = cap.name || cap.capability_name || '';
                        break;
                    case 'resources':
                        capName = cap.uri || cap.capability_name || '';
                        break;
                    case 'prompts':
                        capName = cap.name || cap.capability_name || '';
                        break;
                    default:
                        capName = cap.name || cap.capability_name || '';
                }
                return capName;
            }

            return '';
        }).filter(Boolean);

        return extracted;
    };

    useEffect(() => {
        if (virtualServer) {
            setFormData({
                id: virtualServer.id,
                name: virtualServer.name,
                description: virtualServer.description,
                owner: virtualServer.owner,
                enabled: virtualServer.enabled
            });

            const mappings: CapabilityMapping[] = [];
            if (virtualServer.capabilities && typeof virtualServer.capabilities === 'object') {
                // Preload tool_name_prefix from the normalized blob.
                const prefix = (virtualServer.capabilities as Record<string, unknown>).tool_name_prefix;
                if (typeof prefix === 'string') {
                    setToolNamePrefix(prefix);
                } else {
                    setToolNamePrefix('');
                }

                // Walk known capability types ONLY — the normalized
                // blob also carries source_mapping / reverse_name_mapping
                // / tool_name_prefix, which aren't capability lists.
                (['tools', 'resources', 'prompts'] as const).forEach(type => {
                    const capabilities = (virtualServer.capabilities as Record<string, unknown>)[type];
                    if (Array.isArray(capabilities)) {
                        capabilities.forEach((cap: unknown) => {
                            if (typeof cap === 'object' && cap !== null && 'server_id' in cap && 'capability_name' in cap) {
                                const c = cap as Record<string, unknown>;
                                mappings.push({
                                    server_id: c.server_id as string,
                                    capability_name: c.capability_name as string,
                                    capability_type: type,
                                    exposed_name: typeof c.exposed_name === 'string' && c.exposed_name !== c.capability_name
                                        ? (c.exposed_name) : undefined,
                                    description_override: typeof c.description_override === 'string'
                                        ? (c.description_override) : undefined,
                                });
                            } else {
                                console.warn(`Invalid capability format in the virtual server: ${type}`);
                            }
                        });
                    }
                });
            }

            setCapabilityMappings(mappings);
        } else {
            setFormData({
                id: '',
                name: '',
                description: '',
                owner: currentUser?.id || '',
                enabled: true
            });
            setCapabilityMappings([]);
            setToolNamePrefix('');
        }
        setErrors({});
        setShowOwnerChange(false);
        setSelectedServer('');
        setSearchTerm('');
        setEditingMappingIdx(null);
    }, [virtualServer, currentUser]);

    const validateForm = (): boolean => {
        const newErrors: FormErrors = {};

        // Only validate ID when editing (ID is auto-generated for new servers)
        if (virtualServer && !formData.id?.trim()) {
            newErrors.id = 'Virtual server ID is required';
        } else if (virtualServer && formData.id && !/^[a-zA-Z0-9_-]+$/.test(formData.id)) {
            newErrors.id = 'ID can only contain letters, numbers, hyphens, and underscores';
        }

        if (!formData.name.trim()) {
            newErrors.name = 'Virtual server name is required';
        }

        if (capabilityMappings.length === 0) {
            newErrors.capabilities = 'At least one capability mapping is required';
        }

        if (!formData.owner) {
            newErrors.owner = 'Owner is required';
        }

        setErrors(newErrors);
        return Object.keys(newErrors).length === 0;
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();

        if (!validateForm()) {
            return;
        }

        setIsSubmitting(true);
        try {
            // Build the capabilities payload including the
            // tool overrides. The backend normalizer handles the
            // computed source_mapping / reverse_name_mapping;
            // we ship intent only.
            type ToolEntry = {
                server_id: string;
                capability_name: string;
                exposed_name?: string;
                description_override?: string;
            };
            type BasicEntry = { server_id: string; capability_name: string };
            const capabilitiesPayload: {
                tools: ToolEntry[];
                resources: BasicEntry[];
                prompts: BasicEntry[];
                tool_name_prefix?: string;
            } = { tools: [], resources: [], prompts: [] };

            capabilityMappings.forEach(mapping => {
                if (mapping.capability_type === 'tools') {
                    const entry: ToolEntry = {
                        server_id: mapping.server_id,
                        capability_name: mapping.capability_name,
                    };
                    const exposed = (mapping.exposed_name ?? '').trim();
                    if (exposed && exposed !== mapping.capability_name) {
                        entry.exposed_name = exposed;
                    }
                    const desc = (mapping.description_override ?? '').trim();
                    if (desc) {
                        entry.description_override = desc;
                    }
                    capabilitiesPayload.tools.push(entry);
                } else {
                    capabilitiesPayload[mapping.capability_type].push({
                        server_id: mapping.server_id,
                        capability_name: mapping.capability_name,
                    });
                }
            });

            const trimmedPrefix = toolNamePrefix.trim();
            if (trimmedPrefix) {
                capabilitiesPayload.tool_name_prefix = trimmedPrefix;
            }

            // Don't include ID for new servers (auto-generated by backend)
            const virtualServerData: Omit<VirtualServerConfig, 'created_at'> = virtualServer
                ? { ...formData, capabilities: capabilitiesPayload }
                : {
                    name: formData.name,
                    description: formData.description,
                    enabled: formData.enabled,
                    owner: formData.owner,
                    capabilities: capabilitiesPayload,
                };

            await onSave(virtualServerData);
        } catch (error) {
            console.error('Failed to save virtual server:', errorKind(error));
            setIsSubmitting(false);
        }
    };

    const handleInputChange = (field: keyof typeof formData, value: any) => {
        setFormData(prev => ({ ...prev, [field]: value }));

        if (errors[field as keyof FormErrors]) {
            setErrors(prev => ({ ...prev, [field]: undefined }));
        }
    };

    const handleOwnerChange = (newOwnerId: string) => {
        handleInputChange('owner', newOwnerId);
        setShowOwnerChange(false);
    };

    const getAvailableCapabilities = (): string[] => {
        if (!selectedServer) return [];
        const server = availableServers.find(s => s.id === selectedServer);
        if (!server?.capabilities?.[selectedCapabilityType]) return [];
        return extractCapabilityNames(server.capabilities[selectedCapabilityType], selectedCapabilityType);
    };

    const isCapabilityAdded = (serverId: string, capName: string, type: string): boolean => {
        return capabilityMappings.some(
            m => m.server_id === serverId && m.capability_name === capName && m.capability_type === type
        );
    };

    const addCapability = (serverId: string, capName: string, type: 'tools' | 'resources' | 'prompts') => {
        if (isCapabilityAdded(serverId, capName, type)) {
            return;
        }

        const newMapping = {
            server_id: serverId,
            capability_name: capName,
            capability_type: type
        };

        setCapabilityMappings(prev => [...prev, newMapping]);

        if (errors.capabilities) {
            setErrors(prev => ({ ...prev, capabilities: undefined }));
        }
    };

    const addAllCapabilities = () => {
        if (!selectedServer) {
            return;
        }

        const newMappings = getAvailableCapabilities()
            .filter(capName => !isCapabilityAdded(selectedServer, capName, selectedCapabilityType))
            .map(capName => ({
                server_id: selectedServer,
                capability_name: capName,
                capability_type: selectedCapabilityType
            }));

        if (newMappings.length > 0) {
            setCapabilityMappings(prev => [...prev, ...newMappings]);

            if (errors.capabilities) {
                setErrors(prev => ({ ...prev, capabilities: undefined }));
            }
        }
    };

    const removeCapability = (index: number) => {
        setCapabilityMappings(prev => prev.filter((_, i) => i !== index));
        if (editingMappingIdx === index) setEditingMappingIdx(null);
    };

    // Update one mapping in place. Used by the per-tool
    // editor when the operator types into exposed_name or
    // description_override.
    const updateMapping = (
        index: number,
        patch: Partial<CapabilityMapping>,
    ) => {
        setCapabilityMappings(prev =>
            prev.map((m, i) => (i === index ? { ...m, ...patch } : m)),
        );
    };

    // Compute the effective exposed name a tool will get after the
    // backend normalizer runs — explicit > prefix+name > name.
    // Surfaced live in the UI so operators see name collisions
    // before they save.
    const effectiveExposedName = (mapping: CapabilityMapping): string => {
        const explicit = (mapping.exposed_name ?? '').trim();
        if (explicit) return explicit;
        const prefix = toolNamePrefix.trim();
        if (mapping.capability_type === 'tools' && prefix) {
            return `${prefix}${mapping.capability_name}`;
        }
        return mapping.capability_name;
    };

    // Pre-compute exposed-name collisions for tools so we can flag
    // them inline (the backend normalizer rejects on save; this
    // catches it earlier).
    const exposedNameCollisions: Set<string> = (() => {
        const seen = new Map<string, number>();
        const collisions = new Set<string>();
        capabilityMappings.forEach(m => {
            if (m.capability_type !== 'tools') return;
            const name = effectiveExposedName(m);
            const count = (seen.get(name) ?? 0) + 1;
            seen.set(name, count);
            if (count > 1) collisions.add(name);
        });
        return collisions;
    })();

    const removeAllFromServer = (serverId: string, type: string) => {
        setCapabilityMappings(prev =>
            prev.filter(m => !(m.server_id === serverId && m.capability_type === type))
        );
    };

    const getServerName = (serverId: string) => {
        const server = availableServers.find(s => s.id === serverId);
        return server ? server.name : serverId;
    };

    const getCapabilityTypeIcon = (type: string) => {
        switch (type) {
            case 'tools':
                return <Settings className="w-4 h-4" />;
            case 'resources':
                return <Server className="w-4 h-4" />;
            case 'prompts':
                return <Info className="w-4 h-4" />;
            default:
                return <Settings className="w-4 h-4" />;
        }
    };

    const getCapabilityTypeColor = (type: string) => {
        switch (type) {
            case 'tools':
                return 'bg-blue-100 text-blue-800 border-blue-200';
            case 'resources':
                return 'bg-green-100 text-green-800 border-green-200';
            case 'prompts':
                return 'bg-purple-100 text-purple-800 border-purple-200';
            default:
                return 'bg-surface-raised text-default border-default';
        }
    };

    const groupCapabilitiesByServer = (type: string) => {
        const caps = capabilityMappings.filter(m => m.capability_type === type);
        const grouped: Record<string, CapabilityMapping[]> = {};
        caps.forEach(cap => {
            if (!grouped[cap.server_id]) {
                grouped[cap.server_id] = [];
            }
            grouped[cap.server_id].push(cap);
        });
        return grouped;
    };

    const getTotalCapabilities = () => {
        return capabilityMappings.length;
    };

    const getCapabilitiesByType = (type: string) => {
        return capabilityMappings.filter(m => m.capability_type === type).length;
    };

    const enabledServersWithCapabilities = availableServers.filter(s =>
        s.enabled && s.capabilities &&
        Object.values(s.capabilities).some(caps => caps && caps.length > 0)
    );

    const availableCaps = getAvailableCapabilities();
    const filteredCaps = availableCaps.filter(cap =>
        cap.toLowerCase().includes(searchTerm.toLowerCase())
    );

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-6xl w-full max-h-[90vh] overflow-y-auto">
                <div>
                    <div className="flex items-center justify-between px-6 py-4 border-b bg-surface-alt sticky top-0 z-10">
                        <div>
                            <h3 className="text-lg font-semibold text-default">
                                {virtualServer ? 'Edit Virtual Server' : 'Create Virtual Server'}
                            </h3>
                            <p className="text-sm text-secondary mt-1">
                                Combine capabilities from multiple MCP servers into a single virtual endpoint
                            </p>
                        </div>
                        <button
                            type="button"
                            onClick={onCancel}
                            className="p-2 hover:bg-surface-inset rounded-md transition-colors"
                        >
                            <X className="w-5 h-5 text-muted" />
                        </button>
                    </div>

                    <div className="p-6">
                        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                            <div className="space-y-6">
                                <div>
                                    <h4 className="text-sm font-semibold text-default uppercase tracking-wider border-b pb-2 mb-4">
                                        Basic Information
                                    </h4>

                                    <div className="space-y-4">
                                        {/* Only show ID field when editing (auto-generated for new servers) */}
                                        {virtualServer && (
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Virtual Server ID
                                                </label>
                                                <input
                                                    type="text"
                                                    value={formData.id}
                                                    className="w-full px-3 py-2 border border-strong rounded-md bg-surface-alt text-muted text-sm cursor-not-allowed"
                                                    disabled={true}
                                                    readOnly={true}
                                                />
                                                <p className="mt-1 text-xs text-muted">
                                                    ID cannot be changed after creation
                                                </p>
                                            </div>
                                        )}

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Display Name *
                                            </label>
                                            <input
                                                type="text"
                                                value={formData.name}
                                                onChange={(e) => handleInputChange('name', e.target.value)}
                                                className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm ${
                                                    errors.name ? 'border-red-300 focus:ring-red-500' : 'border-strong'
                                                }`}
                                                placeholder="e.g., Productivity Assistant"
                                            />
                                            {errors.name && (
                                                <div className="flex items-center mt-1 text-sm text-red-600">
                                                    <AlertCircle className="w-4 h-4 mr-1" />
                                                    {errors.name}
                                                </div>
                                            )}
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Description
                                            </label>
                                            <textarea
                                                value={formData.description}
                                                onChange={(e) => handleInputChange('description', e.target.value)}
                                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                                                rows={2}
                                                placeholder="Describe what this virtual server combines and provides..."
                                            />
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Tool name prefix
                                            </label>
                                            <input
                                                type="text"
                                                value={toolNamePrefix}
                                                onChange={(e) => setToolNamePrefix(e.target.value)}
                                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm font-mono"
                                                placeholder="e.g., acme_   (applied to every tool without an explicit rename)"
                                            />
                                            <p className="mt-1 text-xs text-muted">
                                                Helps avoid name collisions when stitching tools from multiple upstreams.
                                                Explicit per-tool renames below override this prefix.
                                            </p>
                                        </div>

                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Owner
                                                </label>
                                                {!virtualServer || !canChangeOwner() ? (
                                                    <div className="flex items-center space-x-2 px-3 py-2 bg-surface-alt border border-strong rounded-md">
                                                        <UserCheck className="w-4 h-4 text-muted" />
                                                        <span className="text-sm text-secondary">
                                                            {getUserDisplayName(formData.owner)} ({getUsernameById(formData.owner)})
                                                        </span>
                                                        {!virtualServer && (
                                                            <span className="text-xs text-muted">(You)</span>
                                                        )}
                                                    </div>
                                                ) : (
                                                    <div className="space-y-2">
                                                        <div className="flex items-center justify-between px-3 py-2 bg-surface-alt border border-strong rounded-md">
                                                            <div className="flex items-center space-x-2">
                                                                <UserCheck className="w-4 h-4 text-muted" />
                                                                <span className="text-sm text-secondary">
                                                                    {getUserDisplayName(formData.owner)} ({getUsernameById(formData.owner)})
                                                                </span>
                                                            </div>
                                                            {!showOwnerChange && (
                                                                <button
                                                                    type="button"
                                                                    onClick={() => setShowOwnerChange(true)}
                                                                    className="text-xs text-blue-600 hover:text-blue-800 underline transition-colors"
                                                                >
                                                                    Change Owner
                                                                </button>
                                                            )}
                                                        </div>

                                                        {showOwnerChange && (
                                                            <div className="space-y-2">
                                                                {loadingUsers ? (
                                                                    <div className="flex items-center space-x-2 text-sm text-muted">
                                                                        <Loader className="w-4 h-4 animate-spin" />
                                                                        <span>Loading users...</span>
                                                                    </div>
                                                                ) : (
                                                                    <div className="space-y-1">
                                                                        <label className="text-xs text-secondary">Select new owner:</label>
                                                                        <select
                                                                            value={formData.owner}
                                                                            onChange={(e) => handleOwnerChange(e.target.value)}
                                                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                                                                        >
                                                                            {users.map(user => (
                                                                                <option key={user.id} value={user.id}>
                                                                                    {user.full_name || user.username} ({user.username})
                                                                                    {user.roles.includes('super_admin') && ' - Admin'}
                                                                                </option>
                                                                            ))}
                                                                        </select>
                                                                        <button
                                                                            type="button"
                                                                            onClick={() => setShowOwnerChange(false)}
                                                                            className="text-xs text-secondary hover:text-default underline"
                                                                        >
                                                                            Cancel
                                                                        </button>
                                                                    </div>
                                                                )}
                                                            </div>
                                                        )}
                                                    </div>
                                                )}
                                                {errors.owner && (
                                                    <div className="flex items-center mt-1 text-sm text-red-600">
                                                        <AlertCircle className="w-4 h-4 mr-1" />
                                                        {errors.owner}
                                                    </div>
                                                )}
                                            </div>

                                            <div className="flex items-center">
                                                <label className="flex items-center space-x-2 cursor-pointer">
                                                    <input
                                                        type="checkbox"
                                                        checked={formData.enabled}
                                                        onChange={(e) => handleInputChange('enabled', e.target.checked)}
                                                        className="rounded border-strong text-blue-600 focus:ring-blue-500"
                                                    />
                                                    <span className="text-sm font-medium text-secondary">Enable virtual server</span>
                                                </label>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                                    <h5 className="text-sm font-medium text-blue-900 mb-3">Capability Summary</h5>
                                    <div className="grid grid-cols-3 gap-4">
                                        {['tools', 'resources', 'prompts'].map(type => (
                                            <div key={type} className="text-center">
                                                <div className="text-2xl font-bold text-blue-900">
                                                    {getCapabilitiesByType(type)}
                                                </div>
                                                <div className="text-blue-700 capitalize text-xs mt-1">{type}</div>
                                            </div>
                                        ))}
                                    </div>
                                    <div className="mt-3 pt-3 border-t border-blue-200 text-center">
                                        <div className="text-sm text-blue-700">
                                            Total: <span className="font-semibold">{getTotalCapabilities()}</span> capabilities
                                        </div>
                                    </div>
                                    {errors.capabilities && (
                                        <div className="flex items-center mt-2 text-sm text-red-600">
                                            <AlertCircle className="w-4 h-4 mr-1" />
                                            {errors.capabilities}
                                        </div>
                                    )}
                                </div>

                                <div>
                                    <h4 className="text-sm font-semibold text-default uppercase tracking-wider border-b pb-2 mb-4">
                                        Selected Capabilities
                                    </h4>
                                    {['tools', 'resources', 'prompts'].map(type => {
                                        const groupedByServer = groupCapabilitiesByServer(type);
                                        const serverIds = Object.keys(groupedByServer);

                                        if (serverIds.length === 0) return null;

                                        return (
                                            <div key={type} className="mb-4">
                                                <div className={`flex items-center space-x-2 px-3 py-2 rounded-t border ${getCapabilityTypeColor(type)}`}>
                                                    {getCapabilityTypeIcon(type)}
                                                    <span className="font-medium capitalize">{type}</span>
                                                    <span className="text-sm">({getCapabilitiesByType(type)})</span>
                                                </div>
                                                <div className="border border-t-0 rounded-b divide-y">
                                                    {serverIds.map(serverId => (
                                                        <div key={serverId} className="p-3 bg-surface-alt">
                                                            <div className="flex items-center justify-between mb-2">
                                                                <span className="text-sm font-medium text-secondary">
                                                                    {getServerName(serverId)}
                                                                </span>
                                                                <button
                                                                    type="button"
                                                                    onClick={() => removeAllFromServer(serverId, type)}
                                                                    className="text-xs text-red-600 hover:text-red-800 underline"
                                                                >
                                                                    Remove all
                                                                </button>
                                                            </div>
                                                            <div className="space-y-1">
                                                                {groupedByServer[serverId].map((cap) => {
                                                                    const globalIdx = capabilityMappings.indexOf(cap);
                                                                    const isEditing = editingMappingIdx === globalIdx;
                                                                    const exposed = effectiveExposedName(cap);
                                                                    const hasOverride =
                                                                        exposed !== cap.capability_name ||
                                                                        !!cap.description_override;
                                                                    const isCollision =
                                                                        cap.capability_type === 'tools' &&
                                                                        exposedNameCollisions.has(exposed);
                                                                    return (
                                                                        <div key={globalIdx} className={`bg-surface px-2 py-1 rounded text-sm border ${isCollision ? 'border-red-300' : 'border-transparent'}`}>
                                                                            <div className="flex items-center justify-between">
                                                                                <div className="flex items-center space-x-2 min-w-0 flex-1 min-h-0">
                                                                                    <span className="text-secondary font-mono truncate" title={cap.capability_name}>
                                                                                        {cap.capability_name}
                                                                                    </span>
                                                                                    {exposed !== cap.capability_name && (
                                                                                        <>
                                                                                            <span className="text-xs text-muted">→</span>
                                                                                            <span className="text-xs font-mono text-blue-700 truncate" title={`Exposed as: ${exposed}`}>
                                                                                                {exposed}
                                                                                            </span>
                                                                                        </>
                                                                                    )}
                                                                                    {hasOverride && !isCollision && (
                                                                                        <span className="text-[10px] uppercase tracking-wider px-1 py-0.5 bg-blue-100 text-blue-700 rounded">
                                                                                            customised
                                                                                        </span>
                                                                                    )}
                                                                                    {isCollision && (
                                                                                        <span className="text-[10px] uppercase tracking-wider px-1 py-0.5 bg-red-100 text-red-700 rounded" title="Two tools resolve to the same exposed name — save will be rejected">
                                                                                            collision
                                                                                        </span>
                                                                                    )}
                                                                                </div>
                                                                                <div className="flex items-center space-x-1">
                                                                                    {cap.capability_type === 'tools' && (
                                                                                        <button
                                                                                            type="button"
                                                                                            onClick={() => setEditingMappingIdx(isEditing ? null : globalIdx)}
                                                                                            className="text-xs text-blue-600 hover:text-blue-800 px-1.5 py-0.5"
                                                                                            title={isEditing ? 'Collapse' : 'Customise name / description'}
                                                                                        >
                                                                                            {isEditing ? 'Done' : 'Customise'}
                                                                                        </button>
                                                                                    )}
                                                                                    <button
                                                                                        type="button"
                                                                                        onClick={() => removeCapability(globalIdx)}
                                                                                        className="text-red-600 hover:text-red-800"
                                                                                    >
                                                                                        <Trash2 className="w-3 h-3" />
                                                                                    </button>
                                                                                </div>
                                                                            </div>
                                                                            {isEditing && cap.capability_type === 'tools' && (
                                                                                <div className="mt-2 pt-2 border-t border-default space-y-2">
                                                                                    <div>
                                                                                        <label className="block text-xs font-medium text-secondary mb-1">
                                                                                            Exposed name (override)
                                                                                        </label>
                                                                                        <input
                                                                                            type="text"
                                                                                            value={cap.exposed_name ?? ''}
                                                                                            onChange={(e) => updateMapping(globalIdx, { exposed_name: e.target.value })}
                                                                                            className="w-full px-2 py-1 border border-strong rounded text-xs font-mono"
                                                                                            placeholder={`Default: ${toolNamePrefix.trim() ? `${toolNamePrefix.trim()}${cap.capability_name}` : cap.capability_name}`}
                                                                                        />
                                                                                    </div>
                                                                                    <div>
                                                                                        <label className="block text-xs font-medium text-secondary mb-1">
                                                                                            Description override
                                                                                        </label>
                                                                                        <textarea
                                                                                            value={cap.description_override ?? ''}
                                                                                            onChange={(e) => updateMapping(globalIdx, { description_override: e.target.value })}
                                                                                            rows={2}
                                                                                            className="w-full px-2 py-1 border border-strong rounded text-xs"
                                                                                            placeholder="Optional. Leave empty to keep the upstream's description."
                                                                                        />
                                                                                    </div>
                                                                                </div>
                                                                            )}
                                                                        </div>
                                                                    );
                                                                })}
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        );
                                    })}
                                    {getTotalCapabilities() === 0 && (
                                        <div className="text-center py-8 text-muted text-sm">
                                            No capabilities selected yet. Add capabilities from the panel on the right.
                                        </div>
                                    )}
                                </div>
                            </div>

                            <div className="space-y-6">
                                <div>
                                    <h4 className="text-sm font-semibold text-default uppercase tracking-wider border-b pb-2 mb-4">
                                        Add Capabilities
                                    </h4>

                                    {enabledServersWithCapabilities.length === 0 && (
                                        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                                            <div className="flex items-start">
                                                <AlertCircle className="w-5 h-5 text-yellow-600 mr-3 flex-shrink-0 mt-0.5" />
                                                <div>
                                                    <h5 className="font-medium text-yellow-900 text-sm">No servers with capabilities available</h5>
                                                    <p className="text-sm text-yellow-700 mt-1">
                                                        You need enabled MCP servers with discovered capabilities to create virtual servers.
                                                        Go to the Servers tab to add and configure MCP servers first.
                                                    </p>
                                                </div>
                                            </div>
                                        </div>
                                    )}

                                    {enabledServersWithCapabilities.length > 0 && (
                                        <>
                                            <div className="space-y-3 mb-4">
                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Select Server
                                                    </label>
                                                    <select
                                                        value={selectedServer}
                                                        onChange={(e) => {
                                                            setSelectedServer(e.target.value);
                                                            setSearchTerm('');
                                                        }}
                                                        className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                                                    >
                                                        <option value="">Choose a server...</option>
                                                        {enabledServersWithCapabilities.map(server => (
                                                            <option key={server.id} value={server.id}>
                                                                {server.name} ({server.id})
                                                            </option>
                                                        ))}
                                                    </select>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Capability Type
                                                    </label>
                                                    <div className="grid grid-cols-3 gap-2">
                                                        {(['tools', 'resources', 'prompts'] as const).map(type => (
                                                            <button
                                                                key={type}
                                                                type="button"
                                                                onClick={() => setSelectedCapabilityType(type)}
                                                                className={`px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                                                                    selectedCapabilityType === type
                                                                        ? getCapabilityTypeColor(type).replace('100', '200')
                                                                        : 'bg-surface-raised text-secondary hover:bg-surface-inset'
                                                                }`}
                                                            >
                                                                <div className="flex items-center justify-center space-x-1">
                                                                    {getCapabilityTypeIcon(type)}
                                                                    <span className="capitalize">{type}</span>
                                                                </div>
                                                            </button>
                                                        ))}
                                                    </div>
                                                </div>

                                                {selectedServer && (
                                                    <button
                                                        type="button"
                                                        onClick={addAllCapabilities}
                                                        disabled={availableCaps.length === 0}
                                                        className="w-full px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-md transition-colors flex items-center justify-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                                    >
                                                        <Plus className="w-4 h-4" />
                                                        <span>Add All {selectedCapabilityType} from {getServerName(selectedServer)}</span>
                                                    </button>
                                                )}
                                            </div>

                                            {selectedServer && (
                                                <div className="border rounded-lg overflow-hidden">
                                                    <div className="bg-surface-alt px-3 py-2 border-b">
                                                        <div className="relative">
                                                            <Search className="w-4 h-4 absolute left-2 top-1/2 transform -translate-y-1/2 text-muted" />
                                                            <input
                                                                type="text"
                                                                value={searchTerm}
                                                                onChange={(e) => setSearchTerm(e.target.value)}
                                                                placeholder={`Search ${selectedCapabilityType}...`}
                                                                className="w-full pl-8 pr-3 py-1.5 border border-strong rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                                                            />
                                                        </div>
                                                    </div>
                                                    <div className="max-h-96 overflow-y-auto">
                                                        {filteredCaps.length === 0 ? (
                                                            <div className="p-4 text-center text-muted text-sm">
                                                                {availableCaps.length === 0
                                                                    ? `No ${selectedCapabilityType} available in this server`
                                                                    : 'No matching capabilities found'}
                                                            </div>
                                                        ) : (
                                                            <div className="divide-y">
                                                                {filteredCaps.map((capName, idx) => {
                                                                    const isAdded = isCapabilityAdded(selectedServer, capName, selectedCapabilityType);
                                                                    return (
                                                                        <div
                                                                            key={idx}
                                                                            className={`flex items-center justify-between p-3 hover:bg-surface-alt transition-colors ${
                                                                                isAdded ? 'bg-green-50' : ''
                                                                            }`}
                                                                        >
                                                                            <span className={`text-sm ${isAdded ? 'text-green-700 font-medium' : 'text-secondary'}`}>
                                                                                {capName}
                                                                            </span>
                                                                            {isAdded ? (
                                                                                <div className="flex items-center space-x-1 text-green-600">
                                                                                    <Check className="w-4 h-4" />
                                                                                    <span className="text-xs font-medium">Added</span>
                                                                                </div>
                                                                            ) : (
                                                                                <button
                                                                                    type="button"
                                                                                    onClick={() => addCapability(selectedServer, capName, selectedCapabilityType)}
                                                                                    className="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded transition-colors"
                                                                                >
                                                                                    Add
                                                                                </button>
                                                                            )}
                                                                        </div>
                                                                    );
                                                                })}
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                            )}

                                            {!selectedServer && (
                                                <div className="border border-dashed border-strong rounded-lg p-8 text-center">
                                                    <Server className="w-12 h-12 text-muted mx-auto mb-3" />
                                                    <p className="text-sm text-secondary">
                                                        Select a server above to view and add its capabilities
                                                    </p>
                                                </div>
                                            )}
                                        </>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>

                    <div className="flex items-center justify-between px-6 py-4 border-t bg-surface-alt sticky bottom-0">
                        <div className="text-sm text-muted">
                            * Required fields
                        </div>
                        <div className="flex space-x-3">
                            <button
                                type="button"
                                onClick={onCancel}
                                className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors text-sm font-medium"
                                disabled={isSubmitting}
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={asyncHandler(handleSubmit, 'click')}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                disabled={isSubmitting || capabilityMappings.length === 0}
                            >
                                {isSubmitting ? (
                                    <>
                                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                                        <span>Saving...</span>
                                    </>
                                ) : (
                                    <>
                                        <CheckCircle className="w-4 h-4" />
                                        <span>{virtualServer ? 'Update' : 'Create'} Virtual Server</span>
                                    </>
                                )}
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default VirtualServerForm;