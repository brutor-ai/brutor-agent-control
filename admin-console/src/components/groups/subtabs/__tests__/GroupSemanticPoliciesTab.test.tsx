/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The read-only list of semantic policies bound to a group.
 *
 * A semantic policy is judged at runtime by an LLM, and the two facts that
 * decide what a violation actually DOES are separate: the mode (enforce vs
 * shadow) and the action (block vs warn). A policy that is switched off has
 * neither, so it must read as "inactive" rather than as the mode it would
 * have run in — an operator counting what is in force must not count a
 * disabled rule.
 *
 * A policy with no surfaces listed applies everywhere, which is the widest
 * possible reach and therefore must be stated ("All surfaces"), not left as
 * an empty cell that reads like "none".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockListSemanticPolicies = vi.fn();

vi.mock('../../../../hooks/useGateway', () => {
    const gatewayClient = { listSemanticPolicies: (...a: unknown[]): unknown => mockListSemanticPolicies(...a) };
    return { useGateway: () => ({ gatewayClient }) };
});

import GroupSemanticPoliciesTab from '../GroupSemanticPoliciesTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import type { SemanticPolicy } from '../../../../lib/semanticPolicyTypes';

const GROUP = { id: 'rg-1', name: 'engineering' } as ResourceGroup;

function policy(overrides: Partial<SemanticPolicy> = {}): SemanticPolicy {
    return {
        id: 'sp-1',
        name: 'No competitor comparisons',
        description: 'Refuses head-to-head claims about named competitors',
        constraint_text: 'The response must not compare our product to a named competitor.',
        action: 'block',
        mode: 'enforce',
        enabled: true,
        surfaces: ['chat_input'],
        resource_group_ids: ['rg-1'],
        ...overrides,
    };
}

const renderTab = () => render(<GroupSemanticPoliciesTab group={GROUP} />);

/** The mode pill in the table — the same words appear in the explainer. */
const BADGE = 'td span.rounded-full';

beforeEach(() => {
    vi.clearAllMocks();
    mockListSemanticPolicies.mockResolvedValue({ policies: [] });
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('GroupSemanticPoliciesTab — what it lists', () => {
    it('says it is loading before claiming anything about coverage', () => {
        mockListSemanticPolicies.mockReturnValue(new Promise(() => { /* never settles */ }));
        renderTab();
        expect(screen.getByText(/Loading semantic policies/)).toBeInTheDocument();
        expect(screen.queryByText('No Semantic Policies Apply')).toBeNull();
    });

    it('shows only the policies bound to this group', async () => {
        mockListSemanticPolicies.mockResolvedValue({
            policies: [
                policy(),
                policy({ id: 'sp-2', name: 'Another group rule', resource_group_ids: ['rg-9'] }),
            ],
        });
        renderTab();
        expect(await screen.findByText('No competitor comparisons')).toBeInTheDocument();
        expect(screen.queryByText('Another group rule')).toBeNull();
        expect(screen.getByText('1 policy apply to this group')).toBeInTheDocument();
    });

    it('a failed load says so instead of reading as "nothing governs this"', async () => {
        mockListSemanticPolicies.mockRejectedValue(new Error('judge unavailable'));
        renderTab();
        expect(await screen.findByText('Failed to load semantic policies')).toBeInTheDocument();
        expect(screen.getByText('judge unavailable')).toBeInTheDocument();
        expect(screen.queryByText('No Semantic Policies Apply')).toBeNull();
    });

    it('survives a response with no policies field', async () => {
        mockListSemanticPolicies.mockResolvedValue({});
        renderTab();
        expect(await screen.findByText('No Semantic Policies Apply')).toBeInTheDocument();
    });

    it('an empty binding set points at where bindings are made', async () => {
        renderTab();
        expect(await screen.findByText('No Semantic Policies Apply')).toBeInTheDocument();
        expect(
            screen.getByText(/Bind a policy from Governance → Semantic Policies → Access Control/),
        ).toBeInTheDocument();
    });
});

describe('GroupSemanticPoliciesTab — reading a row', () => {
    it('shows the natural-language constraint the judge will apply', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: [policy()] });
        renderTab();
        expect(
            await screen.findByText('The response must not compare our product to a named competitor.'),
        ).toBeInTheDocument();
        expect(screen.getByText('Refuses head-to-head claims about named competitors')).toBeInTheDocument();
    });

    it('names each surface the policy is scoped to', async () => {
        mockListSemanticPolicies.mockResolvedValue({
            policies: [policy({ surfaces: ['chat_input', 'a2a_outbound'] })],
        });
        renderTab();
        expect(await screen.findByText('Chat input')).toBeInTheDocument();
        expect(screen.getByText('A2A outbound')).toBeInTheDocument();
    });

    it('a policy scoped to nothing is scoped to EVERYTHING, and says so', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: [policy({ surfaces: [] })] });
        renderTab();
        expect(await screen.findByText('All surfaces')).toBeInTheDocument();
    });

    it('spells out the consequence of a violation', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: [policy({ action: 'block' })] });
        renderTab();
        expect(await screen.findByText('Block (403)')).toBeInTheDocument();
    });

    it('separates enforce from shadow', async () => {
        mockListSemanticPolicies.mockResolvedValue({
            policies: [policy(), policy({ id: 'sp-2', name: 'Shadowed rule', mode: 'shadow' })],
        });
        renderTab();
        // The words also appear in the explainer banner, so match the badge.
        expect(await screen.findByText('enforce', { selector: BADGE })).toBeInTheDocument();
        expect(screen.getByText('shadow', { selector: BADGE })).toBeInTheDocument();
    });

    it('a switched-off policy reads as inactive, not as the mode it would run in', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: [policy({ enabled: false })] });
        renderTab();
        expect(await screen.findByText('inactive', { selector: BADGE })).toBeInTheDocument();
        expect(screen.queryByText('enforce', { selector: BADGE })).toBeNull();
    });
});

describe('GroupSemanticPoliciesTab — searching', () => {
    const many = [
        policy({ id: 'p1', name: 'Competitor guard', constraint_text: 'no competitor claims' }),
        policy({ id: 'p2', name: 'PII guard', constraint_text: 'must not reveal personal data' }),
        policy({ id: 'p3', name: 'Legal guard', constraint_text: 'no legal advice', description: 'counsel only' }),
        policy({ id: 'p4', name: 'Medical guard', constraint_text: 'no diagnosis' }),
    ];

    it('offers no search box for a list short enough to read', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: [policy()] });
        renderTab();
        await screen.findByText('No competitor comparisons');
        expect(screen.queryByPlaceholderText('Search...')).toBeNull();
    });

    it('filters on the name, the description and the constraint text', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: many });
        renderTab();
        const box = await screen.findByPlaceholderText('Search...');

        fireEvent.change(box, { target: { value: 'medical' } });
        expect(screen.getByText('Medical guard')).toBeInTheDocument();
        expect(screen.queryByText('PII guard')).toBeNull();

        fireEvent.change(box, { target: { value: 'personal data' } });
        expect(screen.getByText('PII guard')).toBeInTheDocument();

        fireEvent.change(box, { target: { value: 'counsel' } });
        expect(screen.getByText('Legal guard')).toBeInTheDocument();
    });

    it('a search with no hits says so and keeps the bound total honest', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: many });
        renderTab();
        fireEvent.change(await screen.findByPlaceholderText('Search...'), { target: { value: 'zzz' } });
        expect(screen.getByText('No Matches')).toBeInTheDocument();
        expect(screen.getByText('4 policies apply to this group')).toBeInTheDocument();
    });

    it('clearing the search brings the whole list back', async () => {
        mockListSemanticPolicies.mockResolvedValue({ policies: many });
        renderTab();
        const box = await screen.findByPlaceholderText('Search...');
        fireEvent.change(box, { target: { value: 'medical' } });
        fireEvent.click(screen.getByRole('button'));
        await waitFor(() => expect(screen.getByText('PII guard')).toBeInTheDocument());
        expect(box).toHaveValue('');
    });
});
