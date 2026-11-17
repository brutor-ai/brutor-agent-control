/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for FormTabBar — the two-or-three tab bar the smaller resource
 * forms share so they don't each grow a slightly different one.
 *
 * The property that matters is accessibility rather than looks: a tab bar
 * that doesn't announce which tab is selected is unusable with a screen
 * reader, and that regresses silently.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import FormTabBar from '../FormTabBar';

const tabs = [
    { id: 'general', label: 'General' },
    { id: 'audit', label: 'Audit Info' },
] as const;

describe('FormTabBar', () => {
    it('renders one tab per definition', () => {
        render(<FormTabBar tabs={tabs} active="general" onChange={vi.fn()} />);
        expect(screen.getAllByRole('tab')).toHaveLength(2);
        expect(screen.getByTestId('form-tab-general')).toHaveTextContent('General');
        expect(screen.getByTestId('form-tab-audit')).toHaveTextContent('Audit Info');
    });

    it('marks only the active tab as selected', () => {
        render(<FormTabBar tabs={tabs} active="audit" onChange={vi.fn()} />);
        expect(screen.getByTestId('form-tab-audit')).toHaveAttribute('aria-selected', 'true');
        expect(screen.getByTestId('form-tab-general')).toHaveAttribute('aria-selected', 'false');
    });

    it('reports the tab that was clicked', () => {
        const onChange = vi.fn();
        render(<FormTabBar tabs={tabs} active="general" onChange={onChange} />);
        fireEvent.click(screen.getByTestId('form-tab-audit'));
        expect(onChange).toHaveBeenCalledWith('audit');
    });

    it('uses type=button so a tab click never submits the surrounding form', () => {
        // These bars live inside <form> elements; a default-type button
        // would submit the form on every tab switch.
        render(<FormTabBar tabs={tabs} active="general" onChange={vi.fn()} />);
        for (const tab of screen.getAllByRole('tab')) {
            expect(tab).toHaveAttribute('type', 'button');
        }
    });
});
