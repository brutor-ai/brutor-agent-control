/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Group → Guardrails tab.
 *
 * A two-sub-tab container for everything a resource group is guarded
 * by: the guardrail configs assigned to it (directly or inherited) and
 * the argument policies bound to it. Both views are read-only — the
 * bindings are owned on the Governance side.
 */

import React, { useState, useEffect } from 'react';
import { ShieldCheck, Braces, ScrollText, Bot } from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useTenantTheme } from '../../../context/TenantThemeContext';
import GroupGuardrailsTab from './GroupGuardrailsTab';
import GroupArgumentPoliciesTab from './GroupArgumentPoliciesTab';
import GroupSemanticPoliciesTab from './GroupSemanticPoliciesTab';
import GroupAgentPoliciesTab from './GroupAgentPoliciesTab';

interface Props {
    group: ResourceGroup;
}

type SubTab = 'configuration' | 'argument-policies' | 'semantic-policies' | 'agent-policies';

const SUB_TAB_STORAGE_KEY = 'groupGuardrailsSubTab';

interface SubTabDef {
    id: SubTab;
    label: string;
    icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>;
}

const SUB_TABS: SubTabDef[] = [
    { id: 'configuration',     label: 'Configuration',     icon: ShieldCheck },
    { id: 'argument-policies', label: 'Argument Policies', icon: Braces },
    { id: 'semantic-policies', label: 'Semantic Policies', icon: ScrollText },
    { id: 'agent-policies',    label: 'Agent Policies',    icon: Bot },
];

const GroupGuardrailsPanel: React.FC<Props> = ({ group }) => {
    const { theme } = useTenantTheme();
    const primaryColor = theme.primary_color || '#1BB5E8';

    const [activeSubTab, setActiveSubTab] = useState<SubTab>(() => {
        try {
            const saved = localStorage.getItem(SUB_TAB_STORAGE_KEY);
            if (saved && SUB_TABS.some(t => t.id === saved)) return saved as SubTab;
        } catch { /* ignore */ }
        return 'configuration';
    });

    useEffect(() => {
        try { localStorage.setItem(SUB_TAB_STORAGE_KEY, activeSubTab); }
        catch { /* ignore */ }
    }, [activeSubTab]);

    const renderContent = () => {
        switch (activeSubTab) {
            case 'configuration':     return <GroupGuardrailsTab group={group} />;
            case 'argument-policies': return <GroupArgumentPoliciesTab group={group} />;
            case 'semantic-policies': return <GroupSemanticPoliciesTab group={group} />;
            case 'agent-policies':    return <GroupAgentPoliciesTab group={group} />;
            default:                  return null;
        }
    };

    return (
        <div className="app-column h-full">
            {/* Sub-tab bar */}
            <div className="px-6 pt-4 border-b border-default flex space-x-1">
                {SUB_TABS.map(tab => {
                    const TabIcon = tab.icon;
                    const active = activeSubTab === tab.id;
                    return (
                        <button
                            key={tab.id}
                            onClick={() => setActiveSubTab(tab.id)}
                            className={`
                                flex items-center space-x-1.5 px-4 py-2.5 text-sm font-medium
                                border-b-2 transition-colors whitespace-nowrap
                                ${active
                                    ? 'border-current text-default'
                                    : 'border-transparent text-muted hover:text-secondary hover:border-strong'
                                }
                            `}
                            style={active ? { color: primaryColor, borderColor: primaryColor } : undefined}
                        >
                            <TabIcon
                                className="w-4 h-4"
                                style={active ? { color: primaryColor } : undefined}
                            />
                            <span>{tab.label}</span>
                        </button>
                    );
                })}
            </div>

            {/* Sub-tab content */}
            <div className="scroll-region">
                {renderContent()}
            </div>
        </div>
    );
};

export default GroupGuardrailsPanel;
