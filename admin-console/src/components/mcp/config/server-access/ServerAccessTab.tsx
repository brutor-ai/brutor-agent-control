/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Key, LinkIcon, ShieldCheck } from 'lucide-react';
import OAuthAppClientTab from './oauth_app/OAuthAppClientTab.tsx';
import OAuthTokensTab from './oauth_tokens/OAuthTokensTab.tsx';
import ServerConfigTab from './server_configs/ServerConfigTab.tsx';

// Sub-tab identifiers. Kept as a union so a typo in the initial state
// or the button handler is a compile error.
type SubTabId = 'oauth-clients' | 'oauth-tokens' | 'server-configs';

const ServerAccessTab: React.FC = () => {
    const [activeSubTab, setActiveSubTab] = useState<SubTabId>('oauth-clients');

    const tabs: Array<{ id: SubTabId; label: string; Icon: React.FC<{ className?: string }> }> = [
        { id: 'oauth-clients', label: 'OAuth App Clients', Icon: Key },
        // Inserted between the two existing tabs: read-only diagnostic
        // view of per-user OAuth tokens so admins can tell why an MCP
        // call is 403-ing without dropping into psql.
        { id: 'oauth-tokens', label: 'OAuth Tokens', Icon: ShieldCheck },
        { id: 'server-configs', label: 'MCP Server Access Configurations', Icon: LinkIcon },
    ];

    return (
        <div className="h-full app-column">
            {/* Sub-tabs Navigation */}
            <div className="flex border-b border-default bg-surface px-6">
                {tabs.map(({ id, label, Icon }) => {
                    const active = activeSubTab === id;
                    return (
                        <button
                            key={id}
                            onClick={() => setActiveSubTab(id)}
                            className={`flex items-center space-x-2 px-4 py-3 border-b-2 transition-colors ${
                                active
                                    ? 'border-primary-500 text-primary-600 bg-primary-50'
                                    : 'border-transparent text-secondary hover:text-default hover:bg-surface-alt'
                            }`}
                        >
                            <Icon className="w-4 h-4" />
                            <span className="font-medium">{label}</span>
                        </button>
                    );
                })}
            </div>

            {/* Sub-tab Content */}
            <div className="scroll-region">
                {activeSubTab === 'oauth-clients' && <OAuthAppClientTab />}
                {activeSubTab === 'oauth-tokens' && <OAuthTokensTab />}
                {activeSubTab === 'server-configs' && <ServerConfigTab />}
            </div>
        </div>
    );
};

export default ServerAccessTab;
