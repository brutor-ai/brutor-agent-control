/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useMemo, useState } from 'react';
import {
    Database,
    Search,
    Palette,
    Building2,
    Bell,
} from 'lucide-react';

import SearchConfigurationTab from './configuration/SearchConfigurationTab.tsx';
import AppearanceSettings from './configuration/AppearanceSettings.tsx';
import TenantSettingsTab from './configuration/TenantSettingsTab.tsx';
import NotificationSettingsTab from './configuration/NotificationSettingsTab.tsx';
import { mergeTabs, useSlot } from '../app/slots';

type ConfigSubTab = 'tenant' | 'search' | 'notifications' | 'appearance';

interface ConfigurationTabProps {
    currentUser?: any;
    /** Deep link `config:<tab>:<tenant sub-tab>` (App.tsx). */
    initialTab?: string;
    initialTenantSubTab?: string;
}

const CONFIG_SUB_TABS: { id: ConfigSubTab; label: string; icon: React.FC<any> }[] = [
    { id: 'tenant', label: 'Tenant', icon: Building2 },
    { id: 'search', label: 'Search Configuration', icon: Search },
    { id: 'notifications', label: 'Notifications', icon: Bell },
    { id: 'appearance', label: 'Appearance', icon: Palette },
];

const ConfigurationTab: React.FC<ConfigurationTabProps> = ({
                                                             currentUser,
                                                             initialTab,
                                                             initialTenantSubTab,
                                                           }) => {
    // A tab is one of this page's own or one contributed to the
    // `settings.tabs` slot (MCP Registry is contributed).
    const slotTabs = useSlot('settings.tabs');
    const { tabs, contributed } = useMemo(() => mergeTabs(CONFIG_SUB_TABS, slotTabs), [slotTabs]);
    const [activeTab, setActiveTab] = useState<string>(
        tabs.find((t) => t.id === initialTab)?.id ?? 'tenant',
    );

    return (
        <div className="app-column h-full">
            {/* Header */}
            <div className="px-6 pt-6 pb-4">
                <div className="flex items-center space-x-3 mb-2">
                    <Database className="w-7 h-7 text-primary" />
                    <h1 className="text-2xl font-bold text-default">Settings</h1>
                </div>
                <p className="text-secondary">
                    Manage platform configuration — MCP registries, search providers, and system settings
                </p>
                {currentUser && (
                    <div className="flex items-center mt-3">
                        <span className="text-sm text-secondary">
                            Logged in as {currentUser.username}
                            {(currentUser.role_name || currentUser.role_id) && (
                                <> ({currentUser.role_name || currentUser.role_id})</>
                            )}
                        </span>
                    </div>
                )}
            </div>

            {/* Sub-Tab Navigation */}
            <div className="flex border-b border-default bg-surface px-6">
                {tabs.map(({ id, label, icon: Icon }) => (
                    <button
                        key={id}
                        onClick={() => setActiveTab(id)}
                        className={`flex items-center space-x-2 px-4 py-2.5 border-b-2 transition-colors text-sm font-medium ${
                            activeTab === id
                                ? 'border-primary-500 text-primary-600'
                                : 'border-transparent text-secondary hover:text-default hover:border-strong'
                        }`}
                    >
                        <Icon className="w-4 h-4" />
                        <span>{label}</span>
                    </button>
                ))}
            </div>

            {/* Tab Content */}
            <div className="scroll-region p-6">
                {activeTab === 'tenant' && <TenantSettingsTab initialSubTab={initialTenantSubTab} />}
                {activeTab === 'notifications' && <NotificationSettingsTab />}
                {contributed.get(activeTab)?.render()}
                {activeTab === 'search' && (
                    <SearchConfigurationTab />
                )}
                {activeTab === 'appearance' && (
                    <AppearanceSettings />
                )}
            </div>
        </div>
    );
};

export default ConfigurationTab;
