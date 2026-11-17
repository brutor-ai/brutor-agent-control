/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Governance tab's five-surface sub-tab bar.
 *
 * Its own storage key, deliberately: an operator sweeping MCP *policies*
 * across an estate should not have their Limits tab dragged to MCP as well.
 * The two bars look identical and remember separately.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: {} }),
}));

vi.mock('../GroupLLMGovernanceTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-llm">{group.id}</div>,
}));
vi.mock('../GroupMCPGovernanceTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-mcp">{group.id}</div>,
}));
vi.mock('../GroupSkillGovernanceTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-skill">{group.id}</div>,
}));
vi.mock('../GroupA2AGovernanceTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-a2a">{group.id}</div>,
}));
vi.mock('../GroupMediaGovernanceTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-media">{group.id}</div>,
}));

import GroupResourceGovernanceTab from '../GroupResourceGovernanceTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-1', name: 'engineering' } as ResourceGroup;
const STORAGE_KEY = 'resourceGovernanceSubTab';

beforeEach(() => {
    vi.clearAllMocks();
});

describe('GroupResourceGovernanceTab', () => {
    it('offers all five policy surfaces and opens on LLM', () => {
        render(<GroupResourceGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        for (const label of ['LLM Policies', 'MCP Policies', 'Skill Policies', 'Agent Policies', 'Media Policies']) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
        expect(screen.getByTestId('pane-llm')).toHaveTextContent('rg-1');
    });

    it('switching surface swaps the pane and records the choice', () => {
        render(<GroupResourceGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        fireEvent.click(screen.getByText('Media Policies'));
        expect(screen.getByTestId('pane-media')).toBeInTheDocument();
        expect(screen.queryByTestId('pane-llm')).toBeNull();
        expect(localStorage.getItem(STORAGE_KEY)).toBe('media');
    });

    it('remembers its surface under its OWN key, not the Limits tab’s', () => {
        localStorage.setItem('resourceLimitsSubTab', 'mcp');
        localStorage.setItem(STORAGE_KEY, 'skill');
        render(<GroupResourceGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        expect(screen.getByTestId('pane-skill')).toBeInTheDocument();
    });

    it('a stored surface we no longer offer falls back to LLM', () => {
        localStorage.setItem(STORAGE_KEY, 'telepathy');
        render(<GroupResourceGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        expect(screen.getByTestId('pane-llm')).toBeInTheDocument();
    });
});
