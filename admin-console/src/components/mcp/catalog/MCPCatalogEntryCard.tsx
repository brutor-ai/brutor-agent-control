/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Package } from 'lucide-react';
import { MCPCatalogEntryBrief } from '../../../lib/mcpCatalogTypes';

interface MCPCatalogEntryCardProps {
    entry: MCPCatalogEntryBrief;
    onClick: (entry: MCPCatalogEntryBrief) => void;
}

const runtimeBadge = (runtime: string) => {
    switch (runtime) {
        case 'remote':
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-700">Remote</span>;
        case 'containerized':
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-purple-100 text-purple-700">Containerized</span>;
        default:
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-700">{runtime}</span>;
    }
};

const authBadge = (auth: string) => {
    switch (auth) {
        case 'oauth':
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-700">OAuth</span>;
        case 'api_key':
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-yellow-100 text-yellow-700">API Key</span>;
        case 'header':
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-orange-100 text-orange-700">Header</span>;
        case 'none':
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">None</span>;
        default:
            return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">{auth}</span>;
    }
};

const MCPCatalogEntryCard: React.FC<MCPCatalogEntryCardProps> = ({ entry, onClick }) => {
    const [iconError, setIconError] = useState(false);

    const firstLetter = entry.name.charAt(0).toUpperCase();
    const truncatedDesc = entry.short_description
        ? entry.short_description.length > 100
            ? entry.short_description.slice(0, 100) + '...'
            : entry.short_description
        : 'No description available';

    const displayCategories = (entry.categories || []).slice(0, 3);

    return (
        <div
            className="bg-surface border border-strong rounded-lg p-4 hover:border-primary-500 hover:shadow-md transition-all cursor-pointer"
            onClick={() => onClick(entry)}
        >
            <div className="flex items-start space-x-3 mb-3">
                {/* Icon */}
                {entry.icon_url && !iconError ? (
                    <img
                        src={entry.icon_url}
                        alt={entry.name}
                        className="w-10 h-10 rounded flex-shrink-0 object-contain bg-surface-alt"
                        onError={() => setIconError(true)}
                    />
                ) : (
                    <div className="w-10 h-10 rounded bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center flex-shrink-0">
                        {entry.has_icon ? (
                            <Package className="w-6 h-6 text-white" />
                        ) : (
                            <span className="text-white font-bold text-lg">{firstLetter}</span>
                        )}
                    </div>
                )}
                <div className="min-w-0 flex-1 min-h-0">
                    <h3 className="text-sm font-semibold text-default truncate">{entry.name}</h3>
                    <p className="text-xs text-secondary mt-0.5 line-clamp-2">{truncatedDesc}</p>
                </div>
            </div>

            {/* Badges */}
            <div className="flex flex-wrap items-center gap-1.5 mb-2">
                {runtimeBadge(entry.runtime)}
                {authBadge(entry.auth_pattern)}
                {/* Trust badge. `tenant_signed` is the
                    happy-path; `partner_signed` reserved for cross-
                    tenant trust (future); `revoked` is terminal. */}
                {entry.trust_tier === 'tenant_signed' && (
                    <span
                        className="px-2 py-0.5 text-xs font-medium rounded-full bg-emerald-100 text-emerald-700 inline-flex items-center gap-1"
                        title={entry.signed_at ? `Signed ${entry.signed_at}` : 'Signed by tenant'}
                    >
                        ✓ Verified
                    </span>
                )}
                {entry.trust_tier === 'revoked' && (
                    <span
                        className="px-2 py-0.5 text-xs font-medium rounded-full bg-red-100 text-red-700"
                        title="Trust revoked — do not use"
                    >
                        Revoked
                    </span>
                )}
            </div>

            {/* Category tags */}
            {displayCategories.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {displayCategories.map((cat) => (
                        <span
                            key={cat}
                            className="px-1.5 py-0.5 text-xs bg-surface-alt text-secondary rounded"
                        >
                            {cat}
                        </span>
                    ))}
                    {entry.categories.length > 3 && (
                        <span className="px-1.5 py-0.5 text-xs text-muted">
                            +{entry.categories.length - 3}
                        </span>
                    )}
                </div>
            )}
        </div>
    );
};

export default MCPCatalogEntryCard;
