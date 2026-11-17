/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for GroupAuditTab.
 *
 * A resource group's tabs each own their own save — `onUpdate` here is a
 * refresh signal, not a setter — so this tab has a Save button the other
 * eight forms don't need. The properties worth defending follow from that:
 *
 *   - Save is explicit. Saving per keystroke would write an audit row for
 *     every letter typed into a governed resource.
 *   - Save is disabled until the field actually differs, so the button
 *     never promises a write it won't make.
 *   - Switching to a different group re-syncs the field, or it would keep
 *     showing the previous group's version.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockUpdate = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({ updateResourceGroup: mockUpdate }),
}));

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

import GroupAuditTab from '../GroupAuditTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

function group(overrides: Partial<ResourceGroup> = {}): ResourceGroup {
    return {
        id: 'rg-1',
        name: 'Engineering',
        version: '1.0',
        created_by: 'alice',
        created_at: '2026-07-01T10:00:00Z',
        updated_by: 'bob',
        updated_at: '2026-07-20T10:00:00Z',
        ...overrides,
    } as ResourceGroup;
}

describe('GroupAuditTab', () => {
    beforeEach(() => {
        mockUpdate.mockReset();
        mockUpdate.mockResolvedValue({});
    });

    it('shows the group version and authorship', () => {
        render(<GroupAuditTab group={group()} onUpdate={vi.fn()} />);
        expect(screen.getByTestId('resource-version-input')).toHaveValue('1.0');
        expect(screen.getByTestId('resource-authorship')).toHaveTextContent('alice');
        expect(screen.getByTestId('resource-authorship')).toHaveTextContent('bob');
    });

    it('keeps Save disabled until the version actually changes', () => {
        render(<GroupAuditTab group={group()} onUpdate={vi.fn()} />);
        expect(screen.getByTestId('group-audit-save')).toBeDisabled();

        fireEvent.change(screen.getByTestId('resource-version-input'), {
            target: { value: '2.0' },
        });
        expect(screen.getByTestId('group-audit-save')).toBeEnabled();
    });

    it('does not write on every keystroke', () => {
        render(<GroupAuditTab group={group()} onUpdate={vi.fn()} />);
        fireEvent.change(screen.getByTestId('resource-version-input'), {
            target: { value: '2' },
        });
        fireEvent.change(screen.getByTestId('resource-version-input'), {
            target: { value: '2.' },
        });
        expect(mockUpdate).not.toHaveBeenCalled();
    });

    it('saves the version and signals a refresh', async () => {
        const onUpdate = vi.fn();
        render(<GroupAuditTab group={group()} onUpdate={onUpdate} />);

        fireEvent.change(screen.getByTestId('resource-version-input'), {
            target: { value: '2.0' },
        });
        fireEvent.click(screen.getByTestId('group-audit-save'));

        await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('rg-1', { version: '2.0' }));
        await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    });

    it('leaves the edit in place when the save fails', async () => {
        mockUpdate.mockRejectedValue(new Error('nope'));
        const onUpdate = vi.fn();
        render(<GroupAuditTab group={group()} onUpdate={onUpdate} />);

        fireEvent.change(screen.getByTestId('resource-version-input'), {
            target: { value: '2.0' },
        });
        fireEvent.click(screen.getByTestId('group-audit-save'));

        // The operator's typing survives so they can retry rather than
        // re-enter it.
        await waitFor(() => expect(screen.getByTestId('group-audit-save')).toBeEnabled());
        expect(screen.getByTestId('resource-version-input')).toHaveValue('2.0');
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('re-syncs when the panel switches to another group', () => {
        const { rerender } = render(<GroupAuditTab group={group()} onUpdate={vi.fn()} />);
        rerender(
            <GroupAuditTab
                group={group({ id: 'rg-2', name: 'Finance', version: '3.5' })}
                onUpdate={vi.fn()}
            />,
        );
        expect(screen.getByTestId('resource-version-input')).toHaveValue('3.5');
    });

    it('falls back to the default label for a group with no version yet', () => {
        render(<GroupAuditTab group={group({ version: undefined })} onUpdate={vi.fn()} />);
        expect(screen.getByTestId('resource-version-input')).toHaveValue('1.0');
    });
});
