/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
    X,
    TrendingUp,
    Activity,
    Zap,
    DollarSign,
    AlertTriangle,
    CheckCircle2,
    Clock,
    BarChart3,
    Percent,
    XCircle
} from 'lucide-react';
import { LLModelConfig } from '../../../lib/llmTypes';
import { modelErrorText } from '../../../lib/dispatchRefusal';

interface LLModelStatsModalProps {
    model: LLModelConfig;
    onClose: () => void;
}

const LLModelStatsModal: React.FC<LLModelStatsModalProps> = ({ model, onClose }) => {
    // Calculate metrics
    const totalRequests = model.performance_metrics.total_requests || 0;
    const failedRequests = model.performance_metrics.failed_requests || 0;
    const successfulRequests = totalRequests - failedRequests;
    const successRate = totalRequests > 0 ? ((successfulRequests / totalRequests) * 100) : 0;
    const failureRate = totalRequests > 0 ? ((failedRequests / totalRequests) * 100) : 0;

    // Calculate estimated cost
    const totalTokens = model.performance_metrics.total_tokens_consumed || 0;
    const inputTokens = Math.floor(totalTokens * 0.4);
    const outputTokens = Math.floor(totalTokens * 0.6);

    // One side may legitimately cost nothing; that must not zero the other.
    const inputCost = (inputTokens / 1_000_000) * (model.cost_per_input_token || 0);
    const outputCost = (outputTokens / 1_000_000) * (model.cost_per_output_token || 0);
    const estimatedCost = inputCost + outputCost;

    // Health metrics
    const healthStatus = model.health_status?.status || 'unknown';
    const consecutiveFailures = model.health_status?.consecutive_failures || 0;
    const lastCheck = model.health_status?.last_check;
    const lastSuccess = model.health_status?.last_success;

    const formatDate = (dateStr?: string) => {
        if (!dateStr) return 'Never';
        try {
            return new Date(dateStr).toLocaleString();
        } catch {
            return 'Invalid date';
        }
    };

    const formatDuration = (dateStr?: string) => {
        if (!dateStr) return null;
        try {
            const diffMs = Date.now() - new Date(dateStr).getTime();
            const diffMins = Math.floor(diffMs / 60000);
            const diffHours = Math.floor(diffMins / 60);
            const diffDays = Math.floor(diffHours / 24);
            if (diffDays > 0) return `${diffDays}d ago`;
            if (diffHours > 0) return `${diffHours}h ago`;
            if (diffMins > 0) return `${diffMins}m ago`;
            return 'Just now';
        } catch {
            return null;
        }
    };

    return (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
                {/* Header */}
                <div className="px-6 py-4 border-b border-default flex items-center justify-between flex-shrink-0 bg-gradient-to-r from-primary-50 to-primary-100">
                    <div>
                        <h2 className="text-xl font-bold text-default flex items-center">
                            <BarChart3 className="w-6 h-6 mr-2 text-primary-600" />
                            Model Statistics: {model.name}
                        </h2>
                        <p className="text-sm text-secondary mt-1">
                            {model.provider} &middot; {model.model_name}
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-primary-200 rounded-lg transition-colors"
                    >
                        <X className="w-5 h-5 text-secondary" />
                    </button>
                </div>

                {/* Content */}
                <div className="scroll-region p-6 space-y-6">
                    {/* Request Statistics */}
                    <section>
                        <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                            <Activity className="w-4 h-4" />
                            Request Statistics
                        </h3>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Total Requests</span>
                                    <Activity className="w-4 h-4 text-primary-500" />
                                </div>
                                <p className="text-2xl font-bold text-default">
                                    {totalRequests.toLocaleString()}
                                </p>
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Successful</span>
                                    <CheckCircle2 className="w-4 h-4 text-green-500" />
                                </div>
                                <p className="text-2xl font-bold text-default">
                                    {successfulRequests.toLocaleString()}
                                </p>
                                <p className="text-xs text-muted mt-1">
                                    {successRate.toFixed(1)}% success rate
                                </p>
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Failed</span>
                                    <XCircle className="w-4 h-4 text-red-500" />
                                </div>
                                <p className={`text-2xl font-bold ${failedRequests > 0 ? 'text-red-600' : 'text-default'}`}>
                                    {failedRequests.toLocaleString()}
                                </p>
                                <p className="text-xs text-muted mt-1">
                                    {failureRate.toFixed(1)}% failure rate
                                </p>
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Avg Latency</span>
                                    <Zap className="w-4 h-4 text-yellow-500" />
                                </div>
                                <p className="text-2xl font-bold text-default">
                                    {(model.performance_metrics.avg_latency_ms || 0).toFixed(0)}
                                    <span className="text-sm font-normal text-muted ml-1">ms</span>
                                </p>
                            </div>
                        </div>
                    </section>

                    {/* Token Usage & Cost */}
                    <section className="pt-6 border-t border-default">
                        <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                            <DollarSign className="w-4 h-4" />
                            Token Usage & Cost
                        </h3>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Total Tokens</span>
                                    <TrendingUp className="w-4 h-4 text-primary-500" />
                                </div>
                                <p className="text-2xl font-bold text-default">
                                    {totalTokens.toLocaleString()}
                                </p>
                                <div className="mt-2 space-y-1 text-xs">
                                    <div className="flex justify-between">
                                        <span className="text-muted">Input (~40%):</span>
                                        <span className="font-medium text-default">{inputTokens.toLocaleString()}</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-muted">Output (~60%):</span>
                                        <span className="font-medium text-default">{outputTokens.toLocaleString()}</span>
                                    </div>
                                </div>
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Estimated Cost</span>
                                    <DollarSign className="w-4 h-4 text-green-500" />
                                </div>
                                <p className="text-2xl font-bold text-green-700">
                                    ${estimatedCost.toFixed(4)}
                                </p>
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Pricing Config</span>
                                </div>
                                <div className="space-y-1 text-xs">
                                    <div className="flex justify-between">
                                        <span className="text-muted">Input:</span>
                                        <span className="font-medium text-default">
                                            ${((model.cost_per_input_token || 0) / 1000).toFixed(4)}/1K
                                        </span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-muted">Output:</span>
                                        <span className="font-medium text-default">
                                            ${((model.cost_per_output_token || 0) / 1000).toFixed(4)}/1K
                                        </span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </section>

                    {/* Health Status */}
                    <section className="pt-6 border-t border-default">
                        <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                            <Activity className="w-4 h-4" />
                            Health Status
                        </h3>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Current Status</span>
                                    {healthStatus === 'healthy' ? (
                                        <CheckCircle2 className="w-4 h-4 text-green-500" />
                                    ) : healthStatus === 'unhealthy' ? (
                                        <XCircle className="w-4 h-4 text-red-500" />
                                    ) : (
                                        <AlertTriangle className="w-4 h-4 text-yellow-500" />
                                    )}
                                </div>
                                <p className={`text-xl font-bold capitalize ${
                                    healthStatus === 'healthy' ? 'text-green-700' :
                                    healthStatus === 'unhealthy' ? 'text-red-700' : 'text-yellow-700'
                                }`}>
                                    {healthStatus}
                                </p>
                                {!!model.health_status?.avg_latency_ms && (
                                    <p className="text-xs text-muted mt-1">
                                        Response time: {model.health_status.avg_latency_ms.toFixed(0)}ms
                                    </p>
                                )}
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Failure Info</span>
                                    <Percent className="w-4 h-4 text-muted" />
                                </div>
                                <div className="space-y-2">
                                    <div className="flex justify-between text-xs">
                                        <span className="text-muted">Consecutive failures:</span>
                                        <span className={`font-semibold ${consecutiveFailures > 0 ? 'text-red-600' : 'text-default'}`}>
                                            {consecutiveFailures}
                                        </span>
                                    </div>
                                    {model.health_status?.error_message && (
                                        <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded">
                                            <p
                                                className="text-xs text-red-700 break-words"
                                                title={model.health_status.error_message}
                                            >
                                                {modelErrorText(model.health_status.error_message, '')}
                                            </p>
                                        </div>
                                    )}
                                </div>
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Last Health Check</span>
                                    <Clock className="w-4 h-4 text-muted" />
                                </div>
                                <p className="text-sm text-default font-medium">
                                    {formatDate(lastCheck)}
                                </p>
                                {formatDuration(lastCheck) && (
                                    <p className="text-xs text-muted mt-1">
                                        {formatDuration(lastCheck)}
                                    </p>
                                )}
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Last Successful Check</span>
                                    <CheckCircle2 className="w-4 h-4 text-green-500" />
                                </div>
                                <p className="text-sm text-default font-medium">
                                    {formatDate(lastSuccess)}
                                </p>
                                {formatDuration(lastSuccess) && (
                                    <p className="text-xs text-muted mt-1">
                                        {formatDuration(lastSuccess)}
                                    </p>
                                )}
                            </div>
                            <div className="bg-surface border border-default rounded-lg p-4">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-muted">Last Probe Type</span>
                                    <Activity className="w-4 h-4 text-muted" />
                                </div>
                                <p className="text-sm text-default font-medium capitalize">
                                    {model.health_status?.last_probe_type ??
                                        model.last_probe_type ??
                                        '—'}
                                </p>
                            </div>
                        </div>
                    </section>

                    {/* Model Configuration */}
                    <section className="pt-6 border-t border-default">
                        <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">
                            Model Configuration
                        </h3>
                        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Temperature</p>
                                <p className="text-sm font-semibold text-default">
                                    {model.default_temperature ?? 'Default'}
                                </p>
                            </div>
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Max Tokens</p>
                                <p className="text-sm font-semibold text-default">
                                    {model.default_max_tokens?.toLocaleString() ?? 'Default'}
                                </p>
                            </div>
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Timeout</p>
                                <p className="text-sm font-semibold text-default">
                                    {model.timeout ? `${model.timeout}s` : 'Default'}
                                </p>
                            </div>
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Streaming</p>
                                <p className="text-sm font-semibold text-default">
                                    {model.supports_streaming ? 'Yes' : 'No'}
                                </p>
                            </div>
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Function Calling</p>
                                <p className="text-sm font-semibold text-default">
                                    {model.supports_function_calling ? 'Yes' : 'No'}
                                </p>
                            </div>
                        </div>
                    </section>

                    {/* Timestamps */}
                    <section className="pt-6 border-t border-default">
                        <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">
                            Timestamps
                        </h3>
                        <div className="grid grid-cols-2 gap-4">
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Created At</p>
                                <p className="text-sm font-medium text-default">
                                    {model.created_at ? formatDate(model.created_at) : 'Unknown'}
                                </p>
                            </div>
                            <div className="bg-surface-alt border border-default rounded-lg p-3">
                                <p className="text-xs text-muted mb-1">Updated At</p>
                                <p className="text-sm font-medium text-default">
                                    {model.updated_at ? formatDate(model.updated_at) : 'Unknown'}
                                </p>
                            </div>
                        </div>
                    </section>
                </div>

                {/* Footer */}
                <div className="px-6 py-4 border-t border-default bg-surface-alt flex justify-end flex-shrink-0">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors font-medium text-sm"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default LLModelStatsModal;
