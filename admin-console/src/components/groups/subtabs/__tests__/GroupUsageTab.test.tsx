/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Usage tab's include-descendants default.
 *
 * The load-bearing assertion: an ORGANIZATIONAL group opens aggregated
 * ("Include descendant groups" on) however the operator got here — an
 * org node's own direct usage is usually near zero, and a page of zeros
 * under a group holding twenty systems reads as "nothing happening".
 * An AI System (a leaf) keeps the direct view. The operator can still
 * toggle either way; only the DEFAULT is derived from the group type.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchGroupUsage = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({ fetchGroupUsage: mockFetchGroupUsage }),
}));

import GroupUsageTab from '../GroupUsageTab';
import { GroupType, ResourceGroup } from '../../../../lib/groupTypes';

function group(overrides: Partial<ResourceGroup> = {}): ResourceGroup {
    return {
        id: 'grp-1',
        name: 'Engineering',
        group_type: GroupType.DEPARTMENT,
        stats: { direct_children_count: 3 },
        ...overrides,
    } as ResourceGroup;
}

beforeEach(() => {
    vi.clearAllMocks();
    // The pane guards on a null response; the toggle and fetch args are
    // what these tests pin, not the charts.
    mockFetchGroupUsage.mockResolvedValue(null);
});

describe('GroupUsageTab include-descendants default', () => {
    it('an organizational group opens aggregated', async () => {
        render(<GroupUsageTab group={group()} />);
        await waitFor(() =>
            expect(mockFetchGroupUsage).toHaveBeenCalledWith(
                'grp-1',
                expect.objectContaining({ include_hierarchy: true }),
            ),
        );
        expect(screen.getByLabelText(/include descendant groups/i)).toBeChecked();
    });

    it('an AI System keeps the direct view', async () => {
        render(
            <GroupUsageTab
                group={group({ id: 'sys-1', group_type: GroupType.AI_SYSTEM })}
            />
        );
        await waitFor(() =>
            expect(mockFetchGroupUsage).toHaveBeenCalledWith(
                'sys-1',
                expect.objectContaining({ include_hierarchy: false }),
            ),
        );
        expect(screen.getByLabelText(/include descendant groups/i)).not.toBeChecked();
    });

    it('navigating to a different group re-derives the default', async () => {
        const { rerender } = render(<GroupUsageTab group={group()} />);
        await waitFor(() =>
            expect(mockFetchGroupUsage).toHaveBeenCalledWith(
                'grp-1',
                expect.objectContaining({ include_hierarchy: true }),
            ),
        );
        // The panel keeps one component instance across group navigation —
        // the org-group aggregation must not leak onto a leaf system.
        rerender(
            <GroupUsageTab
                group={group({ id: 'sys-1', group_type: GroupType.AI_SYSTEM })}
            />
        );
        await waitFor(() =>
            expect(mockFetchGroupUsage).toHaveBeenCalledWith(
                'sys-1',
                expect.objectContaining({ include_hierarchy: false }),
            ),
        );
    });

    it('the operator can still switch an org group to direct-only', async () => {
        render(<GroupUsageTab group={group()} />);
        const toggle = await screen.findByLabelText(/include descendant groups/i);
        fireEvent.click(toggle);
        await waitFor(() =>
            expect(mockFetchGroupUsage).toHaveBeenLastCalledWith(
                'grp-1',
                expect.objectContaining({ include_hierarchy: false }),
            ),
        );
        expect(toggle).not.toBeChecked();
    });
});
