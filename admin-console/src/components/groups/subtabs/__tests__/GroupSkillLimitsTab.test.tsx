/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The owning tab around the skill-limits form: load, edit, save, cancel.
 *
 * The form is presentational; this tab is where the round trip lives, so the
 * properties worth pinning are the ones an operator would notice if they
 * broke:
 *
 *   - Save and Cancel appear only once there IS an edit. A permanently
 *     armed Save invites a write that changes nothing but still stamps an
 *     audit row on a governed resource.
 *   - Cancel re-reads from the server rather than trusting a local snapshot,
 *     so it cannot restore a value someone else has since changed.
 *   - A failed save leaves the edit on screen AND keeps the buttons, so the
 *     operator can retry instead of retyping.
 *   - The inheritance note only appears for a group that actually inherits.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchSkillGlobalLimits = vi.fn();
const mockUpdateSkillGlobalLimits = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchSkillGlobalLimits: mockFetchSkillGlobalLimits,
        updateSkillGlobalLimits: mockUpdateSkillGlobalLimits,
    }),
}));

import GroupSkillLimitsTab from '../GroupSkillLimitsTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const child = { id: 'rg-1', name: 'platform', parent_group_id: 'rg-0' } as ResourceGroup;
const root = { id: 'rg-9', name: 'acme' } as ResourceGroup;

function renderTab(group: ResourceGroup = child) {
    const onUpdate = vi.fn();
    render(<GroupSkillLimitsTab group={group} onUpdate={onUpdate} />);
    return { onUpdate };
}

const dailyInput = () => screen.getByTestId('limit-input-execution.daily_limit');
const saveButton = () => screen.queryByRole('button', { name: /save changes/i });

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchSkillGlobalLimits.mockResolvedValue({ skill_global_limits: {} });
    mockUpdateSkillGlobalLimits.mockResolvedValue({});
});

describe('GroupSkillLimitsTab — loading', () => {
    it('reads this group’s limits', async () => {
        mockFetchSkillGlobalLimits.mockResolvedValue({
            skill_global_limits: { execution: { daily_limit: 250 } },
        });
        renderTab();
        await waitFor(() => expect(mockFetchSkillGlobalLimits).toHaveBeenCalledWith('rg-1'));
        expect(await screen.findByTestId('limit-input-execution.daily_limit')).toHaveValue(250);
    });

    it('a group with nothing set opens on an empty, unsaved form', async () => {
        renderTab();
        expect(await screen.findByTestId('limit-input-execution.daily_limit')).toHaveValue(null);
        expect(saveButton()).toBeNull();
    });

    it('a failed load says why instead of showing zeros', async () => {
        mockFetchSkillGlobalLimits.mockRejectedValue(new Error('limits service down'));
        renderTab();
        expect(await screen.findByText('limits service down')).toBeInTheDocument();
    });

    it('re-reads when the panel switches to another group', async () => {
        const { rerender } = render(<GroupSkillLimitsTab group={child} onUpdate={vi.fn()} />);
        await waitFor(() => expect(mockFetchSkillGlobalLimits).toHaveBeenCalledWith('rg-1'));
        rerender(<GroupSkillLimitsTab group={root} onUpdate={vi.fn()} />);
        await waitFor(() => expect(mockFetchSkillGlobalLimits).toHaveBeenCalledWith('rg-9'));
    });
});

describe('GroupSkillLimitsTab — the inheritance note', () => {
    it('explains the inherited ceilings for a group that has a parent', async () => {
        mockFetchSkillGlobalLimits.mockResolvedValue({
            skill_global_limits: {},
            inherited_skill_limits: { execution: { daily_limit: 500 } },
        });
        renderTab();
        expect(await screen.findByText(/have inherited limits from parent groups/)).toBeInTheDocument();
    });

    it('says nothing about parents to a root group', async () => {
        mockFetchSkillGlobalLimits.mockResolvedValue({
            skill_global_limits: {},
            inherited_skill_limits: { execution: { daily_limit: 500 } },
        });
        renderTab(root);
        await screen.findByTestId('limit-input-execution.daily_limit');
        expect(screen.queryByText(/have inherited limits from parent groups/)).toBeNull();
    });

    it('always states the restrictive-merge rule, parent or not', async () => {
        renderTab(root);
        expect(await screen.findByText(/most\s+restrictive value across this group/)).toBeInTheDocument();
    });
});

describe('GroupSkillLimitsTab — editing and saving', () => {
    it('offers no Save until something is edited', async () => {
        renderTab();
        await screen.findByTestId('limit-input-execution.daily_limit');
        expect(saveButton()).toBeNull();

        fireEvent.change(dailyInput(), { target: { value: '100' } });
        expect(saveButton()).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
    });

    it('writes the whole limits dict and signals a refresh', async () => {
        mockFetchSkillGlobalLimits.mockResolvedValue({
            skill_global_limits: { execution: { monthly_limit: 3000 } },
        });
        const { onUpdate } = renderTab();
        await screen.findByTestId('limit-input-execution.daily_limit');
        fireEvent.change(dailyInput(), { target: { value: '100' } });
        fireEvent.click(saveButton() as HTMLElement);

        await waitFor(() =>
            expect(mockUpdateSkillGlobalLimits).toHaveBeenCalledWith('rg-1', {
                execution: { monthly_limit: 3000, daily_limit: 100 },
            }),
        );
        await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    });

    it('a saved form goes quiet again — nothing left to save', async () => {
        renderTab();
        await screen.findByTestId('limit-input-execution.daily_limit');
        fireEvent.change(dailyInput(), { target: { value: '100' } });
        fireEvent.click(saveButton() as HTMLElement);
        await waitFor(() => expect(saveButton()).toBeNull());
    });

    it('a failed save keeps the edit and the buttons so it can be retried', async () => {
        mockUpdateSkillGlobalLimits.mockRejectedValue(new Error('above the inherited limit'));
        const { onUpdate } = renderTab();
        await screen.findByTestId('limit-input-execution.daily_limit');
        fireEvent.change(dailyInput(), { target: { value: '100' } });
        fireEvent.click(saveButton() as HTMLElement);

        expect(await screen.findByText('above the inherited limit')).toBeInTheDocument();
        expect(dailyInput()).toHaveValue(100);
        expect(saveButton()).toBeInTheDocument();
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('cancel re-reads from the server rather than trusting a local snapshot', async () => {
        mockFetchSkillGlobalLimits.mockResolvedValue({
            skill_global_limits: { execution: { daily_limit: 10 } },
        });
        renderTab();
        await screen.findByTestId('limit-input-execution.daily_limit');
        fireEvent.change(dailyInput(), { target: { value: '999' } });

        // Someone else moved the cap while this operator was typing.
        mockFetchSkillGlobalLimits.mockResolvedValue({
            skill_global_limits: { execution: { daily_limit: 42 } },
        });
        fireEvent.click(screen.getByRole('button', { name: /cancel/i }));

        await waitFor(() => expect(dailyInput()).toHaveValue(42));
        expect(saveButton()).toBeNull();
    });
});
