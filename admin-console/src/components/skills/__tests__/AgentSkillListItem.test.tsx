/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One row of the skills list. Two rules live in its action menu and nowhere
 * else in the UI:
 *
 *   - a published skill is immutable, so the row offers "View", never "Edit".
 *     Offering Edit would send the operator into a panel whose Save the backend
 *     rejects — a dead end discovered only after they have typed something;
 *   - the lifecycle actions (Validate / Publish / Archive) are only shown when
 *     the parent actually passed a handler, because which of them is legal
 *     depends on the skill's current stage and only the parent knows that.
 *
 * `GroupTreeDisplay` is stubbed: this row's job is to hand it the skill's
 * groups, not to re-test how that component draws a tree.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../common/GroupTreeDisplay', () => ({
    default: ({ groups }: { groups: { id: string }[] }) => (
        <div data-testid="group-tree">{groups.length} groups</div>
    ),
}));

import AgentSkillListItem from '../AgentSkillListItem';
import type { AgentSkillConfig } from '../../../lib/agentSkillTypes';

const BASE = {
    id: 'skill-abc123',
    name: 'Invoice Extractor',
    description: 'Pulls line items out of PDF invoices',
    version: '1.4.0',
    status: 'draft',
    enabled: true,
    execution_count: 1234,
    groups: [{ id: 'g1' }, { id: 'g2' }],
    metadata: {},
} as unknown as AgentSkillConfig;

function setup(skill: Partial<AgentSkillConfig> = {}, props: Record<string, unknown> = {}) {
    const handlers = {
        onEdit: vi.fn(),
        onDelete: vi.fn(),
        onCopy: vi.fn(),
        onOpenTab: vi.fn(),
    };
    render(
        <AgentSkillListItem
            skill={{ ...BASE, ...skill }}
            {...handlers}
            {...props}
        />
    );
    return handlers;
}

const openMenu = () => {
    const buttons = screen.getAllByRole('button');
    fireEvent.click(buttons[buttons.length - 1]);
};

describe('AgentSkillListItem — the row', () => {
    it('identifies the skill by name, id and version', () => {
        setup();
        expect(screen.getByText('Invoice Extractor')).toBeInTheDocument();
        expect(screen.getByText('skill-abc123')).toBeInTheDocument();
        expect(screen.getByText('v1.4.0')).toBeInTheDocument();
    });

    it('formats the execution count for readability', () => {
        setup();
        expect(screen.getByText('1,234')).toBeInTheDocument();
    });

    it('opens the overview when the name is clicked', () => {
        const h = setup();
        fireEvent.click(screen.getByText('Invoice Extractor'));
        expect(h.onOpenTab).toHaveBeenCalledWith('overview');
    });

    it('dims a disabled skill', () => {
        const { container } = render(
            <AgentSkillListItem
                skill={{ ...BASE, enabled: false }}
                onEdit={vi.fn()}
                onDelete={vi.fn()}
                onCopy={vi.fn()}
                onOpenTab={vi.fn()}
            />
        );
        expect(container.firstElementChild?.className).toContain('opacity-60');
    });

    it('marks a composite skill so it is not mistaken for a leaf', () => {
        setup({ metadata: { skill_type: 'composite' } });
        expect(screen.getByText('Composite')).toBeInTheDocument();
    });

    it('shows a cost estimate when one exists and a dash when it does not', () => {
        setup({ metadata: { cost_estimate_usd: 0.125 } });
        expect(screen.getByText('$0.13')).toBeInTheDocument();

        setup();
        expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    });

    it('flags a skill gated on human approval, and says "Auto" otherwise', () => {
        setup({
            metadata: { approval_required: true },
        });
        expect(screen.getByTitle('Requires approval')).toBeInTheDocument();

        setup();
        expect(screen.getByText('Auto')).toBeInTheDocument();
    });

    it('passes the skill groups to the access display', () => {
        setup();
        expect(screen.getByTestId('group-tree')).toHaveTextContent('2 groups');
    });

    it('survives a skill with no groups at all', () => {
        setup({ groups: undefined });
        expect(screen.getByTestId('group-tree')).toHaveTextContent('0 groups');
    });
});

describe('AgentSkillListItem — bulk selection', () => {
    it('shows no checkbox outside bulk mode', () => {
        setup();
        expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    });

    it('labels the checkbox with the skill it selects', () => {
        const onToggleSelect = vi.fn();
        setup({}, { selectable: true, selected: false, onToggleSelect });

        const box = screen.getByRole('checkbox', { name: 'Select Invoice Extractor' });
        expect(box).not.toBeChecked();
        fireEvent.click(box);
        expect(onToggleSelect).toHaveBeenCalled();
    });

    it('reflects the selected state the parent owns', () => {
        setup({}, { selectable: true, selected: true, onToggleSelect: vi.fn() });
        expect(screen.getByRole('checkbox')).toBeChecked();
    });
});

describe('AgentSkillListItem — action menu', () => {
    it('offers Edit for a draft', () => {
        const h = setup();
        openMenu();
        fireEvent.click(screen.getByText('Edit'));
        expect(h.onEdit).toHaveBeenCalled();
    });

    it('replaces Edit with View once the skill is published', () => {
        // Published skills are immutable; an Edit entry would be a dead end.
        const h = setup({ status: 'published' });
        openMenu();
        expect(screen.queryByText('Edit')).not.toBeInTheDocument();

        fireEvent.click(screen.getByText('View'));
        expect(h.onOpenTab).toHaveBeenCalledWith('overview');
    });

    // A designed workflow does not run in this release: the builder is not offered.
    it('offers no Workflow shortcut, not even for a composite skill', () => {
        setup({ metadata: { skill_type: 'composite' } });
        openMenu();
        expect(screen.queryByText('Workflow')).not.toBeInTheDocument();
    });

    it('hides lifecycle actions the parent did not authorise', () => {
        setup();
        openMenu();
        expect(screen.queryByText('Validate')).not.toBeInTheDocument();
        expect(screen.queryByText('Publish')).not.toBeInTheDocument();
        expect(screen.queryByText('Archive')).not.toBeInTheDocument();
    });

    it('wires each lifecycle action the parent did pass', () => {
        const onValidate = vi.fn();
        const onPublish = vi.fn();
        const onArchive = vi.fn();
        setup({}, { onValidate, onPublish, onArchive });

        openMenu();
        fireEvent.click(screen.getByText('Validate'));
        expect(onValidate).toHaveBeenCalled();

        openMenu();
        fireEvent.click(screen.getByText('Publish'));
        expect(onPublish).toHaveBeenCalled();

        openMenu();
        fireEvent.click(screen.getByText('Archive'));
        expect(onArchive).toHaveBeenCalled();
    });

    it('always allows duplicate and delete', () => {
        const h = setup({ status: 'published' });
        openMenu();
        fireEvent.click(screen.getByText('Duplicate'));
        expect(h.onCopy).toHaveBeenCalled();

        openMenu();
        fireEvent.click(screen.getByText('Delete'));
        expect(h.onDelete).toHaveBeenCalled();
    });

    it('closes the menu after an action so the next click hits the row', () => {
        setup();
        openMenu();
        fireEvent.click(screen.getByText('Duplicate'));
        expect(screen.queryByText('Duplicate')).not.toBeInTheDocument();
    });

    it('closes on a backdrop click without firing anything', () => {
        const h = setup();
        openMenu();
        fireEvent.click(document.querySelector('.fixed.inset-0') as Element);

        expect(screen.queryByText('Duplicate')).not.toBeInTheDocument();
        expect(h.onCopy).not.toHaveBeenCalled();
        expect(h.onDelete).not.toHaveBeenCalled();
    });
});
