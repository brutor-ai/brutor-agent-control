/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect } from 'react';
import {
    Edit,
    Trash2,
    Power,
    PowerOff,
    Activity,
    MoreVertical,
    Copy,
    BarChart3,
    ExternalLink,
    Share2,
} from 'lucide-react';
import { LLModelConfig, formatModelParameters, isRoutingModel, getRoutingGroupId } from '../../../lib/llmTypes';
import LLModelStatusBadge from './LLModelStatusBadge';
import LLModelProbeTypeBadge from './LLModelProbeTypeBadge';
import LLModelNotServedBadge from './LLModelNotServedBadge';
import { ProviderLogo } from './providerLogos';
import LLModelStatsModal from './LLModelStatsModal';
import GroupTreeDisplay from '../../common/GroupTreeDisplay';

interface LLModelListItemProps {
    model: LLModelConfig;
    onEdit: () => void;
    onDelete: () => void;
    onCopy: () => void;
    onTestConnection: () => void;
    onToggleEnabled: (enabled: boolean) => void;
    onNavigateToRoutingGroup?: (routingGroupId: string) => void;
    isTesting: boolean;
    /**
     * When true the row is rendered with a primary-coloured ring +
     * brief pulse so the operator can immediately spot the model that
     * a Mission Control alert deep-linked them to. Selection lives in
     * LLModelTab's selectedModelId state.
     */
    isFocused?: boolean;
}

const LLModelListItem: React.FC<LLModelListItemProps> = ({
                                                             model,
                                                             onEdit,
                                                             onDelete,
                                                             onCopy,
                                                             onTestConnection,
                                                             onToggleEnabled,
                                                             onNavigateToRoutingGroup,
                                                             isTesting,
                                                             isFocused = false,
                                                         }) => {
    const [showMenu, setShowMenu] = useState(false);
    const [showStats, setShowStats] = useState(false);
    const [menuPosition, setMenuPosition] = useState<'bottom' | 'top'>('bottom');
    const menuButtonRef = useRef<HTMLDivElement>(null);

    // Detect if menu should open upward or downward
    useEffect(() => {
        if (showMenu && menuButtonRef.current) {
            const rect = menuButtonRef.current.getBoundingClientRect();
            const spaceBelow = window.innerHeight - rect.bottom;
            const spaceAbove = rect.top;

            // If less than 200px below, open upward
            if (spaceBelow < 200 && spaceAbove > spaceBelow) {
                setMenuPosition('top');
            } else {
                setMenuPosition('bottom');
            }
        }
    }, [showMenu]);

    const healthStatus = model.health_status?.status || 'unknown';
    const successRate = model.performance_metrics?.success_rate || 0;
    const avgLatency = model.health_status?.avg_latency_ms;
    const totalRequests = model.performance_metrics?.total_requests || 0;
    const isRouting = isRoutingModel(model);
    const routingGroupId = getRoutingGroupId(model);

    return (
        <>
            <div
                /* `data-llm-model-id` is the deep-link anchor used by
                   Mission Control's System-Alerts row. Clicking an
                   alert with `resource_type === 'llm_model'` navigates
                   to this tab and scrolls the matching row into view
                   via querySelector(`[data-llm-model-id="…"]`). Don't
                   rename without updating the corresponding effect in
                   LLModelTab.tsx. */
                data-llm-model-id={model.id}
                className={`relative border-b transition-all ${
                    isRouting
                        ? 'bg-sky-50/40 border-sky-200 hover:bg-sky-50/70'
                        : 'bg-surface border-default hover:bg-surface-alt'
                } ${model.enabled ? '' : 'opacity-60'} ${
                    isFocused
                        ? 'ring-2 ring-primary-500 ring-offset-2 ring-offset-surface bg-primary-50/50 z-10'
                        : ''
                }`}
            >
                <div className="flex items-center px-4 py-2">
                    {/* Provider Logo & Name */}
                    <div className="flex items-center space-x-3 flex-1 min-w-0 min-h-0">
                        <div className={`p-2 rounded-lg flex-shrink-0 ${isRouting ? 'bg-sky-100' : 'bg-surface-alt'}`}>
                            {isRouting ? (
                                <Share2 className="w-5 h-5 text-sky-600" />
                            ) : (
                                <ProviderLogo provider={model.provider} size="sm" />
                            )}
                        </div>
                        <div className="flex-1 min-w-0 min-h-0">
                            <div className="flex items-center space-x-2">
                                <h3 className="font-semibold text-default truncate" title={model.name}>
                                    {model.name}
                                </h3>
                                <LLModelStatusBadge status={healthStatus} />
                                <LLModelNotServedBadge model={model} />
                                <LLModelProbeTypeBadge probeType={model.health_status?.probe_type} />
                            </div>
                            <div className="flex items-center space-x-3 mt-1">
                                {isRouting && routingGroupId ? (
                                    <button
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            onNavigateToRoutingGroup?.(routingGroupId);
                                        }}
                                        className="inline-flex items-center space-x-1 text-xs text-sky-700 bg-sky-100 px-2 py-0.5 rounded-full border border-sky-300 hover:bg-sky-200 transition-colors cursor-pointer"
                                        title="Go to Routing Group"
                                    >
                                        <Share2 className="w-3 h-3" />
                                        <span>Routing Group</span>
                                    </button>
                                ) : (
                                    <span className="text-sm text-secondary font-mono truncate">
                                        {model.model_name}
                                    </span>
                                )}
                                <span className="text-xs text-muted font-mono truncate" title={model.id}>
                                    {model.id}
                                </span>
                                {!isRouting && model.routing_group_id && (
                                    <span className="inline-flex items-center space-x-1 text-xs text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                                        <Share2 className="w-3 h-3" />
                                        <span>Load Balanced</span>
                                    </span>
                                )}
                            </div>
                        </div>
                    </div>

                    {/* Mode Badge */}
                    <div className="hidden lg:flex items-center justify-center w-24 flex-shrink-0">
                    <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium ${
                        isRouting ? 'bg-sky-100 text-sky-700' : 'bg-surface-raised text-secondary'
                    }`}>
                        {(() => {
                            if (isRouting) return 'Routing';
                            const mode = (model.mode || 'chat').toLowerCase();
                            // Multimodal modes must be listed BEFORE the
                            // default Chat fallthrough — otherwise
                            // dall-e-3 / tts-1 / whisper-1 / Luma are
                            // mislabelled as Chat, and so are Responses-mode
                            // reasoning models (o-series, gpt-5-pro).
                            switch (mode) {
                                case 'embedding': return 'Embedding';
                                case 'completion': return 'Completion';
                                case 'responses': return 'Responses';
                                case 'image_generation': return 'Image';
                                case 'audio_speech': return 'TTS';
                                case 'audio_transcription': return 'Transcribe';
                                case 'video_generation': return 'Video';
                                case 'rerank': return 'Rerank';
                                case 'moderation': return 'Moderation';
                                case 'chat':
                                default: return 'Chat';
                            }
                        })()}
                    </span>
                    </div>

                    {/* Parameters (Model Size) */}
                    <div className="hidden lg:flex items-center justify-center w-20 flex-shrink-0">
                        {isRouting ? (
                            <span className="text-xs text-muted">—</span>
                        ) : model.parameters != null ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200">
                                {formatModelParameters(model.parameters)}
                            </span>
                        ) : (
                            <span className="text-xs text-muted">—</span>
                        )}
                    </div>


                    {/* Context Window */}
                    <div className="hidden lg:flex items-center justify-center w-16 flex-shrink-0">
                        {(() => {
                            const ctx = model.capabilities?.context_window;
                            if (isRouting || ctx == null) return <span className="text-xs text-muted">—</span>;
                            if (ctx >= 1_000_000) return <span className="text-xs font-medium text-secondary">{(ctx / 1_000_000).toFixed(ctx % 1_000_000 === 0 ? 0 : 1)}M</span>;
                            if (ctx >= 1_000) return <span className="text-xs font-medium text-secondary">{Math.round(ctx / 1_000)}K</span>;
                            return <span className="text-xs font-medium text-secondary">{ctx}</span>;
                        })()}
                    </div>
                    {/* Performance Metrics */}
                    <div className="hidden lg:flex items-center space-x-3 mx-3 flex-shrink-0">
                        {/* Success Rate */}
                        <div className="text-center w-16">
                            <div className={`text-sm font-semibold ${
                                successRate >= 95 ? 'text-green-600' :
                                    successRate >= 80 ? 'text-yellow-600' :
                                        'text-red-600'
                            }`}>
                                {successRate.toFixed(1)}%
                            </div>
                        </div>

                        {/* Latency */}
                        <div className="text-center w-16">
                            <div className="text-sm text-secondary">
                                {avgLatency ? `${avgLatency.toFixed(0)}ms` : '-'}
                            </div>
                        </div>

                        {/* Requests */}
                        <div className="text-center w-16">
                            <div className="text-sm text-secondary">
                                {totalRequests.toLocaleString()}
                            </div>
                        </div>

                        {/* Access */}
                        <div className="text-left min-w-[250px] max-w-[350px]">
                            <GroupTreeDisplay
                                groups={model.groups || []}
                                variant="compact"
                                maxVisible={3}
                            />
                        </div>
                    </div>

                    {/* Actions */}
                    <div className="flex items-center justify-end space-x-2 w-24 flex-shrink-0">
                        {/* Enable/Disable Toggle */}
                        <button
                            onClick={() => onToggleEnabled(!model.enabled)}
                            className={`p-2 rounded-lg transition-colors ${
                                model.enabled
                                    ? 'text-green-600 hover:bg-green-50'
                                    : 'text-muted hover:bg-surface-raised'
                            }`}
                            title={model.enabled ? 'Disable model' : 'Enable model'}
                        >
                            {model.enabled ? (
                                <Power className="w-5 h-5" />
                            ) : (
                                <PowerOff className="w-5 h-5" />
                            )}
                        </button>

                        {/* Quick Actions Menu */}
                        <div className="relative" ref={menuButtonRef}>
                            <button
                                onClick={() => setShowMenu(!showMenu)}
                                className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                            >
                                <MoreVertical className="w-5 h-5" />
                            </button>

                            {showMenu && (
                                <>
                                    <div
                                        className="fixed inset-0 z-10"
                                        onClick={() => setShowMenu(false)}
                                    />
                                    <div className={`absolute right-0 w-48 bg-surface rounded-lg shadow-lg border border-default py-1 z-20 ${
                                        menuPosition === 'top'
                                            ? 'bottom-full mb-1'
                                            : 'top-full mt-1'
                                    }`}>
                                        <button
                                            onClick={() => {
                                                setShowStats(true);
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <BarChart3 className="w-4 h-4 text-muted" />
                                            <span>View Statistics</span>
                                        </button>

                                        <div className="border-t border-default my-1" />

                                        <button
                                            onClick={() => {
                                                onTestConnection();
                                                setShowMenu(false);
                                            }}
                                            disabled={isTesting}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                        >
                                            <Activity className="w-4 h-4 text-muted" />
                                            <span>{isTesting ? 'Testing...' : 'Test Connection'}</span>
                                        </button>

                                        <button
                                            onClick={() => {
                                                onEdit();
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <Edit className="w-4 h-4 text-muted" />
                                            <span>Edit</span>
                                        </button>

                                        <button
                                            onClick={() => {
                                                onCopy();
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <Copy className="w-4 h-4 text-muted" />
                                            <span>Copy</span>
                                        </button>

                                        {(model.provider === 'huggingface' || model.api_url?.includes('huggingface.co') || model.tags?.includes('huggingface')) && (
                                            <button
                                                onClick={() => {
                                                    window.open(`https://huggingface.co/${model.model_name}`, '_blank');
                                                    setShowMenu(false);
                                                }}
                                                className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 text-yellow-700"
                                            >
                                                <ExternalLink className="w-4 h-4" />
                                                <span>HuggingFace Info</span>
                                            </button>
                                        )}

                                        <div className="border-t border-default my-1" />

                                        <button
                                            onClick={() => {
                                                onDelete();
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-red-50 hover:text-red-600 flex items-center space-x-2 text-red-600"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                            <span>Delete</span>
                                        </button>
                                    </div>
                                </>
                            )}
                        </div>
                    </div>
                </div>
            </div>

            {/* Stats Modal */}
            {showStats && (
                <LLModelStatsModal
                    model={model}
                    onClose={() => setShowStats(false)}
                />
            )}
        </>
    );
};

export default LLModelListItem;