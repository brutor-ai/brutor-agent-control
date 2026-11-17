/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent findings sit in the inbox under "Agent" and link to
 * the agent identity's Implementation section (where a release is
 * approved), then to the system's composition.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import type { AssuranceInboxItem } from '../../../lib/assuranceInboxTypes';

const mockList = vi.fn();
const mockGet = vi.fn();

// The page keys its load on the client: one identity for the whole file, or
// every render hands the effect a new dependency and the page fetches for ever.
const stableGateway = {
    gatewayClient: {
        listAssuranceInbox: mockList,
        getAssuranceInboxCounts: vi.fn().mockResolvedValue({ open: 1, acknowledged: 0, resolved: 0, open_critical: 1 }),
        getAssuranceInboxItem: mockGet,
        acknowledgeAssuranceInboxItem: vi.fn(),
        resolveAssuranceInboxItem: vi.fn(),
        reopenAssuranceInboxItem: vi.fn(),
    },
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => stableGateway,
}));

import AssuranceInbox from '../AssuranceInbox';
import { SCREENS, renderScreen } from '../../../app/screens';
import { callsWhileIdle } from '../../../test/idle';

const AGENT_ITEM: AssuranceInboxItem = {
    id: 'inbox-a1',
    tenant_id: 't1',
    ai_system_id: 'sys-1',
    category: 'agent',
    severity: 'critical',
    fingerprint: 'agent:agent-1:agent.unapproved_release:screening-agent@1.6.0',
    title: 'Screening ran release screening-agent@1.6.0 (verified), which is not among its approved versions (1.5.*).',
    evidence: {
        code: 'agent.unapproved_release',
        agent_id: 'agent-1',
        release: { name: 'screening-agent', version: '1.6.0', trust: 'verified', approved: false },
        systems: ['sys-1'],
    },
    occurrence_count: 1,
    first_seen_at: '2026-09-27T10:00:00Z',
    last_seen_at: '2026-09-27T10:00:00Z',
    state: 'open',
    acknowledged_at: null,
    acknowledged_by: null,
    assignee_admin: null,
    disposition: null,
    resolution_note: null,
    resolved_at: null,
    resolved_by: null,
};

beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({ items: [AGENT_ITEM], total: 1, limit: 100, offset: 0 });
    mockGet.mockResolvedValue({ ...AGENT_ITEM, events: [] });
});

describe('AssuranceInbox — agent findings', () => {
    it('labels the category "Agent" in the row and offers it as a filter', async () => {
        render(<AssuranceInbox />);
        const row = await screen.findByTestId('inbox-row');
        expect(within(row).getByTestId('inbox-category-agent')).toHaveTextContent('Agent');
        expect(screen.getByRole('option', { name: 'Agent' })).toBeInTheDocument();
    });

    it('links to the identity’s Implementation section, then the composition', async () => {
        const setActiveTab = vi.fn();
        render(<AssuranceInbox setActiveTab={setActiveTab} />);
        fireEvent.click(await screen.findByTestId('inbox-row'));
        const drawer = await screen.findByTestId('inbox-detail');
        fireEvent.click(within(drawer).getByRole('button', { name: 'View agent implementation' }));
        expect(setActiveTab).toHaveBeenLastCalledWith('a2a-identities:agent-1:implementation');
        fireEvent.click(within(drawer).getByRole('button', { name: 'View composition' }));
        expect(setActiveTab).toHaveBeenLastCalledWith('ai-systems:sys-1:composition');
    });

    // An unhandled tab id is a silent no-op link (see AssuranceInbox.test's
    // KNOWN_TAB_IDS). The identity deep link is a prefix a screen must answer.
    it('the a2a-identities:<id>:<section> deep link opens the identity on that section', () => {
        const el = renderScreen(SCREENS, {
            activeTab: 'a2a-identities:agent-1:implementation',
            setActiveTab: vi.fn(),
            user: { username: 'admin' } as never,
            isFeatureEnabled: () => true,
            refreshGroupTree: vi.fn(),
        }) as React.ReactElement<Record<string, unknown>>;
        expect((el.type as { name?: string }).name).toBe('AgentIdentitiesResourceTab');
        expect(el.props).toMatchObject({ initialAgentId: 'agent-1', initialSection: 'implementation' });
    });
});

describe('AssuranceInbox — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        render(<AssuranceInbox />);
        await screen.findByTestId('inbox-row');
        expect(await callsWhileIdle([mockList])).toBe(0);
    });
});
