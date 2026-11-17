/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Plus,
    Loader,
    Package,
    Search,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { ResourceGroup } from '../../lib/groupTypes';
import GroupDetailsPanel from './GroupDetailsPanel';
import type { TabType } from './GroupDetailsPanel';
import CreateGroupModal from './CreateGroupModal';
import { errorKind, errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

// ============================================================================
// Props
// ============================================================================

interface ResourceGroupsTabProps {
    /** Currently selected group ID (from sidebar tree) */
    selectedGroupId?: string;
    /** Whether to open the create-group modal immediately */
    showCreateModal?: boolean;
    /** Deep-link detail tab (e.g. 'usage' from Mission Control's
     *  "Open usage details"). */
    initialDetailTab?: TabType;
    /** Sub-section within the General tab, for deep links that target one
     *  field (e.g. the lifecycle gate's "owner is named" → 'ownership'). */
    initialGeneralSection?: 'identity' | 'ownership' | 'hierarchy' | 'audit';
    /** Notify App that the group tree changed (create / update / delete) */
    onGroupTreeChanged: () => void;
    /** Navigate sidebar to a specific group */
    onNavigateToGroup: (groupId: string) => void;
}

// ============================================================================
// Component
// ============================================================================

const ResourceGroupsTab: React.FC<ResourceGroupsTabProps> = ({
    selectedGroupId,
    showCreateModal: showCreateProp,
    initialDetailTab,
    initialGeneralSection,
    onGroupTreeChanged,
    onNavigateToGroup,
}) => {
    const { fetchResourceGroup } = useGateway();

    const [group, setGroup] = useState<ResourceGroup | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [createModalOpen, setCreateModalOpen] = useState(showCreateProp ?? false);

    // Sync external create-modal trigger
    useEffect(() => {
        if (showCreateProp) setCreateModalOpen(true);
    }, [showCreateProp]);

    // ── Load selected group details ─────────────────────────────────────────
    useEffect(() => {
        if (!selectedGroupId) {
            setGroup(null);
            return;
        }

        let cancelled = false;
        const load = async () => {
            try {
                setLoading(true);
                setError('');
                const data = await fetchResourceGroup(selectedGroupId, {
                    stats: true,
                    ancestors: true,
                    children: true,
                });
                if (!cancelled) setGroup(data);
            } catch (err) {
                if (!cancelled) {
                    console.error('Failed to load group details:', errorKind(err));
                    setError(errorMessage(err, 'Failed to load group details'));
                }
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        runBackground(load(), 'load');
        return () => { cancelled = true; };
    }, [selectedGroupId]);

    // ── Callbacks from GroupDetailsPanel ─────────────────────────────────────
    const handleUpdate = async () => {
        onGroupTreeChanged();
        // Re-fetch current group to pick up changes
        if (selectedGroupId) {
            try {
                const data = await fetchResourceGroup(selectedGroupId, {
                    stats: true,
                    ancestors: true,
                    children: true,
                });
                setGroup(data);
            } catch { /* ignore — tree refresh is more important */ }
        }
    };

    const handleDelete = () => {
        onGroupTreeChanged();
        setGroup(null);
        // Navigate to the groups landing (no group selected)
        onNavigateToGroup('');
    };

    const handleGroupCreated = (newGroupId?: string) => {
        setCreateModalOpen(false);
        onGroupTreeChanged();
        if (newGroupId) {
            onNavigateToGroup(newGroupId);
        }
    };

    // ── Loading state ───────────────────────────────────────────────────────
    if (loading) {
        return (
            <div className="flex items-center justify-center h-full">
                <div className="text-center">
                    <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                    <p className="text-sm text-secondary">Loading group details...</p>
                </div>
            </div>
        );
    }

    // ── Error state ─────────────────────────────────────────────────────────
    if (error) {
        return (
            <div className="flex items-center justify-center h-full">
                <div className="text-center">
                    <div className="w-14 h-14 bg-surface-alt rounded-full flex items-center justify-center mx-auto mb-4">
                        <Package className="w-7 h-7 text-red-400" />
                    </div>
                    <p className="text-lg font-medium text-secondary mb-2">Failed to Load Group</p>
                    <p className="text-sm text-muted max-w-sm">{error}</p>
                </div>
            </div>
        );
    }

    // ── Group selected → full-width detail panel ────────────────────────────
    if (group) {
        return (
            <>
                <GroupDetailsPanel
                    group={group}
                    onUpdate={asyncHandler(handleUpdate, 'update')}
                    onDelete={handleDelete}
                    initialTab={initialDetailTab}
                    initialGeneralSection={initialGeneralSection}
                />
                {createModalOpen && (
                    <CreateGroupModal
                        onClose={() => setCreateModalOpen(false)}
                        onSuccess={handleGroupCreated}
                    />
                )}
            </>
        );
    }

    // ── No group selected → empty state ─────────────────────────────────────
    return (
        <>
            <div className="flex items-center justify-center h-full">
                <div className="text-center text-muted">
                    <div className="w-16 h-16 bg-surface-raised rounded-full flex items-center justify-center mx-auto mb-4">
                        <Search className="w-8 h-8 text-muted" />
                    </div>
                    <p className="text-lg font-medium mb-2">Select a Resource Group</p>
                    <p className="text-sm max-w-sm mx-auto mb-6">
                        Choose a group from the sidebar tree, or create a new one to get started.
                    </p>
                    <button
                        onClick={() => setCreateModalOpen(true)}
                        className="inline-flex items-center space-x-2 px-5 py-2.5 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                    >
                        <Plus className="w-4 h-4" />
                        <span>New Group</span>
                    </button>
                </div>
            </div>
            {createModalOpen && (
                <CreateGroupModal
                    onClose={() => setCreateModalOpen(false)}
                    onSuccess={handleGroupCreated}
                />
            )}
        </>
    );
};

export default ResourceGroupsTab;
