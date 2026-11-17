/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Limits tab's five-surface sub-tab bar.
 *
 * Limits are set per surface (LLM / MCP / Skill / Agent / Media) and an
 * operator working through an estate visits the same surface on group after
 * group. The chosen surface therefore persists in localStorage — losing it
 * on every navigation would make a twenty-group sweep twenty extra clicks.
 *
 * The stored value is operator-writable state read back at mount, so it is
 * validated against the known surfaces: a stale or hand-edited key must fall
 * back to LLM rather than render an empty pane.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: { primary_color: '#1BB5E8' } }),
}));

vi.mock('../GroupLLMLimitsTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-llm">{group.id}</div>,
}));
vi.mock('../GroupMCPLimitsTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-mcp">{group.id}</div>,
}));
vi.mock('../GroupSkillLimitsTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-skill">{group.id}</div>,
}));
vi.mock('../GroupA2ALimitsTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-a2a">{group.id}</div>,
}));
vi.mock('../GroupMediaLimitsTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-media">{group.id}</div>,
}));

import GroupResourceLimitsTab from '../GroupResourceLimitsTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-1', name: 'engineering' } as ResourceGroup;
const STORAGE_KEY = 'resourceLimitsSubTab';

const renderTab = () => {
    const onUpdate = vi.fn();
    render(<GroupResourceLimitsTab group={GROUP} onUpdate={onUpdate} />);
    return { onUpdate };
};

beforeEach(() => {
    vi.clearAllMocks();
});

describe('GroupResourceLimitsTab', () => {
    it('offers all five limit surfaces', () => {
        renderTab();
        for (const label of ['LLM Limits', 'MCP Limits', 'Skill Limits', 'Agent Limits', 'Media Limits']) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
    });

    it('opens on LLM and renders only that pane', () => {
        renderTab();
        expect(screen.getByTestId('pane-llm')).toHaveTextContent('rg-1');
        expect(screen.queryByTestId('pane-mcp')).toBeNull();
    });

    it('switching surface swaps the pane', () => {
        renderTab();
        fireEvent.click(screen.getByText('MCP Limits'));
        expect(screen.getByTestId('pane-mcp')).toBeInTheDocument();
        expect(screen.queryByTestId('pane-llm')).toBeNull();
    });

    it('remembers the chosen surface across a navigation', () => {
        const { unmount } = render(<GroupResourceLimitsTab group={GROUP} onUpdate={vi.fn()} />);
        fireEvent.click(screen.getByText('Skill Limits'));
        expect(localStorage.getItem(STORAGE_KEY)).toBe('skill');
        unmount();

        renderTab();
        expect(screen.getByTestId('pane-skill')).toBeInTheDocument();
    });

    it('falls back to LLM when the stored surface is not one we know', () => {
        localStorage.setItem(STORAGE_KEY, 'quantum');
        renderTab();
        expect(screen.getByTestId('pane-llm')).toBeInTheDocument();
    });

    it('passes the refresh signal down to the active pane', () => {
        renderTab();
        fireEvent.click(screen.getByText('Agent Limits'));
        expect(screen.getByTestId('pane-a2a')).toBeInTheDocument();
        expect(localStorage.getItem(STORAGE_KEY)).toBe('a2a');
    });
});
