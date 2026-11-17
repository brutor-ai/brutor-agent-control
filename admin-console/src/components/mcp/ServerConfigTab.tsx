/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Network, KeyRound, BookOpen } from 'lucide-react';
import MCPIcon from '../common/MCPIcon';
import ServersTab from '@/components/mcp/config/server/ServersTab';
import VirtualServersTab from '@/components/mcp/virtual/VirtualServersTab';
import ServerAccessTab from "@/components/mcp/config/server-access/ServerAccessTab";
import MCPCatalogTab from '@/components/mcp/catalog/MCPCatalogTab';

const ServerConfigTab: React.FC = () => {
    const [activeTab, setActiveTab] = useState<'servers' | 'server-access' | 'virtual' | 'catalog'>('servers');

    return (
        <div className="app-column h-full">
            {/* Nested tabs for MCP Servers, Virtual Servers, MCP Server Access, and MCP Catalog */}
            <div className="flex border-b border-default bg-surface px-6">
                <button
                    onClick={() => setActiveTab('servers')}
                    className={`flex items-center space-x-2 px-4 py-2 border-b-2 transition-colors text-sm ${
                        activeTab === 'servers'
                            ? 'border-primary-500 text-primary-600'
                            : 'border-transparent text-secondary hover:text-default'
                    }`}
                >
                    <MCPIcon className="w-3 h-3" />
                    <span>MCP Servers</span>
                </button>
                <button
                    onClick={() => setActiveTab('virtual')}
                    className={`flex items-center space-x-2 px-4 py-2 border-b-2 transition-colors text-sm ${
                        activeTab === 'virtual'
                            ? 'border-primary-500 text-primary-600'
                            : 'border-transparent text-secondary hover:text-default'
                    }`}
                >
                    <Network className="w-3 h-3" />
                    <span>Virtual Servers</span>
                </button>
                <button
                    onClick={() => setActiveTab('server-access')}
                    className={`flex items-center space-x-2 px-4 py-2 border-b-2 transition-colors text-sm ${
                        activeTab === 'server-access'
                            ? 'border-primary-500 text-primary-600'
                            : 'border-transparent text-secondary hover:text-default'
                    }`}
                >
                    <KeyRound className="w-3 h-3" />
                    <span>MCP Server Access</span>
                </button>
                <button
                    onClick={() => setActiveTab('catalog')}
                    className={`flex items-center space-x-2 px-3 py-2 text-sm font-medium rounded-lg border transition-colors ${
                        activeTab === 'catalog'
                            ? 'bg-blue-50 border-blue-400 text-blue-700'
                            : 'border-blue-300 text-secondary hover:bg-blue-50 hover:border-blue-400'
                    }`}
                >
                    <BookOpen className="w-3 h-3 text-blue-600" />
                    <span>MCP Catalog</span>
                </button>
            </div>
            <div className="scroll-region overflow-x-auto">
                {activeTab === 'servers' && <ServersTab onNavigateToVirtualServers={() => setActiveTab('virtual')} />}
                {activeTab === 'server-access' && <ServerAccessTab />}
                {activeTab === 'virtual' && <VirtualServersTab />}
                {activeTab === 'catalog' && <MCPCatalogTab />}
            </div>
        </div>
    );
};

export default ServerConfigTab;