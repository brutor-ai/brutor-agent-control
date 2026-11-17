/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Guardrails panel — everything a group is guarded BY, in four
 * read-only views: the guardrail configs assigned to it, and the argument,
 * semantic and agent policies bound to it.
 *
 * They are four separate enforcement mechanisms, not four filters over one
 * list, so the strip must never render two at once — an operator reading
 * "what stops this group doing X" has to know which mechanism they are
 * looking at. The chosen view persists under its own key so a sweep across
 * an estate stays on one mechanism.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: { primary_color: '#1BB5E8' } }),
}));

vi.mock('../GroupGuardrailsTab', () => ({
    default: ({ group }: { group: { id: string } }) => <div data-testid="pane-configuration">{group.id}</div>,
}));
vi.mock('../GroupArgumentPoliciesTab', () => ({
    default: () => <div data-testid="pane-argument" />,
}));
vi.mock('../GroupSemanticPoliciesTab', () => ({
    default: () => <div data-testid="pane-semantic" />,
}));
vi.mock('../GroupAgentPoliciesTab', () => ({
    default: () => <div data-testid="pane-agent" />,
}));

import GroupGuardrailsPanel from '../GroupGuardrailsPanel';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-1', name: 'engineering' } as ResourceGroup;
const STORAGE_KEY = 'groupGuardrailsSubTab';

const renderPanel = () => render(<GroupGuardrailsPanel group={GROUP} />);

beforeEach(() => {
    vi.clearAllMocks();
});

describe('GroupGuardrailsPanel', () => {
    it('offers all four enforcement views and opens on Configuration', () => {
        renderPanel();
        for (const label of ['Configuration', 'Argument Policies', 'Semantic Policies', 'Agent Policies']) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
        expect(screen.getByTestId('pane-configuration')).toHaveTextContent('rg-1');
    });

    it('shows exactly one mechanism at a time', () => {
        renderPanel();
        fireEvent.click(screen.getByText('Semantic Policies'));
        expect(screen.getByTestId('pane-semantic')).toBeInTheDocument();
        expect(screen.queryByTestId('pane-configuration')).toBeNull();
        expect(screen.queryByTestId('pane-argument')).toBeNull();
        expect(screen.queryByTestId('pane-agent')).toBeNull();
    });

    it('reaches each of the four views', () => {
        renderPanel();
        fireEvent.click(screen.getByText('Argument Policies'));
        expect(screen.getByTestId('pane-argument')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Agent Policies'));
        expect(screen.getByTestId('pane-agent')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Configuration'));
        expect(screen.getByTestId('pane-configuration')).toBeInTheDocument();
    });

    it('remembers the chosen view across a navigation', () => {
        const { unmount } = renderPanel();
        fireEvent.click(screen.getByText('Agent Policies'));
        expect(localStorage.getItem(STORAGE_KEY)).toBe('agent-policies');
        unmount();

        renderPanel();
        expect(screen.getByTestId('pane-agent')).toBeInTheDocument();
    });

    it('remembers under its OWN key, not another strip’s', () => {
        localStorage.setItem('resourceGovernanceSubTab', 'mcp');
        localStorage.setItem(STORAGE_KEY, 'semantic-policies');
        renderPanel();
        expect(screen.getByTestId('pane-semantic')).toBeInTheDocument();
    });

    it('a stored view we no longer offer falls back to Configuration', () => {
        localStorage.setItem(STORAGE_KEY, 'telepathy-policies');
        renderPanel();
        expect(screen.getByTestId('pane-configuration')).toBeInTheDocument();
    });
});
