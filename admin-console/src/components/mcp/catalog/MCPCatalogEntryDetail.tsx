/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    Package,
    ExternalLink,
    Download,
    ArrowLeft,
    Globe,
    Container,
    Shield,
    Variable,
    Wrench
} from 'lucide-react';
import { MCPCatalogEntry } from '../../../lib/mcpCatalogTypes';

interface MCPCatalogEntryDetailProps {
    entry: MCPCatalogEntry;
    onImport: (entry: MCPCatalogEntry) => void;
    onBack?: () => void;
}

const runtimeBadge = (runtime: string) => {
    switch (runtime) {
        case 'remote':
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-blue-100 text-blue-700">Remote</span>;
        case 'containerized':
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-purple-100 text-purple-700">Containerized</span>;
        default:
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-700">{runtime}</span>;
    }
};

const authBadge = (auth: string) => {
    switch (auth) {
        case 'oauth':
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-green-100 text-green-700">OAuth</span>;
        case 'api_key':
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-yellow-100 text-yellow-700">API Key</span>;
        case 'header':
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-orange-100 text-orange-700">Header</span>;
        case 'none':
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-600">None</span>;
        default:
            return <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-600">{auth}</span>;
    }
};

const MCPCatalogEntryDetail: React.FC<MCPCatalogEntryDetailProps> = ({ entry, onImport, onBack }) => {
    const [iconError, setIconError] = useState(false);
    const firstLetter = entry.name.charAt(0).toUpperCase();

    return (
        <div className="app-column h-full">
            {/* Top bar: Back + Import */}
            <div className="flex items-center justify-between mb-4">
                {onBack ? (
                    <button
                        onClick={onBack}
                        className="flex items-center space-x-1 text-sm text-secondary hover:text-default transition-colors"
                    >
                        <ArrowLeft className="w-4 h-4" />
                        <span>Back to list</span>
                    </button>
                ) : (
                    <div />
                )}
                <button
                    onClick={() => onImport(entry)}
                    className="flex items-center space-x-2 px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors text-sm"
                >
                    <Download className="w-4 h-4" />
                    <span>Import to Servers</span>
                </button>
            </div>

            {/* Scrollable content */}
            <div className="scroll-region">
            {/* Header */}
            <div className="flex items-start space-x-4 mb-6">
                {entry.icon_url && !iconError ? (
                    <img
                        src={entry.icon_url}
                        alt={entry.name}
                        className="w-16 h-16 rounded flex-shrink-0 object-contain bg-surface-alt"
                        onError={() => setIconError(true)}
                    />
                ) : (
                    <div className="w-16 h-16 rounded bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center flex-shrink-0">
                        {entry.has_icon ? (
                            <Package className="w-10 h-10 text-white" />
                        ) : (
                            <span className="text-white font-bold text-2xl">{firstLetter}</span>
                        )}
                    </div>
                )}
                <div className="flex-1 min-w-0 min-h-0">
                    <h2 className="text-xl font-bold text-default">{entry.name}</h2>
                    {entry.short_description && (
                        <p className="text-sm text-secondary mt-1">{entry.short_description}</p>
                    )}
                    <div className="flex flex-wrap items-center gap-2 mt-2">
                        {runtimeBadge(entry.runtime)}
                        {authBadge(entry.auth_pattern)}
                        {entry.source === 'system' && (
                            <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-indigo-100 text-indigo-700">System</span>
                        )}
                    </div>
                </div>
            </div>

            {/* Description */}
            {entry.description && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2">Description</h3>
                    <div className="text-sm text-secondary whitespace-pre-wrap bg-surface-alt rounded-md p-3 border border-strong">
                        {entry.description}
                    </div>
                </div>
            )}

            {/* Categories */}
            {entry.categories && entry.categories.length > 0 && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2">Categories</h3>
                    <div className="flex flex-wrap gap-1.5">
                        {entry.categories.map((cat) => (
                            <span key={cat} className="px-2 py-1 text-xs bg-surface-alt text-secondary rounded border border-strong">
                                {cat}
                            </span>
                        ))}
                    </div>
                </div>
            )}

            {/* Tool Preview */}
            {entry.tool_preview && entry.tool_preview.length > 0 && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2 flex items-center space-x-1.5">
                        <Wrench className="w-4 h-4" />
                        <span>Tools ({entry.tool_preview.length})</span>
                    </h3>
                    <div className="border border-strong rounded-md overflow-hidden">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                                <tr>
                                    <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>Name</th>
                                    <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>Description</th>
                                </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                                {entry.tool_preview.map((tool) => (
                                    <tr key={tool.name}>
                                        <td className="px-4 py-2 text-sm font-mono text-default whitespace-nowrap">{tool.name}</td>
                                        <td className="px-4 py-2 text-sm text-secondary">{tool.description}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Environment Variables */}
            {entry.env_vars && entry.env_vars.length > 0 && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2 flex items-center space-x-1.5">
                        <Variable className="w-4 h-4" />
                        <span>Environment Variables ({entry.env_vars.length})</span>
                    </h3>
                    <div className="border border-strong rounded-md overflow-hidden">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                                <tr>
                                    <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>Key</th>
                                    <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>Description</th>
                                    <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>Required</th>
                                    <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>Sensitive</th>
                                </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                                {entry.env_vars.map((envVar) => (
                                    <tr key={envVar.key}>
                                        <td className="px-4 py-2 text-sm font-mono text-default whitespace-nowrap">{envVar.key}</td>
                                        <td className="px-4 py-2 text-sm text-secondary">{envVar.description}</td>
                                        <td className="px-4 py-2 text-sm">
                                            {envVar.required ? (
                                                <span className="text-red-600 font-medium">Yes</span>
                                            ) : (
                                                <span className="text-muted">No</span>
                                            )}
                                        </td>
                                        <td className="px-4 py-2 text-sm">
                                            {envVar.sensitive ? (
                                                <Shield className="w-4 h-4 text-yellow-600" />
                                            ) : (
                                                <span className="text-muted">No</span>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Remote Config */}
            {entry.runtime === 'remote' && entry.remote_config && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2 flex items-center space-x-1.5">
                        <Globe className="w-4 h-4" />
                        <span>Remote Configuration</span>
                    </h3>
                    <div className="bg-surface-alt rounded-md p-3 border border-strong text-sm space-y-1">
                        {entry.remote_config.fixedURL && (
                            <div><span className="font-medium text-default">URL:</span> <span className="text-secondary font-mono">{entry.remote_config.fixedURL}</span></div>
                        )}
                        {entry.remote_config.hostname && (
                            <div><span className="font-medium text-default">Hostname:</span> <span className="text-secondary font-mono">{entry.remote_config.hostname}</span></div>
                        )}
                        {entry.remote_config.headers && entry.remote_config.headers.length > 0 && (
                            <div>
                                <span className="font-medium text-default">Headers:</span>
                                <ul className="ml-4 mt-1 space-y-0.5">
                                    {entry.remote_config.headers.map((h) => (
                                        <li key={h.key} className="text-secondary font-mono text-xs">
                                            {h.key}{h.required && <span className="text-red-500 ml-1">*</span>}
                                            {h.sensitive && <Shield className="w-3 h-3 inline ml-1 text-yellow-600" />}
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        )}
                    </div>
                </div>
            )}

            {/* Containerized Config */}
            {entry.runtime === 'containerized' && entry.containerized_config && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2 flex items-center space-x-1.5">
                        <Container className="w-4 h-4" />
                        <span>Container Configuration</span>
                    </h3>
                    <div className="bg-surface-alt rounded-md p-3 border border-strong text-sm space-y-1">
                        <div><span className="font-medium text-default">Image:</span> <span className="text-secondary font-mono">{entry.containerized_config.image}</span></div>
                        <div><span className="font-medium text-default">Port:</span> <span className="text-secondary">{entry.containerized_config.port}</span></div>
                        <div><span className="font-medium text-default">Path:</span> <span className="text-secondary font-mono">{entry.containerized_config.path}</span></div>
                        {entry.containerized_config.args && entry.containerized_config.args.length > 0 && (
                            <div><span className="font-medium text-default">Args:</span> <span className="text-secondary font-mono">{entry.containerized_config.args.join(' ')}</span></div>
                        )}
                    </div>
                </div>
            )}

            {/* Repo URL */}
            {entry.repo_url && (
                <div className="mb-6">
                    <h3 className="text-sm font-semibold text-default mb-2">Repository</h3>
                    <a
                        href={entry.repo_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center space-x-1.5 text-sm text-primary-600 hover:text-primary-700 transition-colors"
                    >
                        <ExternalLink className="w-4 h-4" />
                        <span>{entry.repo_url}</span>
                    </a>
                </div>
            )}

            </div>{/* end scrollable content */}
        </div>
    );
};

export default MCPCatalogEntryDetail;
