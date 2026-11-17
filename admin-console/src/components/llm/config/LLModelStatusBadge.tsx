/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { CheckCircle2, XCircle, AlertCircle, AlertTriangle, Clock, Power, PowerOff, Lock } from 'lucide-react';
import { ModelStatus, HealthStatus } from '../../../lib/llmTypes';
import { modelErrorText } from '../../../lib/dispatchRefusal';

interface LLModelStatusBadgeProps {
    // Simple enabled/disabled mode
    enabled?: boolean;

    // Advanced health status mode
    status?: ModelStatus;
    healthData?: HealthStatus;

    // Display options
    size?: 'sm' | 'md' | 'lg';
    showResponseTime?: boolean;
    showLastCheck?: boolean;
    showErrorMessage?: boolean;
    variant?: 'badge' | 'card' | 'inline';
}

const LLModelStatusBadge: React.FC<LLModelStatusBadgeProps> = ({
                                                                   enabled,
                                                                   status,
                                                                   healthData,
                                                                   size = 'md',
                                                                   showResponseTime = false,
                                                                   showLastCheck = false,
                                                                   showErrorMessage = false,
                                                                   variant = 'badge'
                                                               }) => {
    // If enabled prop is provided (simple mode), use that
    // Otherwise use status prop (health mode)
    const isSimpleMode = enabled !== undefined;

    const statusConfig = {
        // Health statuses
        healthy: {
            bg: 'bg-green-100',
            text: 'text-green-700',
            border: 'border-green-300',
            icon: CheckCircle2,
            label: 'Healthy'
        },
        unhealthy: {
            bg: 'bg-red-100',
            text: 'text-red-700',
            border: 'border-red-300',
            icon: XCircle,
            label: 'Unhealthy'
        },
        degraded: {
            bg: 'bg-yellow-100',
            text: 'text-yellow-700',
            border: 'border-yellow-300',
            icon: AlertTriangle,
            label: 'Degraded'
        },
        unknown: {
            bg: 'bg-surface-raised',
            text: 'text-secondary',
            border: 'border-strong',
            icon: AlertCircle,
            label: 'Unknown'
        },
        requires_tier: {
            // Distinct from "Unhealthy" — the catalogue is correct,
            // the configured API key just doesn't have access to this
            // tier (Codex preview / Pro / o1-pro / Gemini preview).
            bg: 'bg-amber-100',
            text: 'text-amber-800',
            border: 'border-amber-300',
            icon: Lock,
            label: 'Requires Tier'
        },
        // Simple enabled/disabled states
        enabled: {
            bg: 'bg-green-100',
            text: 'text-green-700',
            border: 'border-green-300',
            icon: Power,
            label: 'Enabled'
        },
        disabled: {
            bg: 'bg-surface-raised',
            text: 'text-secondary',
            border: 'border-strong',
            icon: PowerOff,
            label: 'Disabled'
        }
    };

    // Determine which config to use. Fall back to ``unknown`` if the
    // backend ever sends a status string the UI doesn't recognise yet —
    // a missing entry would otherwise crash the badge with
    // ``Cannot read properties of undefined (reading 'label')``.
    let config;
    let displayLabel;

    if (isSimpleMode) {
        const simpleStatus = enabled ? 'enabled' : 'disabled';
        config = statusConfig[simpleStatus];
        displayLabel = config.label;
    } else {
        config = statusConfig[status || 'unknown'] || statusConfig.unknown;
        displayLabel = config.label;
        // Personalise the label for ``requires_tier`` when the
        // backend supplied the specific tier (e.g. "Requires Pro").
        if (status === 'requires_tier' && healthData?.required_tier) {
            displayLabel = `Requires ${healthData.required_tier}`;
        }
    }

    const Icon = config.icon;

    const sizeClasses = {
        sm: {
            container: 'px-2 py-0.5 text-xs',
            icon: 'w-3 h-3',
            text: 'text-xs'
        },
        md: {
            container: 'px-3 py-1.5 text-sm',
            icon: 'w-4 h-4',
            text: 'text-sm'
        },
        lg: {
            container: 'px-4 py-2 text-base',
            icon: 'w-5 h-5',
            text: 'text-base'
        }
    };

    const sizes = sizeClasses[size];

    // Badge variant - simple inline badge
    if (variant === 'badge') {
        return (
            <div className={`inline-flex items-center space-x-1 rounded ${config.bg} ${config.text} ${sizes.container}`}>
                <Icon className={sizes.icon} />
                <span className="font-medium">{displayLabel}</span>
                {!isSimpleMode && showResponseTime && healthData?.avg_latency_ms && (
                    <>
                        <span>•</span>
                        <span>{healthData.avg_latency_ms}ms</span>
                    </>
                )}
            </div>
        );
    }

    // Inline variant - minimal with just icon and text
    if (variant === 'inline') {
        return (
            <div className={`inline-flex items-center space-x-1.5 ${config.text}`}>
                <Icon className={sizes.icon} />
                <span className={`font-medium ${sizes.text}`}>{displayLabel}</span>
                {!isSimpleMode && showResponseTime && healthData?.avg_latency_ms && (
                    <span className={`${sizes.text} opacity-75`}>
                        ({healthData.avg_latency_ms}ms)
                    </span>
                )}
            </div>
        );
    }

    // Card variant - full display with all details (only for health mode)
    if (isSimpleMode) {
        // Simple mode doesn't support card variant, fall back to badge
        return (
            <div className={`inline-flex items-center space-x-1 rounded ${config.bg} ${config.text} ${sizes.container}`}>
                <Icon className={sizes.icon} />
                <span className="font-medium">{displayLabel}</span>
            </div>
        );
    }

    return (
        <div>
            {/* Header */}
            <div className="flex items-center justify-between mb-1">
                <span className={`text-xs ${config.text} opacity-75`}>Health Status</span>
                {showLastCheck && healthData?.last_check && (
                    <span className="text-xs text-muted flex items-center space-x-1">
                        <Clock className="w-3 h-3" />
                        <span>{new Date(healthData.last_check).toLocaleTimeString()}</span>
                    </span>
                )}
            </div>

            {/* Status Badge */}
            <div className={`flex items-center space-x-2 rounded-lg border ${config.bg} ${config.text} ${config.border} ${sizes.container}`}>
                <Icon className={sizes.icon} />
                <span className="font-medium flex-1 min-h-0 min-w-0">{displayLabel}</span>
                {showResponseTime && healthData?.avg_latency_ms && (
                    <>
                        <span className="text-xs">•</span>
                        <span className="text-xs font-medium">
                            {healthData.avg_latency_ms}ms
                        </span>
                    </>
                )}
            </div>

            {/* Error Message */}
            {showErrorMessage && healthData?.error_message && (
                <p className="text-xs text-red-600 mt-1 truncate" title={healthData.error_message}>
                    {modelErrorText(healthData.error_message, '')}
                </p>
            )}

            {/* Additional Health Info */}
            {healthData?.consecutive_failures !== undefined && healthData.consecutive_failures > 0 && (
                <p className="text-xs text-yellow-600 mt-1">
                    ⚠️ {healthData.consecutive_failures} consecutive failure{healthData.consecutive_failures !== 1 ? 's' : ''}
                </p>
            )}
        </div>
    );
};

export default LLModelStatusBadge;