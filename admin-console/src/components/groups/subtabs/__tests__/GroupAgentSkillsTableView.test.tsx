/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The skills bound to a group.
 *
 * A skill row carries two independent facts that read alike and mean very
 * different things: its lifecycle STATUS (draft / validated / published /
 * deprecated — where in its life the skill is) and its ENABLED flag (whether
 * this binding will actually run). A published-but-disabled skill is a
 * perfectly ordinary state, so the table must show both rather than folding
 * them into one badge.
 *
 * As on every other resource table, an inherited binding belongs to an
 * ancestor: it can be inspected here but never removed here.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

import GroupAgentSkillsTableView from '../GroupAgentSkillsTableView';
import type { AgentSkillInfo } from '../../../../lib/groupTypes';

function skill(overrides: Partial<AgentSkillInfo> = {}): AgentSkillInfo {
    return {
        id: 's-1',
        name: 'revenue-forecast',
        description: 'Forecasts pipeline revenue',
        version: '1.2.0',
        status: 'published',
        enabled: true,
        execution_count: 1234,
        is_inherited: false,
        ...overrides,
    };
}

function renderTable(
    skills: AgentSkillInfo[],
    props: Partial<React.ComponentProps<typeof GroupAgentSkillsTableView>> = {},
) {
    const onRemove = vi.fn();
    const view = render(
        <GroupAgentSkillsTableView skills={skills} onRemove={onRemove} removingId={null} {...props} />,
    );
    return { onRemove, ...view };
}

const rowFor = (slug: string) => screen.getByText(slug).closest('tr') as HTMLElement;

describe('GroupAgentSkillsTableView — reading a row', () => {
    it('shows a human name over the machine slug', () => {
        renderTable([skill()]);
        const row = rowFor('revenue-forecast');
        expect(within(row).getByText('Revenue Forecast')).toBeInTheDocument();
        expect(within(row).getByText('v1.2.0')).toBeInTheDocument();
    });

    it('shows the category when the skill declares one', () => {
        renderTable([skill({ category: 'finance' })]);
        expect(within(rowFor('revenue-forecast')).getByText('finance')).toBeInTheDocument();
    });

    it('a skill with no description says so rather than leaving a gap', () => {
        renderTable([skill({ description: undefined })]);
        expect(within(rowFor('revenue-forecast')).getByText('No description')).toBeInTheDocument();
    });

    it('formats a large execution count for reading', () => {
        renderTable([skill({ execution_count: 1234567 })]);
        expect(within(rowFor('revenue-forecast')).getByText('1,234,567')).toBeInTheDocument();
    });

    it('counts what it is showing, singular and plural', () => {
        const { rerender } = renderTable([skill()]);
        expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 1 skill');
        rerender(
            <GroupAgentSkillsTableView
                skills={[skill(), skill({ id: 's-2', name: 'churn-risk' })]}
                onRemove={vi.fn()}
                removingId={null}
            />,
        );
        expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 2 skills');
    });
});

describe('GroupAgentSkillsTableView — status is not the same as enabled', () => {
    it.each([
        ['draft', 'Draft'],
        ['validated', 'Validated'],
        ['published', 'Published'],
        ['deprecated', 'Deprecated'],
    ])('lifecycle status %s reads as "%s"', (status, label) => {
        renderTable([skill({ status })]);
        expect(within(rowFor('revenue-forecast')).getByText(label)).toBeInTheDocument();
    });

    it('an unrecognised status is shown verbatim rather than swallowed', () => {
        renderTable([skill({ status: 'quarantined' })]);
        expect(within(rowFor('revenue-forecast')).getByText('quarantined')).toBeInTheDocument();
    });

    it('a published skill can still be switched off, and says both', () => {
        renderTable([skill({ status: 'published', enabled: false })]);
        const row = rowFor('revenue-forecast');
        expect(within(row).getByText('Published')).toBeInTheDocument();
        expect(within(row).getByText('Disabled')).toBeInTheDocument();
    });

    it('an enabled skill says so', () => {
        renderTable([skill()]);
        expect(within(rowFor('revenue-forecast')).getByText('Enabled')).toBeInTheDocument();
    });
});

describe('GroupAgentSkillsTableView — actions follow binding ownership', () => {
    it('separates direct bindings from inherited ones', () => {
        renderTable([skill(), skill({ id: 's-2', name: 'churn-risk', is_inherited: true })]);
        expect(within(rowFor('revenue-forecast')).getByText('Direct')).toBeInTheDocument();
        expect(within(rowFor('churn-risk')).getByText('Inherited')).toBeInTheDocument();
    });

    it('removal is offered on a direct binding only', () => {
        renderTable([skill(), skill({ id: 's-2', name: 'churn-risk', is_inherited: true })]);
        expect(within(rowFor('revenue-forecast')).getByTitle('Remove skill from group')).toBeInTheDocument();
        expect(within(rowFor('churn-risk')).queryByTitle('Remove skill from group')).toBeNull();
    });

    it('removal reports the id and slug so the caller can confirm', () => {
        const { onRemove } = renderTable([skill()]);
        fireEvent.click(within(rowFor('revenue-forecast')).getByTitle('Remove skill from group'));
        expect(onRemove).toHaveBeenCalledWith('s-1', 'revenue-forecast');
    });

    it('a removal in flight disables that row’s button only', () => {
        renderTable([skill(), skill({ id: 's-2', name: 'churn-risk' })], { removingId: 's-1' });
        expect(within(rowFor('revenue-forecast')).getByTitle('Remove skill from group')).toBeDisabled();
        expect(within(rowFor('churn-risk')).getByTitle('Remove skill from group')).toBeEnabled();
    });

    it('says on the row whether governance and limits are already in force', () => {
        renderTable([skill({ has_governance: true, has_limits: true })]);
        const row = rowFor('revenue-forecast');
        expect(within(row).getByTitle('Governance active — click to configure')).toBeInTheDocument();
        expect(within(row).getByTitle('Per-skill limits active — click to configure')).toBeInTheDocument();
    });

    it('hands the whole skill to the governance and limits dialogs', () => {
        const onConfigureGovernance = vi.fn();
        const onConfigureLimits = vi.fn();
        const s = skill();
        renderTable([s], { onConfigureGovernance, onConfigureLimits });
        const row = rowFor('revenue-forecast');

        fireEvent.click(within(row).getByTitle('Configure governance'));
        expect(onConfigureGovernance).toHaveBeenCalledWith(s);

        fireEvent.click(within(row).getByTitle('Configure per-skill limits'));
        expect(onConfigureLimits).toHaveBeenCalledWith(s);
    });
});
