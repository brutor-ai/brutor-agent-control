/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
    Edit,
    Trash2,
    Power,
    PowerOff,
    Activity,
    Zap,
    TrendingUp,
    MoreVertical,
    AlertCircle,
    BarChart3,
    CheckCircle2,
    DollarSign,
    Copy,
    ExternalLink,
    Share2,
} from 'lucide-react';
import { LLModelConfig, formatModelParameters, isRoutingModel, getRoutingGroupId } from '../../../lib/llmTypes';
import { modelErrorText } from '../../../lib/dispatchRefusal';
import LLModelStatusBadge from './LLModelStatusBadge';
import LLModelNotServedBadge from './LLModelNotServedBadge';
import { ProviderLogo } from './providerLogos';
import LLModelStatsModal from './LLModelStatsModal';
import GroupTreeDisplay from '../../common/GroupTreeDisplay.tsx';

interface LLModelCardProps {
    model: LLModelConfig;
    onEdit: () => void;
    onDelete: () => void;
    onCopy: () => void;
    onTestConnection: () => void;
    onToggleEnabled: (enabled: boolean) => void;
    onNavigateToRoutingGroup?: (routingGroupId: string) => void;
    isTesting: boolean;
    /**
     * When true the card is rendered with a primary-coloured ring +
     * brief pulse so the operator can immediately spot the model that
     * a Mission Control alert deep-linked them to. Selection lives in
     * LLModelTab's selectedModelId state.
     */
    isFocused?: boolean;
}

const LLModelCard: React.FC<LLModelCardProps> = ({
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

    const isAutoDetected = model.api_url_auto_discovered || !model.api_url;
    const isRouting = isRoutingModel(model);
    const routingGroupId = getRoutingGroupId(model);

    return (
        <>
            <div
                data-llm-model-id={model.id}
                className={`relative rounded-lg border-2 transition-all hover:shadow-md ${
                    isRouting
                        ? 'bg-sky-50/40 border-sky-300'
                        : model.enabled ? 'bg-surface border-default' : 'bg-surface border-default opacity-60'
                } ${!isRouting && !model.enabled ? 'opacity-60' : ''} ${
                    isFocused
                        ? 'ring-2 ring-primary-500 ring-offset-2 ring-offset-surface !border-primary-500 z-10'
                        : ''
                }`}
            >
                {/* Header */}
                <div className="p-4 border-b border-default">
                    <div className="flex items-start justify-between mb-3">
                        <div className="flex items-start space-x-3 flex-1 min-w-0 min-h-0">
                            <div className={`p-2 rounded-lg flex-shrink-0 ${isRouting ? 'bg-sky-100' : 'bg-surface-alt'}`}>
                                {isRouting ? (
                                    <Share2 className="w-6 h-6 text-sky-600" />
                                ) : (
                                    <ProviderLogo provider={model.provider} size="md" />
                                )}
                            </div>
                            <div className="flex-1 min-w-0 min-h-0">
                                <h3 className="font-semibold text-default truncate" title={model.name}>
                                    {model.name}
                                </h3>
                                {model.description && (
                                    <p className="text-sm text-secondary line-clamp-2 mt-1">
                                        {model.description}
                                    </p>
                                )}
                            </div>
                        </div>

                        {/* Menu */}
                        <div className="relative ml-2 flex-shrink-0">
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
                                    <div className="absolute right-0 top-full mt-1 w-48 bg-surface rounded-lg shadow-lg border border-default py-1 z-20">
                                        <button
                                            onClick={() => {
                                                setShowStats(true);
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <BarChart3 className="w-4 h-4" />
                                            <span>View Statistics</span>
                                        </button>

                                        <div className="border-t border-default my-1" />

                                        <button
                                            onClick={() => {
                                                onToggleEnabled(!model.enabled);
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            {model.enabled ? (
                                                <>
                                                    <PowerOff className="w-4 h-4" />
                                                    <span>Disable</span>
                                                </>
                                            ) : (
                                                <>
                                                    <Power className="w-4 h-4" />
                                                    <span>Enable</span>
                                                </>
                                            )}
                                        </button>

                                        <button
                                            onClick={() => {
                                                onTestConnection();
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <Activity className="w-4 h-4" />
                                            <span>Test Connection</span>
                                        </button>

                                        <button
                                            onClick={() => {
                                                onEdit();
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <Edit className="w-4 h-4" />
                                            <span>Edit</span>
                                        </button>

                                        <button
                                            onClick={() => {
                                                onCopy();
                                                setShowMenu(false);
                                            }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                                        >
                                            <Copy className="w-4 h-4" />
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
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt text-red-600 flex items-center space-x-2"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                            <span>Delete</span>
                                        </button>
                                    </div>
                                </>
                            )}
                        </div>
                    </div>

                    {/* Status Badge and Model Info */}
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                        <div className="flex items-center space-x-2 min-w-0">
                            <LLModelStatusBadge enabled={model.enabled} size="sm" />
                            <LLModelNotServedBadge model={model} />
                            {isRouting ? (
                                <button
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        if (routingGroupId) onNavigateToRoutingGroup?.(routingGroupId);
                                    }}
                                    className="inline-flex items-center space-x-1 text-xs text-sky-700 bg-sky-100 px-2 py-0.5 rounded-full border border-sky-300 hover:bg-sky-200 transition-colors cursor-pointer"
                                    title="Go to Routing Group"
                                >
                                    <Share2 className="w-3 h-3" />
                                    <span>Routing Group</span>
                                </button>
                            ) : (
                                <span className="text-sm text-secondary truncate" title={model.model_name}>
                                    {model.model_name}
                                </span>
                            )}
                            {!isRouting && model.parameters != null && (
                                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200">
                                    {formatModelParameters(model.parameters)}
                                </span>
                            )}
                        </div>
                    </div>
                </div>

                {/* Performance Metrics and Pricing */}
                <div className="p-4 space-y-4">
                    {/* Health Status */}
                    {model.health_status && (
                        <div>
                            <LLModelStatusBadge
                                status={model.health_status.status || 'unknown'}
                                healthData={model.health_status}
                                variant="card"
                                showResponseTime={true}
                                showLastCheck={true}
                                showErrorMessage={false}
                            />
                        </div>
                    )}

                    {/* Error Message (if present and not healthy) */}
                    {model.health_status?.error_message && model.health_status.status !== 'healthy' && (
                        <div className="p-3 bg-red-50 border border-red-200 rounded-lg">
                            <div className="flex items-start space-x-2">
                                <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
                                <div className="flex-1 min-w-0 min-h-0">
                                    <p className="text-sm font-medium text-red-900 mb-1">Error Details</p>
                                    <p
                                        className="text-xs text-red-700 break-words whitespace-normal"
                                        title={model.health_status.error_message}
                                    >
                                        {modelErrorText(model.health_status.error_message, '')}
                                    </p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Performance Metrics Grid */}
                    {(model.performance_metrics?.avg_latency_ms != null ||
                        model.performance_metrics?.success_rate != null ||
                        model.performance_metrics?.total_requests != null) && (
                        <div className="grid grid-cols-3 gap-3">
                            {model.performance_metrics?.avg_latency_ms != null && (
                                <div>
                                    <div className="flex items-center space-x-1 text-xs text-secondary mb-1">
                                        <Zap className="w-3 h-3" />
                                        <span>Avg Latency</span>
                                    </div>
                                    <p className="text-lg font-semibold text-default">
                                        {model.performance_metrics.avg_latency_ms.toFixed(0)}
                                        <span className="text-xs font-normal text-secondary ml-1">ms</span>
                                    </p>
                                </div>
                            )}

                            {model.performance_metrics?.success_rate != null && (
                                <div>
                                    <div className="flex items-center space-x-1 text-xs text-secondary mb-1">
                                        <TrendingUp className="w-3 h-3" />
                                        <span>Success Rate</span>
                                    </div>
                                    <p className="text-lg font-semibold text-default">
                                        {(model.performance_metrics.success_rate).toFixed(1)}
                                        <span className="text-xs font-normal text-secondary ml-1">%</span>
                                    </p>
                                </div>
                            )}

                            {model.performance_metrics?.total_requests != null && (
                                <div>
                                    <div className="flex items-center space-x-1 text-xs text-secondary mb-1">
                                        <Activity className="w-3 h-3" />
                                        <span>Total</span>
                                    </div>
                                    <p className="text-lg font-semibold text-default">
                                        {model.performance_metrics.total_requests.toLocaleString()}
                                    </p>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Stats Button */}
                    {model.performance_metrics?.total_requests != null && model.performance_metrics.total_requests > 0 && (
                        <button
                            onClick={() => setShowStats(true)}
                            className="w-full flex items-center justify-center space-x-2 px-3 py-2 bg-gradient-to-r from-primary-500 to-primary-600 text-white rounded-lg hover:from-primary-600 hover:to-primary-700 transition-all shadow-sm hover:shadow-md"
                        >
                            <BarChart3 className="w-4 h-4" />
                            <span className="text-sm font-medium">View Detailed Statistics</span>
                        </button>
                    )}

                    {/* Pricing */}
                    {!!(model.cost_per_input_token || model.cost_per_output_token) && (
                        <div className="border border-default rounded-lg p-3">
                            <div className="flex items-center space-x-2 mb-2">
                                <div className="p-1 bg-surface-raised rounded flex-shrink-0">
                                    <DollarSign className="w-3 h-3 text-secondary" />
                                </div>
                                <span className="text-xs font-medium text-default">Estimated Cost (1M tokens)</span>
                            </div>

                            {/* Input Token Cost */}
                            {!!model.cost_per_input_token && (
                                <div className="bg-surface-alt border border-default rounded-lg p-2 mb-2">
                                    <div className="flex items-baseline space-x-1 mb-1">
                                        <span className="text-lg font-bold text-default">
                                            ${(model.cost_per_input_token).toFixed(2)}
                                        </span>
                                        <span className="text-xs text-secondary">input</span>
                                    </div>
                                    <div className="text-xs text-secondary">
                                        1,000,000 input tokens @ ${(model.cost_per_input_token / 1000).toFixed(4)}/1k
                                    </div>
                                </div>
                            )}

                            {/* Output Token Cost */}
                            {!!model.cost_per_output_token && (
                                <div className="bg-surface-alt border border-default rounded-lg p-2">
                                    <div className="flex items-baseline space-x-1 mb-1">
                                        <span className="text-lg font-bold text-default">
                                            ${(model.cost_per_output_token).toFixed(2)}
                                        </span>
                                        <span className="text-xs text-secondary">output</span>
                                    </div>
                                    <div className="text-xs text-secondary">
                                        1,000,000 output tokens @ ${(model.cost_per_output_token / 1000).toFixed(4)}/1k
                                    </div>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Routing Group Info */}
                    {isRouting && (
                        <div className="border border-sky-200 rounded-lg p-3 bg-sky-50">
                            <div className="flex items-start space-x-2">
                                <div className="p-1 bg-sky-100 rounded flex-shrink-0">
                                    <Share2 className="w-3 h-3 text-sky-600" />
                                </div>
                                <div className="flex-1 min-w-0 min-h-0">
                                    <span className="text-xs font-medium text-sky-900">Virtual Routing Model</span>
                                    <p className="text-xs text-sky-700 mt-1">
                                        Routes requests across member models via load balancer.
                                        Health determined by member model availability.
                                    </p>
                                    {routingGroupId && onNavigateToRoutingGroup && (
                                        <button
                                            onClick={() => onNavigateToRoutingGroup(routingGroupId)}
                                            className="mt-2 text-xs text-sky-700 underline hover:text-sky-900 transition-colors"
                                        >
                                            View Routing Group →
                                        </button>
                                    )}
                                </div>
                            </div>
                        </div>
                    )}
                    {!isRouting && model.routing_group_id && (
                        <div className="border border-default rounded-lg p-3">
                            <div className="flex items-start space-x-2">
                                <div className="p-1 bg-surface-raised rounded flex-shrink-0">
                                    <Share2 className="w-3 h-3 text-secondary" />
                                </div>
                                <div className="flex-1 min-w-0 min-h-0">
                                    <div className="flex items-center justify-between mb-1">
                                        <span className="text-xs font-medium text-default">Load Balanced</span>
                                        <span className="text-xs text-secondary">
                                            Weight: {model.routing_weight || 1.0}
                                        </span>
                                    </div>
                                    <div className="text-xs text-secondary">
                                        Part of routing group
                                        <code className="ml-1 px-1.5 py-0.5 bg-surface-raised rounded text-default font-mono">
                                            {model.routing_group_id.substring(0, 8)}...
                                        </code>
                                    </div>
                                    <p className="text-xs text-secondary mt-1">
                                        Traffic distributed via load balancer
                                    </p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Group Access - Tree Display */}
                    <div className="pt-3 border-t border-default">
                        <span className="text-xs text-muted block mb-1">Access</span>
                        <GroupTreeDisplay
                            groups={model.groups || []}
                            variant="expanded"
                            maxVisible={5}
                        />
                    </div>
                </div>

                {/* Footer */}
                <div className="px-4 py-3 border-t border-default bg-surface-alt">
                    <div className="flex items-center space-x-2 text-xs">
                        <span className="text-secondary flex-shrink-0">API URL:</span>
                        {isAutoDetected ? (
                            <div className="flex items-center space-x-1.5 px-2 py-1 bg-blue-50 border border-blue-200 rounded">
                                <CheckCircle2 className="w-3 h-3 text-blue-600 flex-shrink-0" />
                                <span className="text-blue-700 font-medium whitespace-nowrap">Auto Detected</span>
                            </div>
                        ) : (
                            <code className="text-xs bg-surface px-2 py-0.5 rounded border border-default truncate flex-1 min-w-0 min-h-0" title={model.api_url}>
                                {model.api_url}
                            </code>
                        )}
                    </div>
                </div>

                {/* Testing Overlay */}
                {isTesting && (
                    <div className="absolute inset-0 bg-surface/80 backdrop-blur-sm rounded-lg flex items-center justify-center">
                        <div className="flex items-center space-x-2 text-primary-600">
                            <Activity className="w-5 h-5 animate-pulse" />
                            <span className="font-medium">Testing connection...</span>
                        </div>
                    </div>
                )}

                {/* Disabled Overlay */}
                {!model.enabled && (
                    <div className="absolute inset-0 bg-gray-900/5 rounded-lg pointer-events-none" />
                )}
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

export default LLModelCard;