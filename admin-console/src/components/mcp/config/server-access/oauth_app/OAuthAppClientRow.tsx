/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
    Edit,
    Trash2,
    Shield,
    Lock,
    Globe,
    Monitor,
    Smartphone,
    Terminal,
    Server
} from 'lucide-react';
import { OAuthAppClient } from '../../../../../lib/types.ts';

interface OAuthAppClientRowProps {
    client: OAuthAppClient;
    idpName: string;
    onEdit: (client: OAuthAppClient) => void;
    onDelete: (clientId: string, clientName: string) => void;
}

const APP_TYPE_ICONS = {
    webapp: Monitor,
    mobile: Smartphone,
    desktop: Monitor,
    cli: Terminal,
    service: Server
};

const OAuthAppClientRow: React.FC<OAuthAppClientRowProps> = ({ client, idpName, onEdit, onDelete }) => {
    const AppIcon = APP_TYPE_ICONS[client.app_type];

    return (
        <div className="bg-surface rounded-lg border p-4 hover:shadow-md transition-shadow">
            <div className="flex items-start justify-between mb-3">
                <div className="flex items-center space-x-2">
                    <AppIcon className="w-5 h-5 text-primary" />
                    <div>
                        <div className="flex items-center space-x-2">
                            <h3 className="font-semibold text-default">{client.name}</h3>
                            {client.auto_registered && (
                                // Flags clients created via the
                                // inbound RFC 7591 DCR endpoint. Operators
                                // see these at a glance and can revoke
                                // unexpected entries via the Delete button.
                                <span
                                    className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 text-amber-800"
                                    title="Created via RFC 7591 Dynamic Client Registration. Anyone who hit /v1/auth/dcr/.../register created this row."
                                >
                                    via DCR
                                </span>
                            )}
                        </div>
                        <p className="text-[10px] text-muted font-mono">{client.id}</p>
                    </div>
                </div>
                <div className="flex items-center space-x-1">
                    <button
                        onClick={() => onEdit(client)}
                        className="p-1 text-primary hover:text-primary-700 rounded"
                        title="Edit"
                    >
                        <Edit className="w-4 h-4" />
                    </button>
                    <button
                        onClick={() => onDelete(client.id, client.name)}
                        className="p-1 text-red-600 hover:text-red-700 rounded"
                        title="Delete"
                    >
                        <Trash2 className="w-4 h-4" />
                    </button>
                </div>
            </div>

            <div className="space-y-2 text-sm">
                <div className="flex items-center justify-between">
                    <span className="text-secondary">App Type:</span>
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-primary-100 text-primary-800">
            {client.app_type}
          </span>
                </div>

                <div className="flex items-center justify-between">
                    <span className="text-secondary">Client Type:</span>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                        client.client_type === 'confidential'
                            ? 'bg-purple-100 text-purple-800'
                            : client.client_type === 'public_with_secret'
                                ? 'bg-amber-100 text-amber-800'
                                : 'bg-green-100 text-green-800'
                    }`}>
            {client.client_type === 'confidential' ? (
                <>
                    <Lock className="w-3 h-3 mr-1" />
                    Confidential
                </>
            ) : client.client_type === 'public_with_secret' ? (
                <>
                    <Shield className="w-3 h-3 mr-1" />
                    Public + Secret
                </>
            ) : (
                <>
                    <Globe className="w-3 h-3 mr-1" />
                    Public
                </>
            )}
          </span>
                </div>

                <div>
                    <span className="text-secondary">IdP:</span>
                    <div className="flex items-center mt-1">
                        <Shield className="w-3 h-3 text-muted mr-1" />
                        <span className="text-default">{idpName}</span>
                    </div>
                </div>

                <div>
                    <span className="text-secondary">Client ID:</span>
                    <div className="font-mono text-xs text-default mt-1 break-all">
                        {client.client_id}
                    </div>
                </div>

                {client.scopes.length > 0 && (
                    <div>
                        <span className="text-secondary">Scopes:</span>
                        <div className="flex flex-wrap gap-1 mt-1">
                            {client.scopes.slice(0, 2).map((scope) => (
                                <span key={scope} className="inline-flex items-center px-2 py-0.5 rounded text-xs bg-surface-raised text-default">
                  {scope}
                </span>
                            ))}
                            {client.scopes.length > 2 && (
                                <span className="text-xs text-muted">+{client.scopes.length - 2} more</span>
                            )}
                        </div>
                    </div>
                )}

                {client.supports_pkce && (
                    <div className="flex items-center text-xs text-secondary">
                        <Shield className="w-3 h-3 mr-1" />
                        PKCE Enabled
                    </div>
                )}
            </div>
        </div>
    );
};

export default OAuthAppClientRow;