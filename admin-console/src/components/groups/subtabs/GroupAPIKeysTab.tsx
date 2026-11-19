/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    Key,
    Plus,
    Copy,
    Check,
    Eye,
    EyeOff,
    Ban,
    Loader,
    AlertCircle,
    Search,
    X,
    User,
    AlertTriangle,
    CheckCircle,
    XCircle,
    Info,
    Shield,
    RefreshCw
} from 'lucide-react';
import { ResourceGroup, GroupAPIKey, GroupAPIKeyUsageResponse } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import CreateGroupKeyModal from './CreateGroupKeyModal';
import { useConfirm } from '../../common/ConfirmationDialog';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground, runUserAction } from '../../../lib/asyncSafe';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';
import { useAuth } from '../../../hooks/useAuth';
import { IDENTITY_POLICY_READ, keyStanding, LEVEL_LABELS, type IdentityPolicyResponse, type KeyStanding } from '../../../lib/identityPolicy';

/** What the gateway does with a usable key under the group's identity policy, when it is not simply accepted. */
function standingText(standing: KeyStanding, governed: boolean): string | null {
    switch (standing.standing) {
        case 'met':
            return null;
        case 'refused':
            return `Refused now: the identity policy requires ${LEVEL_LABELS[standing.required]}.`;
        case 'below_policy':
            return `Served but recorded below policy (requires ${LEVEL_LABELS[standing.required]})` + (governed ? '; promotion will refuse this key.' : '.');
        case 'refused_after_promotion':
            return 'Promotion will refuse this key: an AI System in production requires workload identity.';
    }
}

interface Props {
    group: ResourceGroup;
}

const GroupAPIKeysTab: React.FC<Props> = ({ group }) => {
    const { gatewayClient, fetchGroupAPIKeys, fetchGroupAPIKeyUsage, revokeAPIKey, enableAPIKey, disableAPIKey } = useGateway();
    const { hasPermission } = useAuth();
    const canReadIdentity = hasPermission(IDENTITY_POLICY_READ);
    const [identity, setIdentity] = useState<IdentityPolicyResponse | null>(null);
    const confirm = useConfirm();

    const [keys, setKeys] = useState<GroupAPIKey[]>([]);
    const [usage, setUsage] = useState<GroupAPIKeyUsageResponse | null>(null);
    const [readFailure, reads] = useReadFailures<'usage' | 'identity'>();
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [copiedKey, setCopiedKey] = useState<string | null>(null);
    const [actioningKey, setActioningKey] = useState<string | null>(null);
    const [newlyCreatedKey, setNewlyCreatedKey] = useState<any | null>(null);
    const [showNewKeyModal, setShowNewKeyModal] = useState(false);
    const [showInfoModal, setShowInfoModal] = useState(false);

    useEffect(() => {
        runBackground(loadKeys(), 'keys');
        runBackground(loadUsage(), 'usage');
    }, [group.id]);

    // The group's identity policy, for each key's standing under it. Without
    // the permission the keys show their level only; a failed read says so.
    useEffect(() => {
        if (!canReadIdentity) return;
        gatewayClient.fetchIdentityPolicy(group.id).then(
            (answer) => {
                setIdentity(answer);
                reads.loaded('identity');
            },
            (err: unknown) => {
                setIdentity(null);
                reads.failed('identity', 'Failed to load the identity policy of this group', err);
            },
        );
    }, [canReadIdentity, gatewayClient, group.id, reads]);

    const loadKeys = async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchGroupAPIKeys(group.id);
            setKeys(response.keys || []);
        } catch (err) {
            console.error('Failed to load keys:', errorKind(err));
            setError(errorMessage(err, 'Failed to load API keys'));
        } finally {
            setLoading(false);
        }
    };

    const loadUsage = async () => {
        try {
            const response = await fetchGroupAPIKeyUsage(group.id);
            setUsage(response);
            reads.loaded('usage');
        } catch (err) {
            reads.failed('usage', 'Failed to load the usage of the API keys', err);
        }
    };

    const handleToggleEnabled = async (key: GroupAPIKey) => {
        try {
            setActioningKey(key.id);
            setError('');

            if (key.enabled) {
                await disableAPIKey(key.id);
            } else {
                await enableAPIKey(key.id);
            }

            await loadKeys();
            await loadUsage();
        } catch (err) {
            console.error('Failed to toggle key:', errorKind(err));
            setError(errorMessage(err, 'Failed to toggle key'));
        } finally {
            setActioningKey(null);
        }
    };

    const handleRevoke = async (keyId: string, keyName: string) => {
        if (!(await confirm({
            title: 'Revoke API key?',
            message: `"${keyName}" will be revoked and any clients using it will lose access immediately.`,
            confirmLabel: 'Revoke key',
            note: 'This cannot be undone.',
        }))) return;

        try {
            setActioningKey(keyId);
            setError('');
            await revokeAPIKey(keyId);
            await loadKeys();
            await loadUsage();
        } catch (err) {
            console.error('Failed to revoke key:', errorKind(err));
            setError(errorMessage(err, 'Failed to revoke key'));
        } finally {
            setActioningKey(null);
        }
    };

    const copyToClipboard = (keyId: string, text: string) => {
        runUserAction(navigator.clipboard.writeText(text), 'write text');
        setCopiedKey(keyId);
        setTimeout(() => setCopiedKey(null), 2000);
    };

    // Filter keys by search
    const filteredKeys = keys.filter(key =>
        key.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        key.key_prefix.toLowerCase().includes(searchTerm.toLowerCase()) ||
        key.id.toLowerCase().includes(searchTerm.toLowerCase())
    );

    // Format number
    const formatNumber = (num: number) => {
        if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
        if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
        return num.toString();
    };

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2">
                    <div>
                        <h2 className="text-lg font-semibold text-default">API Keys</h2>
                        <p className="text-sm text-secondary mt-1">
                            Manage group-scoped API keys for accessing LLM models and MCP servers
                        </p>
                    </div>

                    {/* Info Button */}
                    <button
                        onClick={() => setShowInfoModal(true)}
                        className="p-1.5 text-muted hover:text-primary hover:bg-primary-pale rounded-lg transition-colors"
                        title="About API Keys"
                    >
                        <Info className="w-5 h-5" />
                    </button>
                </div>

                <div className="flex items-center space-x-2">
                    <button
                        onClick={() => {
                            runBackground(loadKeys(), 'keys');
                            runBackground(loadUsage(), 'usage');
                        }}
                        disabled={loading}
                        className="px-3 py-2 border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Refresh"
                    >
                        <RefreshCw className={`w-4 h-4 text-secondary ${loading ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                        onClick={() => setShowCreateModal(true)}
                        className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-2"
                    >
                        <Plus className="w-4 h-4" />
                        <span>Create Key</span>
                    </button>
                </div>
            </div>

            {/* Error Message */}
            {error && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-red-900">Error</p>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                    </div>
                </div>
            )}

            <ReadFailureNotice compact failure={readFailure('identity')} data-testid="keys-identity-failure" />

            {/* Search */}
            <div className="relative">
                <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                <input
                    type="text"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    placeholder="Search by key name or prefix..."
                    className="w-full pl-10 pr-10 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                />
                {searchTerm && (
                    <button
                        onClick={() => setSearchTerm('')}
                        className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted hover:text-secondary"
                    >
                        <X className="w-4 h-4" />
                    </button>
                )}
            </div>

            {/* Keys Table */}
            {loading ? (
                <div className="flex items-center justify-center h-48">
                    <div className="text-center">
                        <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                        <p className="text-sm text-secondary">Loading API keys...</p>
                    </div>
                </div>
            ) : filteredKeys.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <Key className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {searchTerm ? 'No keys found matching your search' : 'No API keys yet'}
                    </p>
                    {!searchTerm && (
                        <p className="text-sm text-muted mt-2">
                            Create a group-scoped key that all members can use
                        </p>
                    )}
                </div>
            ) : (
                <div className="border border-default rounded-lg overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                            <tr>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Name / ID
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Key Snippet
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Description
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Enabled
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Type
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Level
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Last Used
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Expires
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                                    Actions
                                </th>
                            </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                            {filteredKeys.map((key) => {
                                return (
                                    <tr key={key.id} className="hover:bg-surface-alt transition-colors">
                                        {/* Name + ID */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <div className="flex items-center space-x-3">
                                                <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-gradient-to-br from-blue-100 to-cyan-100 flex items-center justify-center">
                                                    <Key className="w-5 h-5 text-primary" />
                                                </div>
                                                <div>
                                                    <div className="text-sm font-medium text-default">
                                                        {key.name}
                                                    </div>
                                                    <div className="text-xs text-muted font-mono">
                                                        {key.id.substring(0, 20)}...
                                                    </div>
                                                    {key.scope === 'user' && key.user_email && (
                                                        <div className="flex items-center space-x-1 mt-1 text-xs border-primary-500 text-primary-600 bg-primary-50">
                                                            <User className="w-3 h-3" />
                                                            <span>{key.user_email}</span>
                                                        </div>
                                                    )}
                                                </div>
                                            </div>
                                        </td>

                                        {/* Key Snippet */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <div className="flex items-center space-x-2">
                                                <code className="text-xs text-secondary font-mono bg-surface-raised px-2 py-1 rounded">
                                                    {key.key_prefix}...
                                                </code>
                                                <button
                                                    onClick={() => copyToClipboard(key.id, key.key_prefix)}
                                                    className="text-muted hover:text-secondary transition-colors"
                                                    title="Copy prefix"
                                                >
                                                    {copiedKey === key.id ? (
                                                        <Check className="w-4 h-4 text-green-600" />
                                                    ) : (
                                                        <Copy className="w-4 h-4" />
                                                    )}
                                                </button>
                                            </div>
                                        </td>

                                        {/* Description */}
                                        <td className="px-6 py-4">
                                            <div className="text-sm text-secondary max-w-xs truncate" title={key.description || ''}>
                                                {key.description || <span className="text-muted italic">No description</span>}
                                            </div>
                                        </td>

                                        {/* Enabled */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            {key.revoked ? (
                                                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-surface-alt text-red-800">
                                                        <Ban className="w-3 h-3 mr-1" />
                                                        Revoked
                                                    </span>
                                            ) : key.is_expired ? (
                                                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-surface-alt text-amber-800">
                                                        <AlertTriangle className="w-3 h-3 mr-1" />
                                                        Expired
                                                    </span>
                                            ) : key.enabled ? (
                                                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-surface-alt text-green-800">
                                                        <CheckCircle className="w-3 h-3 mr-1" />
                                                        Enabled
                                                    </span>
                                            ) : (
                                                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-surface-raised text-default">
                                                        <XCircle className="w-3 h-3 mr-1" />
                                                        Disabled
                                                    </span>
                                            )}
                                        </td>

                                        {/* Type */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            {key.scope === 'user' ? (
                                                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border-primary-500 text-primary-600 bg-primary-50">
                                                        <User className="w-3 h-3 mr-1" />
                                                        User-Specific
                                                    </span>
                                            ) : (
                                                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border-primary-500 text-primary-600 bg-primary-50">
                                                        <Shield className="w-3 h-3 mr-1" />
                                                        Shared
                                                    </span>
                                            )}
                                        </td>

                                        {/* Credential level, and the key's standing under the identity policy */}
                                        <td className="px-6 py-4">
                                            <span
                                                className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-mono font-medium bg-surface-alt text-secondary"
                                                title="Every API key is credential level key"
                                                data-testid={`key-level-${key.id}`}
                                            >
                                                key
                                            </span>
                                            {(() => {
                                                if (!identity || key.revoked || key.is_expired) return null;
                                                const text = standingText(keyStanding(identity, key.scope), identity.attributed_to_ai_system);
                                                return text ? (
                                                    <div className="mt-1 flex items-start gap-1 text-xs text-amber-700 max-w-xs whitespace-normal" data-testid={`key-warning-${key.id}`}>
                                                        <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
                                                        <span>{text}</span>
                                                    </div>
                                                ) : null;
                                            })()}
                                        </td>

                                        {/* Last Used */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <div className="text-sm text-default">
                                                {key.last_used_at
                                                    ? new Date(key.last_used_at).toLocaleDateString()
                                                    : <span className="text-muted">Never</span>
                                                }
                                            </div>
                                            <div className="text-xs text-muted">
                                                {key.usage_count} {key.usage_count === 1 ? 'request' : 'requests'}
                                            </div>
                                        </td>

                                        {/* Expires */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <div className="text-sm text-default">
                                                {key.expires_at ? (
                                                    <>
                                                        {new Date(key.expires_at).toLocaleDateString()}
                                                        {key.is_expired && (
                                                            <div className="text-xs text-red-600 font-medium mt-0.5">
                                                                Expired
                                                            </div>
                                                        )}
                                                    </>
                                                ) : (
                                                    <span className="text-muted">Never</span>
                                                )}
                                            </div>
                                        </td>

                                        {/* Actions */}
                                        <td className="px-6 py-4 whitespace-nowrap text-right">
                                            <div className="flex items-center justify-end space-x-2">
                                                {!key.revoked && (
                                                    <>
                                                        <button
                                                            onClick={asyncHandler(() => handleToggleEnabled(key), 'click')}
                                                            disabled={actioningKey === key.id}
                                                            className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                                                            title={key.enabled ? 'Disable key' : 'Enable key'}
                                                        >
                                                            {actioningKey === key.id ? (
                                                                <Loader className="w-4 h-4 animate-spin" />
                                                            ) : key.enabled ? (
                                                                <EyeOff className="w-4 h-4" />
                                                            ) : (
                                                                <Eye className="w-4 h-4" />
                                                            )}
                                                        </button>
                                                        <button
                                                            onClick={asyncHandler(() => handleRevoke(key.id, key.name), 'click')}
                                                            disabled={actioningKey === key.id}
                                                            className="p-2 text-red-600 hover:text-red-900 hover:bg-surface-alt rounded-lg transition-colors disabled:opacity-50"
                                                            title="Revoke key"
                                                        >
                                                            <Ban className="w-4 h-4" />
                                                        </button>
                                                    </>
                                                )}
                                            </div>
                                        </td>
                                    </tr>
                                );
                            })}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Summary Footer */}
            {filteredKeys.length > 0 && (
                <div className="bg-surface-alt rounded-lg p-4 border border-default">
                    <div className="flex items-center justify-between text-sm">
                        <div className="flex items-center space-x-6">
                            <div>
                                <span className="text-secondary">Showing:</span>
                                <span className="font-semibold text-default ml-2">
                                    {filteredKeys.length} key{filteredKeys.length !== 1 ? 's' : ''}
                                </span>
                            </div>
                            <ReadFailureNotice compact failure={readFailure('usage')} onRetry={() => runBackground(loadUsage(), 'usage')} />
                            {usage && (
                                <>
                                    <div>
                                        <span className="text-secondary">Active:</span>
                                        <span className="font-semibold text-green-600 ml-2">
                                            {usage.active_keys}
                                        </span>
                                    </div>
                                    <div>
                                        <span className="text-secondary">Total Requests:</span>
                                        <span className="font-semibold border-primary-500 text-primary-600 bg-primary-50 ml-2">
                                            {formatNumber(usage.total_usage)}
                                        </span>
                                    </div>
                                </>
                            )}
                        </div>

                        {searchTerm && (
                            <button
                                onClick={() => setSearchTerm('')}
                                className="text-primary-600 hover:text-primary-700 font-medium"
                            >
                                Clear search
                            </button>
                        )}
                    </div>
                </div>
            )}

            {/* Create Key Modal */}
            {showCreateModal && (
                <CreateGroupKeyModal
                    group={group}
                    onClose={() => setShowCreateModal(false)}
                    onKeyCreated={(key) => {
                        setNewlyCreatedKey(key);
                        setShowNewKeyModal(true);
                        runBackground(loadKeys(), 'keys');
                        runBackground(loadUsage(), 'usage');
                    }}
                />
            )}

            {/* Newly Created Key Modal */}
            {showNewKeyModal && newlyCreatedKey && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full mx-4">
                        {/* Header */}
                        <div className="flex items-start justify-between p-6 border-b border-default">
                            <div className="flex items-start space-x-3">
                                <div className="flex-shrink-0 w-10 h-10 bg-surface-alt rounded-lg flex items-center justify-center">
                                    <CheckCircle className="w-6 h-6 text-green-600" />
                                </div>
                                <div>
                                    <h3 className="text-lg font-semibold text-default">
                                        API Key Created Successfully
                                    </h3>
                                    <p className="text-sm text-secondary mt-1">
                                        {newlyCreatedKey.name}
                                    </p>
                                </div>
                            </div>
                            <button
                                onClick={() => {
                                    setShowNewKeyModal(false);
                                    setNewlyCreatedKey(null);
                                }}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>

                        {/* Body */}
                        <div className="p-6 space-y-4">
                            {/* Warning */}
                            <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                                <AlertTriangle className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
                                <div>
                                    <p className="text-sm font-medium text-amber-900">
                                        Save this key now!
                                    </p>
                                    <p className="text-sm text-amber-700 mt-1">
                                        This is the only time you'll be able to see the full key. Make sure to copy and store it securely.
                                    </p>
                                </div>
                            </div>

                            {/* Key Display */}
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Your API Key
                                </label>
                                <div className="flex items-center space-x-2">
                                    <div className="flex-1 px-4 py-3 bg-surface-alt border border-strong rounded-lg min-h-0 min-w-0">
                                        <code className="text-sm text-default font-mono break-all">
                                            {newlyCreatedKey.key_value || newlyCreatedKey.full_key || newlyCreatedKey.key}
                                        </code>
                                    </div>
                                    <button
                                        onClick={() => copyToClipboard('new', newlyCreatedKey.key_value || newlyCreatedKey.full_key || newlyCreatedKey.key)}
                                        className="px-4 py-3 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors flex items-center space-x-2 flex-shrink-0"
                                    >
                                        {copiedKey === 'new' ? (
                                            <>
                                                <Check className="w-4 h-4" />
                                                <span className="text-sm font-medium">Copied!</span>
                                            </>
                                        ) : (
                                            <>
                                                <Copy className="w-4 h-4" />
                                                <span className="text-sm font-medium">Copy</span>
                                            </>
                                        )}
                                    </button>
                                </div>
                            </div>

                            {/* Key Details */}
                            <div className="grid grid-cols-2 gap-4 pt-4 border-t border-default">
                                <div>
                                    <p className="text-xs text-muted uppercase tracking-wide mb-1">Key Prefix</p>
                                    <code className="text-sm text-default font-mono">{newlyCreatedKey.key_prefix}</code>
                                </div>
                                <div>
                                    <p className="text-xs text-muted uppercase tracking-wide mb-1">Access Mode</p>
                                    <p className="text-sm text-default">
                                        {newlyCreatedKey.access_mode === 'user' ? 'User-Specific' : 'Shared Access'}
                                    </p>
                                </div>
                                {newlyCreatedKey.user_email && (
                                    <div className="col-span-2">
                                        <p className="text-xs text-muted uppercase tracking-wide mb-1">User</p>
                                        <p className="text-sm text-default">{newlyCreatedKey.user_email}</p>
                                    </div>
                                )}
                            </div>
                        </div>

                        {/* Footer */}
                        <div className="flex items-center justify-end space-x-3 px-6 py-4 bg-surface-alt border-t border-default rounded-b-lg">
                            <button
                                onClick={() => {
                                    setShowNewKeyModal(false);
                                    setNewlyCreatedKey(null);
                                }}
                                className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors font-medium"
                            >
                                Done
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Info Modal */}
            {showInfoModal && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[90vh] flex flex-col">
                        {/* Header */}
                        <div className="flex items-start justify-between p-6 border-b border-default">
                            <div className="flex items-start space-x-3">
                                <div className="flex-shrink-0 w-10 h-10 bg-primary-pale rounded-lg flex items-center justify-center">
                                    <Info className="w-6 h-6 text-primary" />
                                </div>
                                <div>
                                    <h3 className="text-lg font-semibold text-default">
                                        About Group API Keys
                                    </h3>
                                    <p className="text-sm text-secondary mt-1">
                                        Understanding group-scoped API keys and access modes
                                    </p>
                                </div>
                            </div>
                            <button
                                onClick={() => setShowInfoModal(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>

                        {/* Body */}
                        <div className="p-6 scroll-region">
                            <div className="space-y-6">
                                {/* Group-Scoped Keys Section */}
                                <div>
                                    <h4 className="text-base font-semibold text-default mb-3 flex items-center">
                                        <Key className="w-5 h-5 text-primary mr-2" />
                                        Group-Scoped Keys
                                    </h4>
                                    <div className="space-y-3 text-sm text-secondary">
                                        <p>
                                            API keys scoped to this resource group provide access to the group's LLM models and MCP servers.
                                        </p>
                                        <div className="bg-primary-pale border border-primary rounded-lg p-4">
                                            <p className="font-medium text-primary mb-2">Key Features:</p>
                                            <ul className="space-y-2 text-primary">
                                                <li className="flex items-start">
                                                    <CheckCircle className="w-4 h-4 text-primary mr-2 flex-shrink-0 mt-0.5" />
                                                    <span>Can be used by all members of this group and descendant groups</span>
                                                </li>
                                                <li className="flex items-start">
                                                    <CheckCircle className="w-4 h-4 text-primary mr-2 flex-shrink-0 mt-0.5" />
                                                    <span>Usage is tracked per user for accountability and auditing</span>
                                                </li>
                                                <li className="flex items-start">
                                                    <CheckCircle className="w-4 h-4 text-primary mr-2 flex-shrink-0 mt-0.5" />
                                                    <span>Access permissions follow the group hierarchy</span>
                                                </li>
                                            </ul>
                                        </div>
                                    </div>
                                </div>

                                {/* Access Modes Section */}
                                <div className="pt-4 border-t border-default">
                                    <h4 className="text-base font-semibold text-default mb-3 flex items-center">
                                        <Shield className="w-5 h-5 text-green-600 mr-2" />
                                        Access Modes
                                    </h4>
                                    <div className="space-y-4">
                                        {/* Shared Access */}
                                        <div className="border border-default rounded-lg p-4">
                                            <div className="flex items-start space-x-3">
                                                <div className="flex-shrink-0 w-8 h-8 bg-surface-alt rounded-lg flex items-center justify-center">
                                                    <Shield className="w-5 h-5 text-green-600" />
                                                </div>
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <h5 className="font-semibold text-default mb-1">Shared Access</h5>
                                                    <p className="text-sm text-secondary mb-2">
                                                        Keys for services, automation, CI/CD pipelines, and applications that are not tied to specific users.
                                                    </p>
                                                    <div className="text-sm text-secondary space-y-1">
                                                        <p className="font-medium text-default">Best for:</p>
                                                        <ul className="list-disc list-inside space-y-1 text-secondary ml-2">
                                                            <li>Production services and microservices</li>
                                                            <li>CI/CD pipelines and deployment automation</li>
                                                            <li>Background jobs and scheduled tasks</li>
                                                            <li>Third-party integrations</li>
                                                        </ul>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>

                                        {/* User-Specific Access */}
                                        <div className="border border-default rounded-lg p-4">
                                            <div className="flex items-start space-x-3">
                                                <div className="flex-shrink-0 w-8 h-8 bg-surface-alt rounded-lg flex items-center justify-center">
                                                    <User className="w-5 h-5 text-muted" />
                                                </div>
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <h5 className="font-semibold text-default mb-1">User-Specific Access</h5>
                                                    <p className="text-sm text-secondary mb-2">
                                                        Keys tied to specific users for enhanced tracking, attribution, and accountability.
                                                    </p>
                                                    <div className="text-sm text-secondary space-y-1">
                                                        <p className="font-medium text-default">Best for:</p>
                                                        <ul className="list-disc list-inside space-y-1 text-secondary ml-2">
                                                            <li>Individual developer access</li>
                                                            <li>Personal testing and development</li>
                                                            <li>User-specific usage tracking</li>
                                                            <li>Compliance and audit requirements</li>
                                                        </ul>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                {/* Security Best Practices */}
                                <div className="pt-4 border-t border-default">
                                    <h4 className="text-base font-semibold text-default mb-3 flex items-center">
                                        <AlertTriangle className="w-5 h-5 text-amber-600 mr-2" />
                                        Security Best Practices
                                    </h4>
                                    <div className="bg-surface-alt border border-default rounded-lg p-4">
                                        <ul className="space-y-2 text-sm text-amber-900">
                                            <li className="flex items-start">
                                                <span className="font-bold mr-2">•</span>
                                                <span>Never share API keys or commit them to version control</span>
                                            </li>
                                            <li className="flex items-start">
                                                <span className="font-bold mr-2">•</span>
                                                <span>Store keys securely in environment variables or secret management systems</span>
                                            </li>
                                            <li className="flex items-start">
                                                <span className="font-bold mr-2">•</span>
                                                <span>Rotate keys regularly and revoke unused or compromised keys immediately</span>
                                            </li>
                                            <li className="flex items-start">
                                                <span className="font-bold mr-2">•</span>
                                                <span>Use user-specific keys when you need to track individual usage</span>
                                            </li>
                                            <li className="flex items-start">
                                                <span className="font-bold mr-2">•</span>
                                                <span>Set expiration dates for temporary or testing keys</span>
                                            </li>
                                        </ul>
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Footer */}
                        <div className="flex items-center justify-end px-6 py-4 bg-surface-alt border-t border-default rounded-b-lg">
                            <button
                                onClick={() => setShowInfoModal(false)}
                                className="px-4 py-2 bg-primary text-white rounded-lg hover:bg-primary-700 transition-colors font-medium"
                            >
                                Got it
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default GroupAPIKeysTab;