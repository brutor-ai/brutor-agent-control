/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../AssuranceInbox', () => ({ default: () => <div data-testid="inbox-stub" /> }));
vi.mock('../AssuranceChecks', () => ({ default: () => <div data-testid="checks-stub" /> }));
vi.mock('../AssuranceDigestPanel', () => ({ default: () => <div data-testid="digest-stub" /> }));

import AssuranceInboxPage from '../AssuranceInboxPage';

describe('AssuranceInboxPage', () => {
    it('holds the inbox, the continuous checks and the weekly summary, in that order', () => {
        render(<AssuranceInboxPage />);
        expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Inbox');
        const page = screen.getByTestId('assurance-inbox-page');
        const order = ['inbox-stub', 'checks-stub', 'digest-stub'].map((id) =>
            Array.from(page.querySelectorAll('[data-testid]')).findIndex((el) => el.getAttribute('data-testid') === id),
        );
        expect(order).toEqual([...order].sort((a, b) => a - b));
        expect(order.every((i) => i >= 0)).toBe(true);
    });
});
