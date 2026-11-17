/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import {
    X,
    Plus,
    RefreshCw,
    AlertCircle,
    User,
    Search,
    ChevronDown,
    Check,
    Folder,
    Info,
    Shield,
    Key
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { ResourceGroup, formatGroupLabel } from '../../../lib/groupTypes';
import { errorKind, errorMessage, failureSentence } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';

interface CreateGroupKeyModalProps {
    group: ResourceGroup;
    onClose: () => void;
    onKeyCreated: (key: any) => void;
}

interface EndUser {
    id: number;
    email: string;
    first_name?: string;
    last_name?: string;
    is_active: boolean;
    is_verified: boolean;
}

type AccessMode = 'user' | 'shared';

/**
 * Create Group Key Modal - Form for creating API keys scoped to a resource group
 *
 * Access Modes:
 * - User Access: Key grants access for specific users when they access this group
 * - Shared Access: Key grants shared access to group resources (not tied to specific users)
 */
const CreateGroupKeyModal: React.FC<CreateGroupKeyModalProps> = ({ group, onClose, onKeyCreated }) => {
    const { createGroupAPIKey, fetchEndUsers, gatewayClient } = useGateway();

    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [accessMode, setAccessMode] = useState<AccessMode>('shared');
    const [selectedUser, setSelectedUser] = useState<EndUser | null>(null);
    const [expiresAt, setExpiresAt] = useState('');
    // Optional agent-identity binding: calls with this key are attributed to
    // the agent principal and pass its agent-authz grants.
    const [agentId, setAgentId] = useState('');
    const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
    const [readFailure, reads] = useReadFailures<'agents'>();
    const [agentsAsked, setAgentsAsked] = useState(0);
    const [creating, setCreating] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // User selection state
    const [users, setUsers] = useState<EndUser[]>([]);
    const [loadingUsers, setLoadingUsers] = useState(false);
    const [userSearch, setUserSearch] = useState('');
    const [showUserDropdown, setShowUserDropdown] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);

    // Load users when access mode changes to 'user'
    useEffect(() => {
        if (accessMode === 'user' && users.length === 0) {
            runBackground(loadUsers(), 'users');
        }
    }, [accessMode]);

    // Close dropdown when clicking outside
    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
                setShowUserDropdown(false);
            }
        };

        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, []);

    const loadUsers = async () => {
        setLoadingUsers(true);
        try {
            const response = await fetchEndUsers({
                active_only: true,
                limit: 100
            });
            setUsers(response.users || []);
        } catch (err) {
            console.error('Failed to load users:', errorKind(err));
            setError(failureSentence('Failed to load users', err));
        } finally {
            setLoadingUsers(false);
        }
    };

    useEffect(() => {
        if (!gatewayClient) return;
        gatewayClient.listAgentIdentities()
            .then((resp: any) => {
                const rows = Array.isArray(resp?.agents) ? resp.agents
                    : Array.isArray(resp) ? resp : [];
                setAgents(rows.map((a: any) => ({ id: a.id, name: a.name })));
                reads.loaded('agents');
            })
            .catch((err: unknown) => {
                setAgents([]);
                reads.lost('agents', 'Failed to load the agent identities; the key cannot be bound to one', err);
            });
    }, [gatewayClient, agentsAsked, reads]);

    const handleUserSelect = (user: EndUser) => {
        setSelectedUser(user);
        setUserSearch(getUserDisplayName(user));
        setShowUserDropdown(false);
        setError(null);
    };

    const getUserDisplayName = (user: EndUser): string => {
        const name = [user.first_name, user.last_name].filter(Boolean).join(' ');
        return name ? `${name} (${user.email})` : user.email;
    };

    const filteredUsers = users.filter(user => {
        if (!userSearch.trim()) return true;
        const searchLower = userSearch.toLowerCase();
        const displayName = getUserDisplayName(user).toLowerCase();
        const userId = String(user.id).toLowerCase();
        return displayName.includes(searchLower) || userId.includes(searchLower);
    });

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();

        if (!name.trim()) {
            setError('Name is required');
            return;
        }

        if (accessMode === 'user' && !selectedUser) {
            setError('Please select a user for user-specific access');
            return;
        }

        setCreating(true);
        setError(null);

        try {
            // Use Gateway client to create the key
            const keyData = await createGroupAPIKey(group.id, {
                name: name.trim(),
                description: description.trim() || undefined,
                access_mode: accessMode,
                user_id: accessMode === 'user' && selectedUser ? selectedUser.id : undefined,  // Send as number
                expires_at: expiresAt || undefined,
                agent_id: agentId || undefined
            });

            onKeyCreated(keyData);
            onClose();
        } catch (err) {
            console.error('Failed to create key:', errorKind(err));
            setError(errorMessage(err, 'Failed to create API key'));
        } finally {
            setCreating(false);
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
            <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <Key className="w-5 h-5 text-primary-600" />
                        <h3 className="text-lg font-semibold text-default">
                            Create API Key
                        </h3>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Form */}
                <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="px-6 py-6 space-y-5 overflow-y-auto">
                    {/* Group Info - Compact */}
                    <div className="flex items-center space-x-2 text-sm text-secondary pb-4 border-b border-default">
                        <Folder className="w-4 h-4 text-primary-600 flex-shrink-0" />
                        <span className="text-muted">Creating key for:</span>
                        <span className="font-medium text-default">{formatGroupLabel(group)}</span>
                    </div>

                    {/* Error Message */}
                    {error && (
                        <div className="bg-surface-alt border border-default rounded-lg p-3 flex items-start space-x-2">
                            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                            <div className="flex-1 min-h-0 min-w-0">
                                <p className="text-sm text-red-700">{error}</p>
                            </div>
                        </div>
                    )}

                    {/* Key Name */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">
                            Key Name <span className="text-red-500">*</span>
                        </label>
                        <input
                            type="text"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                            placeholder="e.g., Production API Key"
                            required
                        />
                        <p className="text-xs text-muted mt-1">
                            A descriptive name to identify this key
                        </p>
                    </div>

                    {/* Description */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">
                            Description (optional)
                        </label>
                        <textarea
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            rows={2}
                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                            placeholder="What will this key be used for?"
                        />
                    </div>

                    {/* Access Mode Selection */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-3">
                            Access Mode <span className="text-red-500">*</span>
                        </label>

                        <div className="space-y-3">
                            {/* Shared Access Option */}
                            <label
                                className={`flex items-start space-x-3 p-4 border-2 rounded-lg cursor-pointer transition-all ${
                                    accessMode === 'shared'
                                        ? 'border-primary-500 bg-primary-50'
                                        : 'border-default hover:border-strong'
                                }`}
                            >
                                <input
                                    type="radio"
                                    name="accessMode"
                                    value="shared"
                                    checked={accessMode === 'shared'}
                                    onChange={(e) => {
                                        setAccessMode(e.target.value as AccessMode);
                                        setSelectedUser(null);
                                        setUserSearch('');
                                    }}
                                    className="mt-0.5 w-4 h-4 text-primary-600 focus:ring-primary-500"
                                />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <div className="flex items-center space-x-2 mb-1">
                                        <Shield className="w-4 h-4 text-green-600" />
                                        <span className="font-medium text-default">Shared Access</span>
                                        <span className="text-xs bg-surface-alt text-green-700 px-2 py-0.5 rounded-full">
                                            Recommended
                                        </span>
                                    </div>
                                    <p className="text-sm text-secondary leading-relaxed">
                                        Key provides shared access to group resources. Not tied to any specific user.
                                        Ideal for services, applications, and automation.
                                    </p>
                                </div>
                            </label>

                            {/* User Access Option */}
                            <label
                                className={`flex items-start space-x-3 p-4 border-2 rounded-lg cursor-pointer transition-all ${
                                    accessMode === 'user'
                                        ? 'border-primary-500 bg-primary-50'
                                        : 'border-default hover:border-strong'
                                }`}
                            >
                                <input
                                    type="radio"
                                    name="accessMode"
                                    value="user"
                                    checked={accessMode === 'user'}
                                    onChange={(e) => setAccessMode(e.target.value as AccessMode)}
                                    className="mt-0.5 w-4 h-4 text-primary-600 focus:ring-primary-500"
                                />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <div className="flex items-center space-x-2 mb-1">
                                        <User className="w-4 h-4 text-muted" />
                                        <span className="font-medium text-default">User-Specific Access</span>
                                    </div>
                                    <p className="text-sm text-secondary leading-relaxed">
                                        Key grants access for a specific user when they access this group.
                                        Requests are tracked and associated with the user.
                                    </p>
                                </div>
                            </label>
                        </div>

                        {/* Inline Help Text */}
                        <p className="mt-2 text-xs text-muted flex items-start space-x-1">
                            <Info className="w-3 h-3 flex-shrink-0 mt-0.5" />
                            <span>
                                <strong>Tip:</strong> Shared Access is recommended for most use cases including CI/CD, automated services, and applications.
                            </span>
                        </p>
                    </div>

                    {/* User Selection (only for user access mode) */}
                    {accessMode === 'user' && (
                        <div className="border-t border-default pt-4">
                            <label className="block text-sm font-medium text-secondary mb-2">
                                Select User <span className="text-red-500">*</span>
                            </label>

                            {loadingUsers ? (
                                <div className="flex items-center justify-center py-8">
                                    <RefreshCw className="w-5 h-5 text-primary-600 animate-spin" />
                                    <span className="ml-2 text-sm text-secondary">Loading users...</span>
                                </div>
                            ) : (
                                <div className="relative" ref={dropdownRef}>
                                    {/* Search Input */}
                                    <div className="relative">
                                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                                        <input
                                            type="text"
                                            value={userSearch}
                                            onChange={(e) => {
                                                setUserSearch(e.target.value);
                                                setShowUserDropdown(true);
                                                if (!e.target.value) {
                                                    setSelectedUser(null);
                                                }
                                            }}
                                            onFocus={() => setShowUserDropdown(true)}
                                            className="w-full pl-9 pr-10 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                                            placeholder="Search by name or email..."
                                        />
                                        <ChevronDown className="absolute right-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                                    </div>

                                    {/* Dropdown */}
                                    {showUserDropdown && (
                                        <div className="absolute z-10 w-full mt-1 bg-surface border border-strong rounded-lg shadow-lg max-h-60 overflow-y-auto">
                                            {filteredUsers.length === 0 ? (
                                                <div className="px-4 py-8 text-center text-sm text-muted">
                                                    {userSearch ? 'No users found matching your search' : 'No users available'}
                                                </div>
                                            ) : (
                                                <ul className="py-1">
                                                    {filteredUsers.map((user) => (
                                                        <li key={user.id}>
                                                            <button
                                                                type="button"
                                                                onClick={() => handleUserSelect(user)}
                                                                className="w-full px-4 py-2 text-left hover:bg-surface-alt flex items-center justify-between group"
                                                            >
                                                                <div className="flex-1 min-w-0 min-h-0">
                                                                    <div className="flex items-center space-x-2">
                                                                        <User className="w-4 h-4 text-muted flex-shrink-0" />
                                                                        <div className="min-w-0 flex-1 min-h-0">
                                                                            <p className="text-sm font-medium text-default truncate">
                                                                                {user.first_name && user.last_name
                                                                                    ? `${user.first_name} ${user.last_name}`
                                                                                    : user.email
                                                                                }
                                                                            </p>
                                                                            <p className="text-xs text-muted truncate">
                                                                                {user.first_name && user.last_name ? user.email : `ID: ${user.id}`}
                                                                            </p>
                                                                        </div>
                                                                    </div>
                                                                </div>
                                                                {selectedUser?.id === user.id && (
                                                                    <Check className="w-4 h-4 text-primary-600 flex-shrink-0 ml-2" />
                                                                )}
                                                            </button>
                                                        </li>
                                                    ))}
                                                </ul>
                                            )}
                                        </div>
                                    )}

                                    {/* Selected User Info */}
                                    {selectedUser && (
                                        <div className="mt-2 p-3 bg-surface-alt border border-default rounded-lg">
                                            <div className="flex items-start space-x-2">
                                                <User className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                                <div className="flex-1 min-w-0 min-h-0">
                                                    <p className="text-sm font-medium text-secondary">
                                                        {selectedUser.first_name && selectedUser.last_name
                                                            ? `${selectedUser.first_name} ${selectedUser.last_name}`
                                                            : 'User'}
                                                    </p>
                                                    <p className="text-xs text-secondary">{selectedUser.email}</p>
                                                    <p className="text-xs text-muted mt-1">User ID: {selectedUser.id}</p>
                                                </div>
                                            </div>
                                        </div>
                                    )}

                                    {/* Refresh Button */}
                                    <button
                                        type="button"
                                        onClick={asyncHandler(loadUsers, 'click')}
                                        disabled={loadingUsers}
                                        className="mt-2 text-xs text-primary-600 hover:text-primary-700 flex items-center space-x-1 disabled:opacity-50"
                                    >
                                        <RefreshCw className={`w-3 h-3 ${loadingUsers ? 'animate-spin' : ''}`} />
                                        <span>Refresh user list</span>
                                    </button>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Expiration Date */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">
                            Expiration Date (optional)
                        </label>
                        <input
                            type="datetime-local"
                            value={expiresAt}
                            onChange={(e) => setExpiresAt(e.target.value)}
                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                        />
                        <p className="text-xs text-muted mt-1">
                            Leave empty for keys that never expire
                        </p>
                    </div>

                    {/* Agent identity binding */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">
                            Agent identity (optional)
                        </label>
                        <ReadFailureNotice compact failure={readFailure('agents')} onRetry={() => setAgentsAsked((n) => n + 1)} />
                        <select
                            value={agentId}
                            onChange={(e) => setAgentId(e.target.value)}
                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-primary-500 bg-surface"
                        >
                            <option value="">— No agent binding —</option>
                            {agents.map((a) => (
                                <option key={a.id} value={a.id}>{a.name}</option>
                            ))}
                        </select>
                        <p className="text-xs text-muted mt-1">
                            Bind this key to an agent principal: every call it makes is
                            attributed to that agent and passes through its Agent Policy
                            grants (Governance → Agent Policies)
                        </p>
                    </div>
                </form>

                {/* Footer */}
                <div className="flex items-center justify-end space-x-3 px-6 py-4 border-t border-default bg-surface-alt">
                    <button
                        type="button"
                        onClick={onClose}
                        disabled={creating}
                        className="px-4 py-2 border border-strong text-secondary rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={(e: any) => {
                            e.preventDefault();
                            const form = e.target.closest('.bg-surface').querySelector('form');
                            if (form) form.requestSubmit();
                        }}
                        disabled={creating || (accessMode === 'user' && !selectedUser)}
                        className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-2"
                    >
                        {creating ? (
                            <>
                                <RefreshCw className="w-4 h-4 animate-spin" />
                                <span>Creating...</span>
                            </>
                        ) : (
                            <>
                                <Plus className="w-4 h-4" />
                                <span>Create Key</span>
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default CreateGroupKeyModal;