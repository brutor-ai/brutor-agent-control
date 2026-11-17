/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    Trash2,
    CheckCircle,
    XCircle,
    ArrowUpRight,
    Activity,
    AlertCircle,
    Lock,
    ShieldCheck,
    Gauge
} from 'lucide-react';
import { LLModelInfo } from '../../../lib/groupTypes';
import { ProviderLogo } from '@/components/llm/config/providerLogos';

/** A column the caller adds to the table. */
export interface ModelTableColumn {
    id: string;
    header: string;
    /** Tooltip of the column header. */
    headerTitle?: string;
    render: (model: LLModelInfo) => React.ReactNode;
}

const NO_COLUMNS: readonly ModelTableColumn[] = [];

interface GroupLLModelsTableViewProps {
    models: LLModelInfo[];
    onRemove: (modelId: string, modelName: string) => void;
    onConfigureGovernance?: (model: LLModelInfo) => void;
    onConfigureLimits?: (model: LLModelInfo) => void;
    /** Columns shown between Health and Actions, in this order. */
    extraColumns?: readonly ModelTableColumn[];
    removingId: string | null;
    /**
     * Resolve the badge label/color for a model. Receives the *real*
     * platform provider key (the model's `provider` field) plus the
     * model name as a fallback for unknown providers — see
     * GroupLLModelsTab.tsx for the precedence rules and why the
     * provider must be honoured first.
     */
    getModelProvider: (modelName: string, providerKey?: string) => { provider: string; color: string; bgColor: string };
}

const GroupLLModelsTableView: React.FC<GroupLLModelsTableViewProps> = ({
                                                                           models,
                                                                           onRemove,
                                                                           onConfigureGovernance,
                                                                           onConfigureLimits,
                                                                           extraColumns = NO_COLUMNS,
                                                                           removingId,
                                                                           getModelProvider
                                                                       }) => {
    // Helper function to render health status badge.
    //
    // ``requires_tier`` is the eligibility-gated state for restricted
    // models (Codex preview / Pro / o1-pro / Gemini preview): the
    // catalogue is correct, the configured API key just doesn't have
    // access to that tier. Shown in amber with a Lock icon to make
    // the difference from "Unhealthy" obvious. When the backend
    // includes a specific tier name, surface it ("Requires Pro").
    const renderHealthStatus = (status: string, requiredTier?: string) => {
        switch (status) {
            case 'healthy':
                return (
                    <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                        <Activity className="w-3 h-3 text-green-600" />
                        <span className="text-xs font-medium text-green-700">Healthy</span>
                    </div>
                );
            case 'unhealthy':
                return (
                    <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                        <XCircle className="w-3 h-3 text-red-600" />
                        <span className="text-xs font-medium text-red-700">Unhealthy</span>
                    </div>
                );
            case 'degraded':
                return (
                    <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                        <AlertCircle className="w-3 h-3 text-yellow-600" />
                        <span className="text-xs font-medium text-yellow-700">Degraded</span>
                    </div>
                );
            case 'requires_tier':
                return (
                    <div
                        className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full"
                        title={
                            requiredTier
                                ? `Requires ${requiredTier} tier — your API key doesn't have access to this model`
                                : 'Restricted-tier model — your API key doesn’t have access'
                        }
                    >
                        <Lock className="w-3 h-3 text-amber-700" />
                        <span className="text-xs font-medium text-amber-800">
                            {requiredTier ? `Requires ${requiredTier}` : 'Requires Tier'}
                        </span>
                    </div>
                );
            default:
                return (
                    <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-raised rounded-full">
                        <Activity className="w-3 h-3 text-muted" />
                        <span className="text-xs font-medium text-secondary">Unknown</span>
                    </div>
                );
        }
    };

    return (
        <div className="bg-surface rounded-lg border border-default overflow-hidden">
            <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-default">
                    <thead className="bg-surface-alt">
                    <tr>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Provider
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Name
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Description
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Model identifier
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Inherited
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Health
                        </th>
                        {extraColumns.map((column) => (
                            <th
                                key={column.id}
                                scope="col"
                                className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}
                                title={column.headerTitle}
                            >
                                {column.header}
                            </th>
                        ))}
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-right`}>
                            Actions
                        </th>
                    </tr>
                    </thead>
                    <tbody className="bg-surface divide-y divide-default">
                    {models.map((model) => {
                        const { provider } = getModelProvider(model.name, model.provider);
                        const isRemoving = removingId === model.id;
                        const isInherited = model.is_inherited;

                        // The icon should track the *actual* serving
                        // platform from the model row (e.g. ollama),
                        // not the display label which can be a vendor
                        // family ("Meta") for fallback rows. We
                        // already enforce known-provider precedence
                        // in getModelProvider, but we look the icon
                        // up directly so that even fallback rows
                        // don't accidentally trigger a missing-icon
                        // fallback and surface the wrong glyph.
                        const iconKey = (model.provider || '').toLowerCase() || provider.toLowerCase();
                        return (
                            <tr
                                key={model.id}
                                className={`transition-colors ${
                                    isInherited
                                        ? 'bg-surface-alt'
                                        : 'hover:bg-surface-alt'
                                }`}
                            >
                                {/* Provider Icon */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <div className="flex items-center space-x-2">
                                        <div className="p-1.5 rounded-lg bg-surface border border-default">
                                            <ProviderLogo provider={iconKey} size="xs" />
                                        </div>
                                        <span className="text-sm font-medium text-secondary">
                                                {provider}
                                            </span>
                                    </div>
                                </td>

                                {/* Name & ID Combined */}
                                <td className="px-4 py-4">
                                    <div className="flex flex-col">
                                        <div className="text-sm font-medium text-default">
                                            {model.name}
                                        </div>
                                        <code className="text-xs text-muted font-mono mt-0.5">
                                            {model.id}
                                        </code>
                                    </div>
                                </td>

                                {/* Description */}
                                <td className="px-4 py-4">
                                    <div className="text-sm text-secondary max-w-xs">
                                        {model.description ? (
                                            <span className="line-clamp-2">{model.description}</span>
                                        ) : (
                                            <span className="text-muted italic">No description</span>
                                        )}
                                    </div>
                                </td>

                                {/* The provider model name */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <div className="text-sm text-secondary">
                                        {model.model_name}
                                    </div>
                                </td>

                                {/* Inherited Status */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {isInherited ? (
                                        <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                                            <ArrowUpRight className="w-3 h-3 text-muted" />
                                            <span className="text-xs font-medium text-secondary">
                                                    Inherited
                                                </span>
                                        </div>
                                    ) : (
                                        <div className="inline-flex items-center space-x-1 px-2 py-1 bg-primary-pale rounded-full">
                                            <CheckCircle className="w-3 h-3 text-primary" />
                                            <span className="text-xs font-medium text-primary">
                                                    Direct
                                                </span>
                                        </div>
                                    )}
                                </td>

                                {/* Health Status */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {renderHealthStatus(
                                        model.health_status.status,
                                        model.health_status.required_tier,
                                    )}
                                </td>

                                {/* Columns added by the caller */}
                                {extraColumns.map((column) => (
                                    <td key={column.id} className="px-4 py-4 whitespace-nowrap">
                                        {column.render(model)}
                                    </td>
                                ))}

                                {/* Actions */}
                                <td className="px-4 py-4 whitespace-nowrap text-right">
                                    <div className="flex items-center justify-end space-x-1">
                                        <button
                                            onClick={() => onConfigureGovernance?.(model)}
                                            className={`relative p-1.5 rounded transition-colors ${
                                                model.has_governance
                                                    ? 'text-primary-600 hover:text-primary-700 hover:bg-primary-50'
                                                    : 'text-muted hover:text-secondary hover:bg-surface-alt'
                                            }`}
                                            title={model.has_governance ? "Governance active — click to configure" : "Configure governance"}
                                        >
                                            <ShieldCheck className="w-4 h-4" />
                                            {model.has_governance && (
                                                <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-primary-500 rounded-full ring-2 ring-white" />
                                            )}
                                        </button>
                                        <button
                                            onClick={() => onConfigureLimits?.(model)}
                                            className={`relative p-1.5 rounded transition-colors ${
                                                model.has_limits
                                                    ? 'text-amber-600 hover:text-amber-700 hover:bg-surface-alt'
                                                    : 'text-muted hover:text-secondary hover:bg-surface-alt'
                                            }`}
                                            title={model.has_limits ? "Per-model limits active — click to configure" : "Configure per-model limits"}
                                        >
                                            <Gauge className="w-4 h-4" />
                                            {model.has_limits && (
                                                <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-amber-500 rounded-full ring-2 ring-white" />
                                            )}
                                        </button>
                                        {!isInherited && (
                                            <button
                                                onClick={() => onRemove(model.id, model.name)}
                                                disabled={isRemoving}
                                                className="p-1.5 text-red-600 hover:text-red-700 hover:bg-surface-alt rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                                title="Remove model"
                                            >
                                                {isRemoving ? (
                                                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-red-600"></div>
                                                ) : (
                                                    <Trash2 className="w-4 h-4" />
                                                )}
                                            </button>
                                        )}
                                    </div>
                                </td>
                            </tr>
                        );
                    })}
                    </tbody>
                </table>
            </div>

            {/* Footer with count */}
            <div className="px-4 py-3 bg-surface-alt border-t border-default">
                <div className="text-sm text-secondary">
                    Showing <span className="font-medium">{models.length}</span> model{models.length !== 1 ? 's' : ''}
                </div>
            </div>
        </div>
    );
};

export default GroupLLModelsTableView;