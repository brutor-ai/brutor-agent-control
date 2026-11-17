/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Icon picker for resource groups.
 *
 * Pinned: picking hands back the registry key (that is what gets stored), clear
 * hands back '' so the group falls back to its type's default, search and category
 * narrow the grid, and a disabled picker offers nothing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { ICON_REGISTRY } from '../../../lib/lucideIconMap';

import LucideIconPicker from '../LucideIconPicker';

const onChange = vi.fn();
const TOTAL = Object.keys(ICON_REGISTRY).length;

const openPicker = (value = '') => {
    render(<LucideIconPicker value={value} onChange={onChange} groupType="team" />);
    fireEvent.click(screen.getAllByRole('button')[0]);
};
const counter = () => screen.getByText(/\d+ icons?( ·.*)?$/);

describe('LucideIconPicker', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(cleanup);

    it('names the current icon, an unknown value as itself, and no value as Default', () => {
        render(<LucideIconPicker value="brain" onChange={onChange} />);
        expect(screen.getByText('Brain')).toBeInTheDocument();
        cleanup();
        render(<LucideIconPicker value="not-registered" onChange={onChange} />);
        expect(screen.getByText('not-registered')).toBeInTheDocument();
        cleanup();
        render(<LucideIconPicker value="" onChange={onChange} groupType="team" />);
        expect(screen.getByText('Default')).toBeInTheDocument();
    });

    it('offers every icon, and picking hands back its registry key and closes', () => {
        openPicker();
        expect(counter()).toHaveTextContent(`${TOTAL} icons`);
        fireEvent.click(screen.getByTitle('Brain'));
        expect(onChange).toHaveBeenCalledWith('brain');
        expect(screen.queryByPlaceholderText('Search icons...')).toBeNull();
    });

    it('searches by key, label or category, and says when nothing matches', () => {
        openPicker();
        const search = screen.getByPlaceholderText('Search icons...');
        fireEvent.change(search, { target: { value: 'BRAIN' } });
        expect(screen.getByTitle('Brain')).toBeInTheDocument();
        expect(Number(counter().textContent?.split(' ')[0])).toBeLessThan(TOTAL);

        fireEvent.change(search, { target: { value: 'tech' } });
        expect(screen.getByTitle('Brain')).toBeInTheDocument();

        fireEvent.change(search, { target: { value: 'zzzzzz' } });
        expect(counter()).toHaveTextContent('0 icons');
        expect(screen.queryByTitle('Brain')).toBeNull();
    });

    it('a category narrows the grid, a second click and All both clear it', () => {
        openPicker();
        const techCount = Object.values(ICON_REGISTRY).filter((e) => e.category === 'Tech').length;
        const tech = screen.getByRole('button', { name: 'Tech' });
        fireEvent.click(tech);
        expect(counter()).toHaveTextContent(`${techCount} icon`);
        fireEvent.click(tech);
        expect(counter()).toHaveTextContent(`${TOTAL} icons`);

        fireEvent.click(tech);
        fireEvent.click(screen.getByRole('button', { name: 'All' }));
        expect(counter()).toHaveTextContent(`${TOTAL} icons`);
    });

    it('clear hands back an empty name without toggling the dropdown open', () => {
        render(<LucideIconPicker value="brain" onChange={onChange} />);
        fireEvent.click(screen.getByTitle('Clear custom icon'));
        expect(onChange).toHaveBeenCalledWith('');
        expect(screen.queryByPlaceholderText('Search icons...')).toBeNull();
    });

    it('a disabled picker cannot be opened or cleared', () => {
        render(<LucideIconPicker value="brain" onChange={onChange} disabled />);
        expect(screen.queryByTitle('Clear custom icon')).toBeNull();
        fireEvent.click(screen.getByRole('button'));
        expect(screen.queryByPlaceholderText('Search icons...')).toBeNull();
    });

    it('closes when clicking outside', () => {
        openPicker();
        fireEvent.mouseDown(document.body);
        expect(screen.queryByPlaceholderText('Search icons...')).toBeNull();
    });
});
