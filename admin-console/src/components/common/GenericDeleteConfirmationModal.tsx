/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import type { InUseRefusal } from '../../lib/inUseRefusal';
import InUseList from './InUseList';
import { AlertTriangle, Trash2, X, XCircle, Server, Brain, Settings, Key, User, UserMinus, Network, Sparkles, Layers, ShieldCheck, Monitor, FileText, BookOpen, Building2, Download } from 'lucide-react';

type ItemType = 'server' | 'model' | 'config' | 'client' | 'virtual-server' | 'member' | 'end-user' | 'routing-group' | 'skill' | 'group' | 'guardrail' | 'argument-policy' | 'semantic-policy' | 'machine' | 'document' | 'documents' | 'collection' | 'tenant' | 'audit-export-destination' | 'kb-connector';
type ActionType = 'delete' | 'remove';

interface ItemInfo {
    id: string;
    description?: string;
    enabled?: boolean;
    provider?: string;
    model_name?: string;
    [key: string]: unknown;
}

interface DeleteConfirmationModalProps {
    isOpen: boolean;
    onClose: () => void;
    onConfirm: () => void;
    itemType: ItemType;
    itemName: string;
    itemInfo?: ItemInfo;
    isDeleting?: boolean;
    customWarning?: string;
    customNote?: string;
    action?: ActionType;  // 'delete' (default) or 'remove'
    errorMessage?: string | null;  // Server-side error to display inline
    /** What uses the item, when the Control Plane refused the delete for that reason: listed under the error. */
    inUse?: InUseRefusal | null;
    skipConfirmText?: boolean;  // Skip typed confirmation (for bulk deletes)
    confirmWord?: string;  // Override the word user must type to confirm (default: itemName)
}

const ItemTypeConfig = {
    server: {
        label: 'Server',
        icon: Server,
        warningTitle: 'You are about to permanently delete this server',
        warningMessage: 'This will also cascade-delete the server\'s linked Server Configuration (capability allowlist, OAuth tool gating, resource-group bindings, per-tool rate limits, and the deployment record if any). If the server was deployed via a catalog import, the underlying container, the Docker/Kubernetes/ECS deployment row, and any cached tool definitions are removed too. This cannot be recovered.',
        additionalNote: 'If this MCP server is referenced by any Virtual servers or Server Access configurations, you may need to update those before deletion.',
    },
    model: {
        label: 'LLM Model',
        icon: Brain,
        warningTitle: 'You are about to permanently delete this LLM model',
        warningMessage: 'This will remove the model configuration, API credentials, and performance history.',
        additionalNote: 'If this model is part of a routing group or referenced by client configurations, those references will be removed.',
    },
    config: {
        label: 'Configuration',
        icon: Settings,
        warningTitle: 'You are about to permanently delete this MCP server configuration',
        warningMessage: 'This will remove the configuration and cannot be recovered.',
        additionalNote: 'Ensure this configuration is not actively being used before deletion.',
    },
    client: {
        label: 'OAuth Client',
        icon: Key,
        warningTitle: 'You are about to permanently delete this OAuth App client',
        warningMessage: 'This will remove the client configuration and all associated credentials.',
        additionalNote: 'Any server configurations using this OAuth client will need to be updated.',
    },
    'virtual-server': {
        label: 'Virtual Server',
        icon: Server,
        warningTitle: 'You are about to permanently delete this virtual server',
        warningMessage: 'This will remove the virtual server aggregation and cannot be recovered.',
        additionalNote: 'The underlying physical servers will not be affected.',
    },
    member: {
        label: 'Member',
        icon: User,
        warningTitle: 'You are about to remove this member from the resource group',
        warningMessage: 'This will remove the user from the group and revoke their access to group resources.',
        additionalNote: 'The user account will not be deleted, only their group membership.',
    },
    'end-user': {
        label: 'End User',
        icon: User,
        warningTitle: 'You are about to permanently delete this end-user',
        warningMessage: 'This will permanently delete the user account and all associated data including memberships in resource groups.',
        additionalNote: 'This action cannot be undone. All of the user\'s data will be permanently deleted.',
    },
    'routing-group': {
        label: 'Routing Group',
        icon: Network,
        warningTitle: 'You are about to permanently delete this routing group',
        warningMessage: 'This will remove the routing group configuration, its virtual model, and all load balancing settings. Any assigned models will be automatically disconnected.',
        additionalNote: 'Assigned models will not be deleted — they will become standalone models with no routing group. The associated virtual model will also be removed.',
    },
    skill: {
        label: 'Agent Skill',
        icon: Sparkles,
        warningTitle: 'You are about to permanently delete this agent skill',
        warningMessage: 'This will remove the skill definition, its SKILL.md content, and execution history.',
        additionalNote: 'Published skills should be deprecated or archived instead of deleted. Resource group bindings referencing this skill will be removed.',
    },
    group: {
        label: 'Resource Group',
        icon: Layers,
        warningTitle: 'You are about to permanently delete this resource group',
        warningMessage: 'This will delete the group, all child groups, and remove all member associations.',
        additionalNote: 'AI model assignments, MCP server bindings, API keys, and rate limits associated with this group will also be removed.',
    },
    guardrail: {
        label: 'Guardrail Config',
        icon: ShieldCheck,
        warningTitle: 'You are about to permanently delete this guardrail configuration',
        warningMessage: 'This will remove all guardrail settings, built-in checks, provider configurations, and resource group associations for this configuration.',
        additionalNote: 'If this is the only active guardrail configuration, content filtering will be disabled for the affected resource groups.',
    },
    'argument-policy': {
        label: 'Argument Policy',
        icon: ShieldCheck,
        warningTitle: 'You are about to permanently delete this argument policy',
        warningMessage: 'This will remove the AST-aware rule set and unbind it from every resource group that referenced it.',
        additionalNote: 'Any tool/skill argument the policy was protecting becomes unrestricted unless another policy covers it.',
    },
    'semantic-policy': {
        label: 'Semantic Policy',
        icon: ShieldCheck,
        warningTitle: 'You are about to permanently delete this semantic policy',
        warningMessage: 'This will remove the natural-language constraint and unbind it from every resource group that referenced it.',
        additionalNote: 'Requests the policy was judging become ungoverned by it unless another policy covers the same intent.',
    },
    machine: {
        label: 'Deployment Machine',
        icon: Monitor,
        warningTitle: 'You are about to permanently remove this deployment machine',
        warningMessage: 'This will remove the machine configuration from the gateway. Running models on the remote host will not be stopped.',
        additionalNote: 'Any LLM models registered from this machine will be disabled (not deleted). You can re-add the machine later.',
    },
    document: {
        label: 'KB Document',
        icon: FileText,
        warningTitle: 'You are about to permanently delete this document',
        warningMessage: 'This will remove the document, all its chunks, and vector embeddings from the collection.',
        additionalNote: 'The document will need to be re-uploaded and re-indexed if needed later.',
    },
    documents: {
        label: 'KB Documents',
        icon: FileText,
        warningTitle: 'You are about to permanently delete multiple documents',
        warningMessage: 'This will remove all selected documents, their chunks, and vector embeddings from the collection.',
        additionalNote: 'These documents will need to be re-uploaded and re-indexed if needed later.',
    },
    collection: {
        label: 'KB Collection',
        icon: BookOpen,
        warningTitle: 'You are about to permanently delete this knowledge base collection',
        warningMessage: 'This will remove the collection, all its documents, text chunks, and vector embeddings from Qdrant.',
        additionalNote: 'Any resource groups using this collection will lose access to its documents for RAG retrieval.',
    },
    tenant: {
        label: 'Tenant',
        icon: Building2,
        warningTitle: 'You are about to permanently delete this tenant',
        warningMessage: 'This will permanently delete the tenant and ALL of its data including users, AI models, MCP servers, guardrails, policies, knowledge base, and all other resources.',
        additionalNote: 'This action cannot be undone. All tenant data will be permanently deleted.',
    },
    'audit-export-destination': {
        label: 'Export Destination',
        icon: Download,
        warningTitle: 'You are about to delete this audit export destination',
        warningMessage: 'This permanently removes the destination configuration (schedule + credentials). Future exports stop. Past exports already shipped to S3 / SIEM / the webhook collector are NOT deleted — only the schedule is.',
        additionalNote: 'If you want to pause exports temporarily, edit the destination and uncheck Enabled instead of deleting.',
    },
    'kb-connector': {
        label: 'KB Connector',
        icon: BookOpen,
        warningTitle: 'You are about to delete this KB connector',
        warningMessage: 'This removes the connector configuration and stored credentials. Already-indexed content stays in the KB collection — only the pull-ingest scheduler stops.',
        additionalNote: 'If you only want to pause syncs, edit the connector and toggle Enabled off instead.',
    },
};

const GenericDeleteConfirmationModal: React.FC<DeleteConfirmationModalProps> = ({
                                                                                    isOpen,
                                                                                    onClose,
                                                                                    onConfirm,
                                                                                    itemType,
                                                                                    itemName,
                                                                                    itemInfo,
                                                                                    isDeleting = false,
                                                                                    customWarning,
                                                                                    customNote,
                                                                                    action = 'delete',  // default to 'delete'
                                                                                    errorMessage,
                                                                                    inUse,
                                                                                    skipConfirmText = false,
                                                                                    confirmWord,
                                                                                }) => {
    const [confirmText, setConfirmText] = useState('');
    const expectedText = confirmWord || itemName;
    const isConfirmDisabled = skipConfirmText ? false : confirmText !== expectedText;

    // Reset confirmation text when modal opens or item changes
    useEffect(() => {
        if (isOpen) setConfirmText('');
    }, [isOpen, itemName]);

    const config = ItemTypeConfig[itemType];
    const Icon = config.icon;

    // Determine action-specific text
    const isRemove = action === 'remove';
    const actionVerb = isRemove ? 'Remove' : 'Delete';
    const actionVerbLower = isRemove ? 'remove' : 'delete';
    const actionVerbGerund = isRemove ? 'Removing' : 'Deleting';
    const ActionIcon = isRemove ? UserMinus : Trash2;

    if (!isOpen) return null;

    const handleConfirm = () => {
        if (!isConfirmDisabled) {
            onConfirm();
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !isConfirmDisabled) {
            handleConfirm();
        } else if (e.key === 'Escape') {
            onClose();
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-lg w-full animate-scale-in">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-red-100 bg-red-50">
                    <div className="flex items-center space-x-3">
                        <div className="w-12 h-12 bg-red-100 rounded-full flex items-center justify-center">
                            <AlertTriangle className="w-6 h-6 text-red-600" />
                        </div>
                        <div>
                            <h2 className="text-xl font-semibold text-red-900">
                                {actionVerb} {config.label}
                            </h2>
                            <p className="text-sm text-red-700 mt-0.5">
                                {isRemove ? 'Remove from group' : 'This action cannot be undone'}
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        disabled={isDeleting}
                        className="p-2 hover:bg-red-100 rounded-md transition-colors disabled:opacity-50"
                    >
                        <X className="w-5 h-5 text-red-600" />
                    </button>
                </div>

                {/* Content */}
                <div className="p-6 space-y-4">
                    {/* Warning Message */}
                    <div className="bg-red-50 border border-red-200 rounded-lg p-4">
                        <div className="flex items-start space-x-3">
                            <ActionIcon className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                            <div className="flex-1 min-h-0 min-w-0">
                                <p className="text-red-900 font-medium mb-1">
                                    {config.warningTitle}
                                </p>
                                <p className="text-sm text-red-800">
                                    {customWarning || config.warningMessage}
                                </p>
                            </div>
                        </div>
                    </div>

                    {/* Item Info */}
                    <div className="bg-surface-alt border border-default rounded-lg p-4">
                        <div className="flex items-start space-x-3">
                            <Icon className="w-5 h-5 text-secondary mt-0.5" />
                            <div className="flex-1 min-w-0 min-h-0">
                                <div className="font-semibold text-default mb-1 break-all">
                                    {itemName}
                                </div>
                                {itemInfo?.id && (
                                    <div className="text-sm text-secondary mb-1">
                                        ID: <code className="text-xs bg-surface-inset px-1 py-0.5 rounded">{itemInfo.id}</code>
                                    </div>
                                )}
                                {itemInfo?.provider && (
                                    <div className="text-sm text-secondary mb-1">
                                        Provider: <span className="font-medium">{itemInfo.provider}</span>
                                    </div>
                                )}
                                {itemInfo?.model_name && (
                                    <div className="text-sm text-secondary mb-1">
                                        Model: <code className="text-xs bg-surface-inset px-1 py-0.5 rounded">{itemInfo.model_name}</code>
                                    </div>
                                )}
                                {itemInfo?.description && (
                                    <div className="text-sm text-secondary">
                                        {itemInfo.description}
                                    </div>
                                )}
                                {itemInfo?.enabled !== undefined && (
                                    <div className="mt-2">
                                        <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                                            itemInfo.enabled
                                                ? 'bg-green-100 text-green-800'
                                                : 'bg-surface-raised text-default'
                                        }`}>
                                            {itemInfo.enabled ? 'Enabled' : 'Disabled'}
                                        </span>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>

                    {/* Confirmation Input */}
                    {!skipConfirmText && (
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Type <span className="font-bold text-red-600">{expectedText}</span> to confirm {actionVerbLower}
                        </label>
                        <input
                            type="text"
                            value={confirmText}
                            onChange={(e) => setConfirmText(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder={`Enter "${expectedText}"`}
                            disabled={isDeleting}
                            autoFocus
                            className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 transition-colors ${
                                confirmText && isConfirmDisabled
                                    ? 'border-red-300 focus:ring-red-500 focus:border-red-500'
                                    : confirmText && !isConfirmDisabled
                                        ? 'border-green-300 focus:ring-green-500 focus:border-green-500'
                                        : 'border-strong focus:ring-primary focus:border-primary'
                            }`}
                        />
                        {confirmText && isConfirmDisabled && (
                            <p className="text-sm text-red-600 mt-1">
                                {config.label} name does not match
                            </p>
                        )}
                    </div>
                    )}

                    {/* Additional Warning */}
                    <div className="flex items-start space-x-2 text-sm text-secondary bg-surface-alt p-3 rounded-md">
                        <AlertTriangle className="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
                        <p>
                            <strong>Note:</strong> {customNote || config.additionalNote}
                        </p>
                    </div>

                    {/* Server-side error message */}
                    {errorMessage && (
                        <div className="bg-red-50 border border-red-300 rounded-lg p-4">
                            <div className="flex items-start space-x-3">
                                <XCircle className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <p className="text-sm font-semibold text-red-900 mb-1">
                                        {actionVerb} failed
                                    </p>
                                    <p className="text-sm text-red-800">
                                        {errorMessage}
                                    </p>
                                    {inUse && inUse.groups.length > 0 && (
                                        <div className="mt-3">
                                            <InUseList groups={inUse.groups} />
                                        </div>
                                    )}
                                </div>
                            </div>
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="flex justify-end space-x-3 p-6 border-t bg-surface-alt">
                    <button
                        onClick={onClose}
                        disabled={isDeleting}
                        className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={handleConfirm}
                        disabled={isConfirmDisabled || isDeleting}
                        className={`px-4 py-2 rounded-md transition-colors flex items-center space-x-2 ${
                            isConfirmDisabled || isDeleting
                                ? 'bg-surface-inset text-muted cursor-not-allowed'
                                : 'bg-red-600 text-white hover:bg-red-700'
                        }`}
                    >
                        {isDeleting ? (
                            <>
                                <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                                <span>{actionVerbGerund}...</span>
                            </>
                        ) : (
                            <>
                                <ActionIcon className="w-4 h-4" />
                                <span>{actionVerb} {config.label}</span>
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default GenericDeleteConfirmationModal;