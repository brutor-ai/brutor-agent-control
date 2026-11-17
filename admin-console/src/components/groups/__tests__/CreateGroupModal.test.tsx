/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two-step group-type picker: Organizational Unit / AI System /
 * Custom. The category answers "what is this thing?"; the subtype grid
 * (organizational only) answers "where does it sit?". A tenant that
 * never uses AI Systems must be able to build a pure org tree here.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        createResourceGroup: vi.fn().mockResolvedValue({ id: 'g-1' }),
        fetchGroupHierarchyTree: vi.fn().mockResolvedValue({ tree: [] }),
    }),
}));

// Pickers drag in ThemeProvider; the type picker under test doesn't need them.
vi.mock('../../common/IconColorPicker', () => ({ default: () => null }));
vi.mock('../../common/LucideIconPicker', () => ({ default: () => null }));

import CreateGroupModal from '../CreateGroupModal';

const noop = () => {};

describe('CreateGroupModal — group type categories', () => {
    it('offers the three categories', () => {
        render(<CreateGroupModal onClose={noop} onSuccess={noop} />);
        expect(screen.getByText('Organizational Unit')).toBeInTheDocument();
        expect(screen.getByText('AI System')).toBeInTheDocument();
        expect(screen.getByText('Custom')).toBeInTheDocument();
    });

    it('defaults to Custom with no subtype grid and no kind picker', () => {
        render(<CreateGroupModal onClose={noop} onSuccess={noop} />);
        expect(screen.queryByTestId('organizing-subtype-picker')).not.toBeInTheDocument();
        expect(screen.queryByTestId('system-kind-picker')).not.toBeInTheDocument();
    });

    it('organizational category opens the subtype grid, defaulting to Team', () => {
        render(<CreateGroupModal onClose={noop} onSuccess={noop} />);
        fireEvent.click(screen.getByText('Organizational Unit'));
        const grid = screen.getByTestId('organizing-subtype-picker');
        expect(grid).toBeInTheDocument();
        // All eight organisational levels are offered.
        for (const label of ['Organization', 'Division', 'Department', 'Team', 'Project']) {
            expect(screen.getAllByText(label).length).toBeGreaterThanOrEqual(1);
        }
        // Team preselected as the common case.
        expect(screen.getByText('Team').closest('button')).toHaveClass('border-primary-600');
    });

    it('subtype selection sticks within the organizational category', () => {
        render(<CreateGroupModal onClose={noop} onSuccess={noop} />);
        fireEvent.click(screen.getByText('Organizational Unit'));
        fireEvent.click(screen.getByText('Department'));
        expect(screen.getByText('Department').closest('button')).toHaveClass('border-primary-600');
        expect(screen.getByText('Team').closest('button')).not.toHaveClass('border-primary-600');
    });

    it('AI System category shows the kind picker instead of the subtype grid', () => {
        render(<CreateGroupModal onClose={noop} onSuccess={noop} />);
        fireEvent.click(screen.getByText('AI System'));
        expect(screen.getByTestId('system-kind-picker')).toBeInTheDocument();
        expect(screen.queryByTestId('organizing-subtype-picker')).not.toBeInTheDocument();
    });
});
