/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import {
    RefreshCw,
    Activity,
    Zap,
    PauseCircle,
    Share2,
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway.ts';
import { RouterStatusResponse } from '../../../lib/llmTypes.ts';
import RouterStatusSubTab from './RouterStatusSubTab';
import RouterTestSubTab from './RouterTestSubTab';
import RoutingGroupsSubTab from './RoutingGroupsSubTab';
import toast from 'react-hot-toast';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { mergeTabs, useSlot } from '../../../app/slots';
import { failureSentence, errorKind } from '../../../lib/narrow';

const LLMRoutingTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    // A view is one of this page's own ('groups', 'status', 'test') or a tab
    // contributed to the `llm-routing.tabs` slot (Routing Group Cache is).
    const [activeView, setActiveView] = useState<string>('groups');
    const slotTabs = useSlot('llm-routing.tabs');
    const [routerStatus, setRouterStatus] = useState<RouterStatusResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const hasLoadedRef = useRef(false);

    // Track if any form is open in child components (prevents auto-refresh)
    const [isFormActive, setIsFormActive] = useState(false);

    /** Resolves to whether the status loaded; a failure has already been toasted. */
    const loadRouterStatus = async (): Promise<boolean> => {
        try {
            const response = await gatewayClient.getRouterStatus();
            setRouterStatus(response);
            return true;
        } catch (error) {
            console.error('Failed to load router status:', errorKind(error));
            toast.error(failureSentence('Failed to load router status', error));
            return false;
        }
    };

    const loadData = async () => {
        if (!hasLoadedRef.current) setLoading(true);
        try {
            // The Status, Test Router, and Routing Group Cache
            // sub-tabs each fetch their own data — this top-
            // level loader only owns router status.
            return await loadRouterStatus();
        } finally {
            setLoading(false);
            hasLoadedRef.current = true;
        }
    };

    const handleRefresh = async () => {
        setRefreshing(true);
        try {
            if (await loadData()) toast.success('Router data refreshed');
        } finally {
            setRefreshing(false);
        }
    };

    useEffect(() => {
        // Skip auto-refresh if form is active
        if (isFormActive) {
            return;
        }

        // Load data initially and set up auto-refresh
        runBackground(loadData(), 'data');
        const interval = setInterval(() => runBackground(loadData(), 'data'), 30000);
        return () => clearInterval(interval);
    }, [isFormActive]); // Re-run when isFormActive changes

    if (loading) {
        return (
            <div className="flex items-center justify-center h-96">
                <RefreshCw className="w-8 h-8 animate-spin text-primary-600" />
            </div>
        );
    }

    const { tabs, contributed } = mergeTabs(
        [
            { id: 'groups', label: 'Routing Groups', icon: Share2 },
            { id: 'status', label: 'Status', icon: Activity },
            { id: 'test', label: 'Test Router', icon: Zap },
        ],
        slotTabs,
    );

    return (
        <div className="h-full app-column">
            {/* Header - Fixed at top */}
            <div className="flex-shrink-0 px-6 pt-6 pb-4 bg-surface border-b border-default">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="text-2xl font-bold text-default">Routing Groups</h1>
                        <p className="text-sm text-secondary mt-1">
                            Intelligent request routing and load balancing across multiple models
                        </p>
                    </div>
                    <div className="flex items-center space-x-3">
                        {/* Auto-refresh pause indicator */}
                        {isFormActive && (
                            <div className="flex items-center space-x-2 px-3 py-2 bg-yellow-50 border border-yellow-200 rounded-lg">
                                <PauseCircle className="w-4 h-4 text-yellow-600" />
                                <span className="text-sm text-yellow-700">Auto-refresh paused</span>
                            </div>
                        )}

                        <button
                            onClick={asyncHandler(handleRefresh, 'click')}
                            disabled={refreshing}
                            className="flex items-center space-x-2 px-4 py-2 border border-strong text-secondary rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        >
                            <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                            <span>Refresh</span>
                        </button>
                    </div>
                </div>
            </div>

            {/* Main Content Area - Scrollable */}
            <div className="flex-1 px-6 py-6 overflow-hidden min-h-0 min-w-0">
                {/* Main Content with full width */}
                <div className="h-full app-column overflow-hidden">
                    {/* Tab Navigation - Fixed */}
                    <div className="flex-shrink-0 border-b border-default mb-6">
                        <nav className="-mb-px flex space-x-8">
                            {tabs.map((tab) => {
                                const Icon = tab.icon;
                                const isActive = activeView === tab.id;
                                return (
                                    <button
                                        key={tab.id}
                                        onClick={() => setActiveView(tab.id)}
                                        className={`
                                            flex items-center space-x-2 py-4 px-1 border-b-2 font-medium text-sm transition-colors
                                            ${isActive
                                            ? 'border-primary-500 text-primary-600'
                                            : 'border-transparent text-muted hover:text-secondary hover:border-strong'
                                        }
                                        `}
                                    >
                                        <Icon className="w-4 h-4" />
                                        <span>{tab.label}</span>
                                    </button>
                                );
                            })}
                        </nav>
                    </div>

                    {/* Tab Content - Scrollable */}
                    <div className="scroll-region overflow-x-hidden">
                        {activeView === 'groups' && (
                            <RoutingGroupsSubTab
                                onRefresh={asyncHandler(loadData, 'refresh')}
                                onFormStateChange={setIsFormActive}
                            />
                        )}

                        {activeView === 'status' && (
                            <RouterStatusSubTab
                                status={routerStatus}
                            />
                        )}

                        {activeView === 'test' && <RouterTestSubTab />}

                        {contributed.get(activeView)?.render()}
                    </div>
                </div>
            </div>
        </div>
    );
};

export default LLMRoutingTab;