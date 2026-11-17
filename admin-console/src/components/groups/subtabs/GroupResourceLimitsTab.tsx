/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { Brain, Server, Sparkles, Workflow, Film } from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useTenantTheme } from '../../../context/TenantThemeContext';
import GroupLLMLimitsTab from './GroupLLMLimitsTab';
import GroupMCPLimitsTab from './GroupMCPLimitsTab';
import GroupSkillLimitsTab from './GroupSkillLimitsTab';
import GroupA2ALimitsTab from './GroupA2ALimitsTab';
import GroupMediaLimitsTab from './GroupMediaLimitsTab';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

type SubTab = 'llm' | 'mcp' | 'skill' | 'a2a' | 'media';

const SUB_TAB_STORAGE_KEY = 'resourceLimitsSubTab';

interface SubTabDef {
    id: SubTab;
    label: string;
    icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>;
}

const SUB_TABS: SubTabDef[] = [
    { id: 'llm',   label: 'LLM Limits',          icon: Brain },
    { id: 'mcp',   label: 'MCP Limits',           icon: Server },
    { id: 'skill', label: 'Skill Limits',         icon: Sparkles },
    { id: 'a2a',   label: 'Agent Limits',         icon: Workflow },
    { id: 'media', label: 'Media Limits',         icon: Film },
];

const GroupResourceLimitsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { theme } = useTenantTheme();
    const primaryColor = theme.primary_color || '#1BB5E8';

    const [activeSubTab, setActiveSubTab] = useState<SubTab>(() => {
        try {
            const saved = localStorage.getItem(SUB_TAB_STORAGE_KEY);
            if (saved && SUB_TABS.some(t => t.id === saved)) return saved as SubTab;
        } catch { /* ignore */ }
        return 'llm';
    });

    useEffect(() => {
        try { localStorage.setItem(SUB_TAB_STORAGE_KEY, activeSubTab); }
        catch { /* ignore */ }
    }, [activeSubTab]);

    const renderContent = () => {
        switch (activeSubTab) {
            case 'llm':   return <GroupLLMLimitsTab group={group} onUpdate={onUpdate} />;
            case 'mcp':   return <GroupMCPLimitsTab group={group} onUpdate={onUpdate} />;
            case 'skill': return <GroupSkillLimitsTab group={group} onUpdate={onUpdate} />;
            case 'a2a':   return <GroupA2ALimitsTab group={group} onUpdate={onUpdate} />;
            case 'media': return <GroupMediaLimitsTab group={group} onUpdate={onUpdate} />;
            default:      return null;
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

export default GroupResourceLimitsTab;
