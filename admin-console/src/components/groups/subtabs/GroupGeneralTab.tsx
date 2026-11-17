/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The General tab of a resource group, organized as sub-tabs:
 *
 *   * Identity — what this group IS: id, type, kind, name, description,
 *     icon, status.
 *   * Ownership & Purpose — the ISO 42001 governance
 *     declarations: owner, lifecycle stage, intended use/clients.
 *   * Hierarchy & Inheritance — where it sits in the tree and what it
 *     inherits (resources toggle; governance always inherits).
 *   * Audit Info — the shared audit panel (created/updated by, version):
 *     audit provenance belongs with the group's identity, not beside it.
 *
 * Edit mode is global across the first three sub-tabs: one Edit / Save
 * pair in the header edits whichever sub-tab is showing, and Save
 * writes the whole form — switching sub-tabs while editing keeps the
 * draft.
 */

import React, { useEffect, useState } from 'react';
import {
    Edit,
    Trash2,
    Save,
    X,
    AlertCircle,
    Copy,
    Check,
    User,
    Layers,
    ChevronRight,
    Info,
    FileClock,
    FolderSymlink,
    Fingerprint,
} from 'lucide-react';
import {
    ResourceGroup,
    GroupType,
    GROUP_TYPE_LABELS,
    LIFECYCLE_STAGES,
    LIFECYCLE_STAGE_LABELS,
    LifecycleStage,
    SystemKind,
    SYSTEM_KIND_LABELS,
    SYSTEM_KIND_DESCRIPTIONS,
    systemKindLabel, RollupExclusionReason, ROLLUP_EXCLUSION_REASON_LABELS, ROLLUP_EXCLUSION_TEMPORARY } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { resolveGroupIcon, resolveGroupColor, getIconEntry } from '../../../lib/lucideIconMap';
import { useTheme } from '../../../hooks/useTheme';
import LucideIconPicker from '../../common/LucideIconPicker';
import IconColorPicker from '../../common/IconColorPicker';
import MoveGroupModal from './MoveGroupModal';
import GroupAuditTab from './GroupAuditTab';
import { EU_AI_ACT_ROLE_LABEL } from '../../../lib/obligationTypes';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import { errorKind, enumValue, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runUserAction } from '../../../lib/asyncSafe';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
    onDelete: () => void;
    /** Deep-link target. The contract lifecycle gate links straight to
     *  'ownership' when it reports a missing owner or intended use, so the
     *  operator lands on the field to fix rather than on Identity. */
    initialSubTab?: GeneralSubTab;
}

type GeneralSubTab = 'identity' | 'ownership' | 'hierarchy' | 'audit';

const SUB_TABS: { id: GeneralSubTab; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { id: 'identity',  label: 'Identity',                icon: Fingerprint },
    { id: 'ownership', label: 'Ownership & Purpose',     icon: User },
    { id: 'hierarchy', label: 'Hierarchy & Inheritance', icon: Layers },
    { id: 'audit',     label: 'Audit Info',              icon: FileClock },
];

const GroupGeneralTab: React.FC<Props> = ({ group, onUpdate, onDelete, initialSubTab }) => {
    const { updateResourceGroup, deleteResourceGroup } = useGateway();
    const { theme } = useTheme();

    const [subTab, setSubTab] = useState<GeneralSubTab>(initialSubTab ?? 'identity');
    const [isEditing, setIsEditing] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [showMoveModal, setShowMoveModal] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [error, setError] = useState('');
    const [copiedId, setCopiedId] = useState(false);

    const [formData, setFormData] = useState({
        name: group.name,
        display_name: group.display_name || '',
        description: group.description || '',
        group_type: group.group_type || GroupType.CUSTOM,
        // Only sent for AI Systems. Defaults to AGENT so the
        // picker has a valid selection the moment the type is switched.
        system_kind: group.system_kind || SystemKind.AGENT,
        is_active: group.is_active,
        inherit_resources: group.inherit_resources ?? true,
        icon: group.icon || '',
        color: group.color || '',
        // Governance / ownership
        owner: group.owner || '',
        lifecycle_stage: (group.lifecycle_stage || '') as '' | LifecycleStage,
        rollup_exclusion: group.rollup_exclusion ?? null,
        eu_ai_act_role: group.eu_ai_act_role || '',
        intended_use: group.intended_use || '',
        intended_client_labels: (group.intended_client_labels || []).join(', '),
        intended_client_systems: group.intended_client_systems || [],
        enforce_accepted_callers: group.enforce_accepted_callers ?? false,
    });

    const handleEdit = () => {
        setIsEditing(true);
        setError('');
    };

    const handleCancel = () => {
        setFormData({
            name: group.name,
            display_name: group.display_name || '',
            description: group.description || '',
            group_type: group.group_type || GroupType.CUSTOM,
            system_kind: group.system_kind || SystemKind.AGENT,
            is_active: group.is_active,
            inherit_resources: group.inherit_resources ?? true,
            icon: group.icon || '',
            color: group.color || '',
            owner: group.owner || '',
            lifecycle_stage: (group.lifecycle_stage || '') as '' | LifecycleStage,
        rollup_exclusion: group.rollup_exclusion ?? null,
            eu_ai_act_role: group.eu_ai_act_role || '',
            intended_use: group.intended_use || '',
            intended_client_labels: (group.intended_client_labels || []).join(', '),
            intended_client_systems: group.intended_client_systems || [],
            enforce_accepted_callers: group.enforce_accepted_callers ?? false,
        });
        setIsEditing(false);
        setError('');
    };

    const handleSave = async () => {
        try {
            setIsSaving(true);
            setError('');

            await updateResourceGroup(group.id, {
                name: formData.name,
                display_name: formData.display_name || undefined,
                description: formData.description || undefined,
                group_type: enumValue(formData.group_type),
                // Send the kind only for AI Systems; explicit null on any other
                // type so demoting a system clears the now-meaningless kind
                // rather than leaving a stale value behind.
                system_kind: formData.group_type === GroupType.AI_SYSTEM && formData.system_kind
                    ? enumValue(formData.system_kind)
                    : null,
                is_active: formData.is_active,
                inherit_resources: formData.inherit_resources,
                icon: formData.icon || undefined,
                color: formData.color || undefined,
                // Governance / ownership. Send null (not undefined) to
                // clear — the control plane uses model_fields_set so an explicit
                // null reaches the DB.
                owner: formData.owner.trim() || null,
                lifecycle_stage: formData.lifecycle_stage || null,
                // What an exclusion is set with: who set it and when are
                // the Control Plane's to state, and are not sent back.
                rollup_exclusion: formData.rollup_exclusion
                    ? {
                          reason: formData.rollup_exclusion.reason,
                          note: formData.rollup_exclusion.note,
                          expires_at: formData.rollup_exclusion.expires_at ?? null,
                      }
                    : null,
                eu_ai_act_role: formData.eu_ai_act_role || null,
                intended_use: formData.intended_use.trim() || null,
                intended_client_labels: formData.intended_client_labels.trim()
                    ? formData.intended_client_labels.split(',').map((s) => s.trim()).filter(Boolean)
                    : null,
                // The serving side's acceptance of caller
                // systems. Only an AI System can be called as a service.
                intended_client_systems: formData.group_type === GroupType.AI_SYSTEM
                    && formData.intended_client_systems.length > 0
                    ? formData.intended_client_systems
                    : null,
                // AI Systems only (the control plane 422s it
                // on any other group type), so it is omitted — left unchanged —
                // everywhere else.
                ...(formData.group_type === GroupType.AI_SYSTEM
                    ? { enforce_accepted_callers: formData.enforce_accepted_callers }
                    : {}),
            });

            setIsEditing(false);
            onUpdate();
        } catch (err) {
            console.error('Failed to update group:', errorKind(err));
            setError(errorMessage(err, 'Failed to update group'));
        } finally {
            setIsSaving(false);
        }
    };

    const handleDelete = async () => {
        try {
            setIsDeleting(true);
            setDeleteError(null);

            await deleteResourceGroup(group.id);
            onDelete();
        } catch (err) {
            console.error('Failed to delete group:', errorKind(err));
            setDeleteError(errorMessage(err, 'Failed to delete group'));
            setIsDeleting(false);
        }
    };

    const copyToClipboard = (text: string) => {
        runUserAction(navigator.clipboard.writeText(text), 'write text');
        setCopiedId(true);
        setTimeout(() => setCopiedId(false), 2000);
    };

    // ─────────────────────────────────────────────────────────────────
    // Sub-tab: Identity
    // ─────────────────────────────────────────────────────────────────

    const renderIdentity = () => (
        <div className={`rounded-lg border ${isEditing ? 'border-primary-200 bg-primary-50' : 'border-default'} p-6`}>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-6">
                <div className="space-y-6">
                    {/* Group ID */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Group ID
                        </label>
                        <div className="flex items-center space-x-2">
                            <input
                                type="text"
                                value={group.id}
                                readOnly
                                className="flex-1 px-3 py-2 border border-strong rounded-lg bg-surface-alt text-secondary font-mono text-sm min-h-0 min-w-0"
                            />
                            <button
                                onClick={() => copyToClipboard(group.id)}
                                className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-lg transition-colors"
                                title="Copy ID"
                            >
                                {copiedId ? (
                                    <Check className="w-4 h-4 text-green-600" />
                                ) : (
                                    <Copy className="w-4 h-4" />
                                )}
                            </button>
                        </div>
                    </div>

                    {/* Group Type */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Group Type *
                        </label>
                        {isEditing ? (
                            <select
                                value={formData.group_type}
                                onChange={(e) => setFormData({ ...formData, group_type: e.target.value as GroupType })}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                            >
                                {Object.values(GroupType).map((type) => (
                                    <option key={type} value={type}>
                                        {GROUP_TYPE_LABELS[type]}
                                    </option>
                                ))}
                            </select>
                        ) : (
                            <div className="flex items-center space-x-3">
                                {(() => {
                                    const Icon = resolveGroupIcon(group.icon, group.group_type, group.system_kind);
                                    return <Icon className="w-5 h-5 text-secondary" />;
                                })()}
                                <span className="text-default">
                                    {GROUP_TYPE_LABELS[group.group_type || GroupType.CUSTOM]}
                                </span>
                            </div>
                        )}
                    </div>

                    {/* AI System kind. Rendered only for AI
                        Systems; meaningless (and rejected by the API) on any
                        other group type. */}
                    {formData.group_type === GroupType.AI_SYSTEM && (
                        <div data-testid="system-kind-field">
                            <label className="block text-sm font-medium text-secondary mb-2">
                                AI System Kind *
                            </label>
                            {isEditing ? (
                                <>
                                    <select
                                        aria-label="AI System Kind"
                                        value={formData.system_kind}
                                        onChange={(e) => setFormData({ ...formData, system_kind: e.target.value as SystemKind })}
                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                    >
                                        {Object.values(SystemKind).map((kind) => (
                                            <option key={kind} value={kind}>
                                                {SYSTEM_KIND_LABELS[kind]}
                                            </option>
                                        ))}
                                    </select>
                                    <p className="text-sm text-muted mt-1">
                                        {SYSTEM_KIND_DESCRIPTIONS[formData.system_kind]}
                                    </p>
                                </>
                            ) : (
                                <div className="flex items-center space-x-3">
                                    {(() => {
                                        const Icon = resolveGroupIcon(undefined, GroupType.AI_SYSTEM, group.system_kind);
                                        return <Icon className="w-5 h-5 text-secondary" />;
                                    })()}
                                    <span className="text-default">
                                        {systemKindLabel(group.system_kind)}
                                    </span>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Name */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Name *
                        </label>
                        {isEditing ? (
                            <input
                                type="text"
                                value={formData.name}
                                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                placeholder="e.g., Engineering, Frontend Team"
                            />
                        ) : (
                            <p className="text-default">{group.name}</p>
                        )}
                    </div>

                    {/* Display Name */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Display Name
                        </label>
                        {isEditing ? (
                            <input
                                type="text"
                                value={formData.display_name}
                                onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                placeholder="Optional friendly name"
                            />
                        ) : (
                            <p className="text-default">{group.display_name || '-'}</p>
                        )}
                    </div>

                    {/* Description */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Description
                        </label>
                        {isEditing ? (
                            <textarea
                                value={formData.description}
                                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                                rows={3}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface resize-none"
                                placeholder="Optional description"
                            />
                        ) : (
                            <p className="text-default whitespace-pre-wrap">{group.description || '-'}</p>
                        )}
                    </div>
                </div>

                <div className="space-y-6">
                    {/* Status */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Status
                        </label>
                        {isEditing ? (
                            <label className="flex items-center space-x-3 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={formData.is_active}
                                    onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                                    className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                                />
                                <span className="text-sm text-secondary">Group is active</span>
                            </label>
                        ) : (
                            <span className={`inline-flex px-3 py-1 text-sm font-medium rounded-full ${
                                group.is_active
                                    ? 'text-green-700 bg-surface-alt'
                                    : 'text-secondary bg-surface-raised'
                            }`}>
                                {group.is_active ? 'Active' : 'Inactive'}
                            </span>
                        )}
                    </div>

                    {/* Custom Icon */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Icon Override
                        </label>
                        {isEditing ? (
                            <LucideIconPicker
                                value={formData.icon}
                                onChange={(iconName) => setFormData({ ...formData, icon: iconName })}
                                groupType={formData.group_type}
                            />
                        ) : (
                            <div className="flex items-center space-x-3">
                                {(() => {
                                    const Icon = resolveGroupIcon(group.icon, group.group_type);
                                    return <Icon className="w-5 h-5 text-secondary" />;
                                })()}
                                <span className="text-default">
                                    {group.icon
                                        ? (getIconEntry(group.icon)?.label || group.icon)
                                        : 'Default (from group type)'}
                                </span>
                            </div>
                        )}
                    </div>

                    {/* Icon Color */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Icon Color
                        </label>
                        {isEditing ? (
                            <IconColorPicker
                                value={formData.color}
                                onChange={(colorKey) => setFormData({ ...formData, color: colorKey })}
                            />
                        ) : (
                            <div className="flex items-center space-x-3">
                                {(() => {
                                    const c = resolveGroupColor(group.color, theme);
                                    return (
                                        <>
                                            <div
                                                className="w-5 h-5 rounded-full"
                                                style={{ backgroundColor: c.fg }}
                                            />
                                            <span className="text-default">
                                                {group.color ? c.label : 'Default (Sky)'}
                                            </span>
                                        </>
                                    );
                                })()}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );

    // ─────────────────────────────────────────────────────────────────
    // Sub-tab: Ownership & Purpose (ISO 42001 declarations)
    // ─────────────────────────────────────────────────────────────────

    const renderOwnership = () => (
        <div className={`rounded-lg border ${isEditing ? 'border-primary-200 bg-primary-50' : 'border-default'} p-6`}>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-6">
                <div className="space-y-6">
                    {/* Owner (AI-System / governance attributes) */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Owner
                        </label>
                        {isEditing ? (
                            <input
                                type="text"
                                value={formData.owner}
                                onChange={(e) => setFormData({ ...formData, owner: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                placeholder="Accountable owner (person or team)"
                            />
                        ) : (
                            <p className="text-default">{group.owner || '-'}</p>
                        )}
                    </div>

                    {/* Lifecycle stage */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Lifecycle Stage
                        </label>
                        {isEditing ? (
                            <select
                                value={formData.lifecycle_stage}
                                onChange={(e) => setFormData({ ...formData, lifecycle_stage: e.target.value as '' | LifecycleStage })}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                            >
                                <option value="">Not set</option>
                                {LIFECYCLE_STAGES.map((s) => (
                                    <option key={s} value={s}>{LIFECYCLE_STAGE_LABELS[s]}</option>
                                ))}
                            </select>
                        ) : (
                            <p className="text-default">
                                {group.lifecycle_stage ? LIFECYCLE_STAGE_LABELS[group.lifecycle_stage] : '-'}
                            </p>
                        )}
                    </div>

                    {/* Estate rollup exclusion. Only AI Systems sit in the
                        Mission Control rollup, so only they can be excused from it. */}
                    {group.group_type === GroupType.AI_SYSTEM && (
                        <div data-testid="rollup-exclusion">
                            <label className="block text-sm font-medium text-secondary mb-2">
                                Estate rollup
                            </label>
                            {isEditing ? (
                                <div className="space-y-2">
                                    <label className="flex items-center gap-2 cursor-pointer">
                                        <input
                                            type="checkbox"
                                            checked={formData.rollup_exclusion !== null}
                                            data-testid="rollup-exclusion-toggle"
                                            onChange={(e) =>
                                                setFormData({
                                                    ...formData,
                                                    rollup_exclusion: e.target.checked
                                                        ? { reason: 'scheduled_offline', note: '', expires_at: null }
                                                        : null,
                                                })
                                            }
                                            className="h-4 w-4 text-primary-600 border-strong rounded"
                                        />
                                        <span className="text-sm text-default">
                                            Exclude from Mission Control&apos;s worst-of rollup
                                        </span>
                                    </label>
                                    {formData.rollup_exclusion && (
                                        <div className="pl-6 space-y-2">
                                            <select
                                                value={formData.rollup_exclusion.reason}
                                                data-testid="rollup-exclusion-reason"
                                                onChange={(e) =>
                                                    setFormData({
                                                        ...formData,
                                                        rollup_exclusion: {
                                                            ...formData.rollup_exclusion!,
                                                            reason: e.target.value as RollupExclusionReason,
                                                        },
                                                    })
                                                }
                                                className="w-full px-3 py-2 border border-strong rounded-lg bg-surface"
                                            >
                                                {(Object.keys(ROLLUP_EXCLUSION_REASON_LABELS) as RollupExclusionReason[]).map((r) => (
                                                    <option key={r} value={r}>{ROLLUP_EXCLUSION_REASON_LABELS[r]}</option>
                                                ))}
                                            </select>
                                            <input
                                                type="text"
                                                value={formData.rollup_exclusion.note}
                                                data-testid="rollup-exclusion-note"
                                                onChange={(e) =>
                                                    setFormData({
                                                        ...formData,
                                                        rollup_exclusion: { ...formData.rollup_exclusion!, note: e.target.value },
                                                    })
                                                }
                                                placeholder="Why — this appears on the report and in the digest"
                                                className="w-full px-3 py-2 border border-strong rounded-lg bg-surface"
                                            />
                                            <div className="flex items-center gap-2">
                                                <input
                                                    type="date"
                                                    value={(formData.rollup_exclusion.expires_at || '').slice(0, 10)}
                                                    data-testid="rollup-exclusion-expires"
                                                    onChange={(e) =>
                                                        setFormData({
                                                            ...formData,
                                                            rollup_exclusion: {
                                                                ...formData.rollup_exclusion!,
                                                                expires_at: e.target.value ? `${e.target.value}T00:00:00Z` : null,
                                                            },
                                                        })
                                                    }
                                                    className="px-3 py-2 border border-strong rounded-lg bg-surface"
                                                />
                                                <span className="text-xs text-muted">
                                                    {ROLLUP_EXCLUSION_TEMPORARY.includes(formData.rollup_exclusion.reason)
                                                        ? 'Expiry required — the system re-enters the rollup when it lapses.'
                                                        : 'Expiry optional.'}
                                                </span>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            ) : group.rollup_exclusion ? (
                                <p className="text-default">
                                    Excluded — {ROLLUP_EXCLUSION_REASON_LABELS[group.rollup_exclusion.reason] ?? group.rollup_exclusion.reason}
                                    {group.rollup_exclusion.expires_at
                                        ? ` until ${new Date(group.rollup_exclusion.expires_at).toLocaleDateString()}`
                                        : ''}
                                    {group.rollup_exclusion.note ? ` · ${group.rollup_exclusion.note}` : ''}
                                </p>
                            ) : (
                                <p className="text-default">Included in the rollup</p>
                            )}
                        </div>
                    )}

                    {/* EU AI Act regulated role. Obligations attach to
                        the (system, role) pair — Art 25 can flip a deployer
                        into a provider on substantial modification, which is
                        exactly what a contract-closure drift flags. */}
                    <div data-testid="eu-ai-act-role-field">
                        <label className="block text-sm font-medium text-secondary mb-2">
                            EU AI Act Role
                        </label>
                        {isEditing ? (
                            <>
                                <select
                                    aria-label="EU AI Act Role"
                                    value={formData.eu_ai_act_role}
                                    onChange={(e) => setFormData({ ...formData, eu_ai_act_role: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                >
                                    <option value="">Not declared</option>
                                    {Object.entries(EU_AI_ACT_ROLE_LABEL).map(([value, label]) => (
                                        <option key={value} value={value}>{label}</option>
                                    ))}
                                </select>
                                <p className="text-xs text-muted mt-1">
                                    Obligations attach to the (system, role) pair. A contract
                                    drift on this system is a substantial-modification signal —
                                    review the role when one fires (Art 25).
                                </p>
                            </>
                        ) : (
                            <p className="text-default">
                                {group.eu_ai_act_role
                                    ? EU_AI_ACT_ROLE_LABEL[group.eu_ai_act_role] ?? group.eu_ai_act_role
                                    : '-'}
                            </p>
                        )}
                    </div>
                </div>

                <div className="space-y-6">
                    {/* Intended use (ISO 42001) */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Intended Use
                        </label>
                        {isEditing ? (
                            <textarea
                                value={formData.intended_use}
                                onChange={(e) => setFormData({ ...formData, intended_use: e.target.value })}
                                rows={3}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface resize-none"
                                placeholder="ISO 42001 intended-use / purpose documentation"
                            />
                        ) : (
                            <p className="text-default whitespace-pre-wrap">{group.intended_use || '-'}</p>
                        )}
                    </div>

                    {/* Intended client labels (comma-separated) — non-system
                        callers: apps and channels identified by client_label. */}
                    <div data-testid="intended-client-labels-field">
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Intended Client Labels
                        </label>
                        {isEditing ? (
                            <input
                                type="text"
                                aria-label="Intended Client Labels"
                                value={formData.intended_client_labels}
                                onChange={(e) => setFormData({ ...formData, intended_client_labels: e.target.value })}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                placeholder="e.g., Goose, Internal Portal, Claude Code"
                            />
                        ) : (
                            <p className="text-default">
                                {group.intended_client_labels && group.intended_client_labels.length > 0
                                    ? group.intended_client_labels.join(', ')
                                    : '-'}
                            </p>
                        )}
                        {isEditing && (
                            <p className="text-xs text-muted mt-1">
                                Comma-separated client apps/channels (not AI Systems).
                            </p>
                        )}
                    </div>

                    {/* Intended client systems — the AI Systems this
                        one accepts as callers. A caller not listed here is an
                        "unaccepted caller" finding on this system. */}
                    {formData.group_type === GroupType.AI_SYSTEM && (
                        <>
                            <IntendedClientSystemsField
                                groupId={group.id}
                                editing={isEditing}
                                value={isEditing ? formData.intended_client_systems : group.intended_client_systems || []}
                                onChange={(ids) => setFormData({ ...formData, intended_client_systems: ids })}
                            />
                            <EnforceAcceptedCallersField
                                editing={isEditing}
                                value={isEditing ? formData.enforce_accepted_callers : group.enforce_accepted_callers ?? false}
                                acceptedCount={(isEditing ? formData.intended_client_systems : group.intended_client_systems || []).length}
                                onChange={(v) => setFormData({ ...formData, enforce_accepted_callers: v })}
                            />
                        </>
                    )}
                </div>
            </div>
        </div>
    );

    // ─────────────────────────────────────────────────────────────────
    // Sub-tab: Hierarchy & Inheritance
    // ─────────────────────────────────────────────────────────────────

    const renderHierarchy = () => (
        <div className="space-y-6">
            <div className={`rounded-lg border ${isEditing ? 'border-primary-200 bg-primary-50' : 'border-default'} p-6`}>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-6">
                    <div className="space-y-6">
                        {/* Hierarchy Level */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-2">
                                Hierarchy Level
                            </label>
                            <p className="text-default">Level {group.hierarchy_level}</p>
                        </div>

                        {/* Parent Group */}
                        {group.parent_group_id && (
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Parent Group
                                </label>
                                <p className="text-default">{group.parent_group_name || group.parent_group_id}</p>
                            </div>
                        )}

                        {/* Children Count */}
                        {group.children_count !== undefined && group.children_count > 0 && (
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Child Groups
                                </label>
                                <p className="text-default">{group.children_count} group(s)</p>
                            </div>
                        )}

                        {/* Hierarchy Path */}
                        {group.hierarchy_path && (
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Full Path
                                </label>
                                <p className="text-default text-sm font-mono bg-surface-alt px-3 py-2 rounded border border-default">
                                    {group.hierarchy_path}
                                </p>
                            </div>
                        )}
                    </div>

                    <div className="space-y-6">
                        {/* Resource Inheritance Setting */}
                        {group.parent_group_id && (
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Resource Inheritance
                                </label>
                                {isEditing ? (
                                    <label className="flex items-start space-x-3 cursor-pointer p-3 border border-strong rounded-lg hover:bg-surface-alt bg-surface">
                                        <input
                                            type="checkbox"
                                            checked={formData.inherit_resources}
                                            onChange={(e) => setFormData({ ...formData, inherit_resources: e.target.checked })}
                                            className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500 mt-0.5"
                                        />
                                        <div className="flex-1 min-h-0 min-w-0">
                                            <span className="text-sm font-medium text-default">
                                                Inherit resources from parent groups
                                            </span>
                                            <p className="text-xs text-secondary mt-1">
                                                When enabled, this group inherits LLM models, MCP servers, and agent skills from parent groups.
                                                When disabled, only directly assigned resources are available.
                                            </p>
                                            {!formData.inherit_resources && (
                                                <div className="mt-2 p-2 bg-surface-alt border border-default rounded text-xs text-amber-800">
                                                    ⚠️ Warning: Disabling resource inheritance means this group only has access to its directly assigned LLMs, MCPs, and skills.
                                                </div>
                                            )}
                                        </div>
                                    </label>
                                ) : (
                                    <div className="flex items-start space-x-3">
                                        {group.inherit_resources ? (
                                            <div className="flex items-start space-x-2 text-green-700">
                                                <Check className="w-5 h-5 flex-shrink-0 mt-0.5" />
                                                <div>
                                                    <span className="text-sm font-medium">Enabled</span>
                                                    <p className="text-xs text-secondary mt-1">
                                                        Inherits LLM models, MCP servers, and agent skills from parent groups
                                                    </p>
                                                </div>
                                            </div>
                                        ) : (
                                            <div className="flex items-start space-x-2">
                                                <AlertCircle className="w-5 h-5 flex-shrink-0 text-amber-600 mt-0.5" />
                                                <div>
                                                    <span className="text-sm font-medium text-amber-900">Disabled</span>
                                                    <p className="text-xs text-secondary mt-1">
                                                        Uses only directly assigned resources (LLMs, MCPs, skills)
                                                    </p>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* Governance Inheritance Notice */}
                                <div className="mt-3 p-2.5 bg-primary-pale border border-primary rounded-lg">
                                    <div className="flex items-start space-x-2">
                                        <Info className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                        <div>
                                            <span className="text-xs font-medium text-primary">Governance always inherits</span>
                                            <p className="text-xs text-primary mt-0.5">
                                                Guardrails, usage limits, and MCP capability restrictions are always inherited from parent groups
                                                and cannot be disabled. Guardrails are additive, limits are restrictive (minimum values apply).
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* Ancestors breadcrumb / root-group card */}
            {group.parent_group_id ? (
                <div className="border border-default rounded-lg p-4 bg-surface">
                    <div className="flex items-center space-x-2 mb-3">
                        <Layers className="w-4 h-4 text-secondary" />
                        <h4 className="text-sm font-semibold text-default">Hierarchy</h4>
                    </div>

                    {group.ancestors && group.ancestors.length > 0 ? (
                        <div className="flex flex-wrap items-center gap-2">
                            {group.ancestors.map((ancestor, index) => (
                                <React.Fragment key={ancestor.id}>
                                    {index > 0 && (
                                        <ChevronRight className="w-4 h-4 text-muted flex-shrink-0" />
                                    )}
                                    <div className="flex items-center space-x-2 px-3 py-1.5 bg-surface-alt border border-default rounded text-sm">
                                        <span className="text-default font-medium">{ancestor.name}</span>
                                        {ancestor.group_type && (
                                            <span className="text-xs text-muted">
                                                ({ancestor.group_type})
                                            </span>
                                        )}
                                    </div>
                                </React.Fragment>
                            ))}

                            <ChevronRight className="w-4 h-4 text-muted flex-shrink-0" />

                            {/* Current Group */}
                            <div className="flex items-center space-x-2 px-3 py-1.5 bg-primary-50 border-2 border-primary-300 rounded text-sm">
                                <span className="text-primary-900 font-semibold">{group.name}</span>
                                {group.group_type && (
                                    <span className="text-xs text-primary-700">
                                        ({group.group_type})
                                    </span>
                                )}
                            </div>
                        </div>
                    ) : (
                        <div className="flex items-center space-x-2 text-sm">
                            <span className="text-secondary">Parent:</span>
                            <span className="text-default font-medium">
                                {group.parent_group_name || group.parent_group_id}
                            </span>
                        </div>
                    )}
                </div>
            ) : (
                <div className="border border-primary bg-primary-pale rounded-lg p-4">
                    <div className="flex items-start space-x-3">
                        <Info className="w-5 h-5 text-primary flex-shrink-0 mt-0.5" />
                        <div>
                            <p className="text-sm font-medium text-primary">Root Group</p>
                            <p className="text-sm text-primary mt-1">
                                This is a root-level group with no parent groups.
                            </p>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );

    const renderSubTabContent = () => {
        switch (subTab) {
            case 'identity':  return renderIdentity();
            case 'ownership': return renderOwnership();
            case 'hierarchy': return renderHierarchy();
            // The audit panel (created/updated provenance + version), embedded.
            case 'audit':     return <GroupAuditTab group={group} onUpdate={onUpdate} embedded />;
            default:          return null;
        }
    };

    // Edit/Save applies to the three form sub-tabs; the audit sub-tab has
    // its own explicit "Save version" and would only be confused by a
    // second Save button above it.
    const showFormActions = subTab !== 'audit';

    return (
        <div className="p-6 space-y-4">
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

            {/* Header: title + actions */}
            <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold text-default">Group Information</h2>
                {showFormActions && (
                    <div className="flex items-center space-x-2">
                        {isEditing ? (
                            <>
                                <button
                                    onClick={handleCancel}
                                    disabled={isSaving}
                                    className="px-4 py-2 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                                >
                                    <X className="w-4 h-4 inline mr-2" />
                                    Cancel
                                </button>
                                <button
                                    onClick={asyncHandler(handleSave, 'click')}
                                    disabled={isSaving}
                                    className="px-4 py-2 text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50"
                                >
                                    <Save className="w-4 h-4 inline mr-2" />
                                    {isSaving ? 'Saving...' : 'Save Changes'}
                                </button>
                            </>
                        ) : (
                            <>
                                <button
                                    onClick={handleEdit}
                                    className="px-4 py-2 text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
                                >
                                    <Edit className="w-4 h-4 inline mr-2" />
                                    Edit
                                </button>
                                <button
                                    onClick={() => setShowMoveModal(true)}
                                    className="px-4 py-2 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                                >
                                    <FolderSymlink className="w-4 h-4 inline mr-2" />
                                    Move
                                </button>
                                <button
                                    onClick={() => setShowDeleteConfirm(true)}
                                    className="px-4 py-2 text-white bg-red-600 rounded-lg hover:bg-red-700 transition-colors"
                                >
                                    <Trash2 className="w-4 h-4 inline mr-2" />
                                    Delete
                                </button>
                            </>
                        )}
                    </div>
                )}
            </div>

            {/* Sub-tab bar */}
            <div className="flex border-b border-default" role="tablist" aria-label="General sections">
                {SUB_TABS.map(({ id, label, icon: Icon }) => {
                    const active = subTab === id;
                    return (
                        <button
                            key={id}
                            role="tab"
                            aria-selected={active}
                            data-testid={`general-subtab-${id}`}
                            onClick={() => setSubTab(id)}
                            className={`flex items-center gap-2 px-4 py-2.5 border-b-2 text-sm font-medium transition-colors whitespace-nowrap ${
                                active
                                    ? 'border-primary text-primary'
                                    : 'border-transparent text-secondary hover:text-default hover:border-strong'
                            }`}
                        >
                            <Icon className="w-4 h-4" />
                            <span>{label}</span>
                            {/* Draft marker: editing spans the form sub-tabs; a dot
                                reminds the operator a switched-away tab holds edits. */}
                            {isEditing && id !== 'audit' && (
                                <span className="w-1.5 h-1.5 rounded-full bg-primary-500" aria-hidden="true" />
                            )}
                        </button>
                    );
                })}
            </div>

            {renderSubTabContent()}

            {/* Delete Confirmation Modal */}
            {showDeleteConfirm && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => {
                        setShowDeleteConfirm(false);
                        setDeleteError(null);
                    }}
                    onConfirm={asyncHandler(handleDelete, 'confirm')}
                    itemType="group"
                    itemName={group.name}
                    itemInfo={{
                        id: group.id,
                        description: group.description || group.display_name || 'Resource group',
                        enabled: group.is_active,
                    }}
                    customWarning={
                        group.children_count && group.children_count > 0
                            ? `This group has ${group.children_count} child group(s) that will also be deleted.`
                            : undefined
                    }
                    isDeleting={isDeleting}
                    errorMessage={deleteError}
                />
            )}

            {/* Move Group Modal */}
            {showMoveModal && (
                <MoveGroupModal
                    group={group}
                    onMove={() => {
                        setShowMoveModal(false);
                        onUpdate();
                    }}
                    onClose={() => setShowMoveModal(false)}
                />
            )}
        </div>
    );
};

/**
 * Picker for the AI Systems in this tenant that may call this one as a
 * shared service (`intended_client_systems`). Declaring a
 * dependency on the caller's side does not accept the caller here — both
 * sides declare, and a mismatch is a finding on the side that is missing it.
 */
function IntendedClientSystemsField({
    groupId,
    editing,
    value,
    onChange,
}: {
    groupId: string;
    editing: boolean;
    value: string[];
    onChange: (ids: string[]) => void;
}) {
    const { fetchAISystems } = useGateway();
    const [systems, setSystems] = useState<{ id: string; name: string }[] | null>(null);
    const [readFailure, reads] = useReadFailures<'systems'>();
    const [systemsAsked, setSystemsAsked] = useState(0);

    useEffect(() => {
        let cancelled = false;
        void (async () => {
            try {
                const res = await fetchAISystems({ limit: 500 });
                if (!cancelled) {
                    setSystems(
                        res.systems
                            .filter((sys) => sys.id !== groupId)
                            .map((sys) => ({ id: sys.id, name: sys.display_name || sys.name })),
                    );
                    reads.loaded('systems');
                }
            } catch (err) {
                if (cancelled) return;
                setSystems([]);
                reads.lost('systems', 'Failed to load the AI Systems', err);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [fetchAISystems, groupId, systemsAsked, reads]);

    const nameOf = (id: string) => systems?.find((sys) => sys.id === id)?.name ?? id;
    const toggle = (id: string) =>
        onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id]);

    return (
        <div data-testid="intended-client-systems-field">
            <label className="block text-sm font-medium text-secondary mb-2" id="intended-client-systems-label">
                Intended Client Systems
            </label>
            {editing ? (
                <>
                    <div
                        role="group"
                        aria-labelledby="intended-client-systems-label"
                        className="max-h-40 overflow-y-auto rounded-lg border border-strong bg-surface p-2 space-y-1"
                    >
                        {systems === null ? (
                            <p className="text-xs text-muted">Loading AI Systems…</p>
                        ) : readFailure('systems') ? (
                            <ReadFailureNotice compact failure={readFailure('systems')} onRetry={() => setSystemsAsked((n) => n + 1)} />
                        ) : systems.length === 0 ? (
                            <p className="text-xs text-muted">No other AI Systems in this tenant.</p>
                        ) : (
                            systems.map((sys) => (
                                <label key={sys.id} className="flex items-center gap-2 text-sm text-default">
                                    <input
                                        type="checkbox"
                                        checked={value.includes(sys.id)}
                                        onChange={() => toggle(sys.id)}
                                        data-testid={`intended-client-system-${sys.id}`}
                                    />
                                    {sys.name}
                                </label>
                            ))
                        )}
                    </div>
                    <p className="text-xs text-muted mt-1">
                        AI Systems that may call this one as a shared service. A caller not
                        listed here shows as an unaccepted caller.
                    </p>
                </>
            ) : (
                <p className="text-default" data-testid="intended-client-systems-value">
                    {value.length > 0 ? value.map(nameOf).join(', ') : '-'}
                </p>
            )}
        </div>
    );
}

/**
 * Per service, turn the accepted-caller list from an
 * advisory finding into runtime enforcement. When on, the gateway refuses an
 * A2A call to this system from any calling AI System not listed above
 * (before the upstream is contacted; HTTP 403, proxy subtype
 * `unaccepted_caller`, audited). "Caller" is the IMMEDIATE calling system.
 */
function EnforceAcceptedCallersField({
    editing,
    value,
    acceptedCount,
    onChange,
}: {
    editing: boolean;
    value: boolean;
    acceptedCount: number;
    onChange: (v: boolean) => void;
}) {
    const explanation = value
        ? 'Enforced: the gateway refuses an A2A call to this system from any AI System not listed above — before it reaches the agent — with HTTP 403 (unaccepted_caller), and audits the refusal. Callers are the immediate calling system.'
        : 'Advisory: an unlisted calling AI System is still served, and shows as an "unaccepted caller" finding on this system.';
    return (
        <div data-testid="enforce-accepted-callers-field">
            {editing ? (
                <label className="flex items-start gap-2 text-sm text-default">
                    <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={value}
                        onChange={(e) => onChange(e.target.checked)}
                        data-testid="enforce-accepted-callers"
                    />
                    <span>
                        <span className="font-medium">Refuse callers not listed</span>
                        <span className="block text-xs text-muted mt-0.5">{explanation}</span>
                        {value && acceptedCount === 0 && (
                            <span
                                className="block text-xs text-amber-800 mt-1"
                                data-testid="enforce-accepted-callers-empty"
                            >
                                No client systems are listed: every calling AI System will be refused.
                            </span>
                        )}
                    </span>
                </label>
            ) : (
                <>
                    <span className="block text-sm font-medium text-secondary mb-1">Caller enforcement</span>
                    <p className="text-default" data-testid="enforce-accepted-callers-value">
                        {value ? 'Enforced — only listed callers' : 'Advisory'}
                    </p>
                    <p className="text-xs text-muted mt-1">{explanation}</p>
                </>
            )}
        </div>
    );
}

export default GroupGeneralTab;
