/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where an administrator chooses how agents authenticate, the console shows
 * the options side by side: what each proves, what a copy of it can do, and
 * the guide that compares them. Short, and accurate to the levels the
 * gateway assigns: an option no credential can reach is not offered.
 */

import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import { KeyTradeOffs, LevelTradeOffs, TradeOffList } from '../IdentityTradeOffs';
import { ASSURANCE_LEVELS, CHOOSING_IDENTITY_URL, KEY_TRADE_OFFS, LEVEL_TRADE_OFFS, OFFERED_LEVELS } from '../../../lib/identityPolicy';

describe('the choosing guide', () => {
    it('is the docs page that compares the options', () => {
        expect(CHOOSING_IDENTITY_URL).toBe('https://docs.brutor.ai/build-an-agent/identity/choosing/');
    });
});

describe('LevelTradeOffs', () => {
    it('lists every credential level a credential can reach, weakest first, with what it proves and what a copy can do', () => {
        expect(LEVEL_TRADE_OFFS.map((row) => row.option)).toEqual([...ASSURANCE_LEVELS]);
        render(<LevelTradeOffs />);
        const table = screen.getByTestId('trade-offs-levels');
        expect(within(table).getAllByTestId(/^trade-off-/).map((row) => row.dataset.testid)).toEqual(['trade-off-key', 'trade-off-workload', 'trade-off-workload_pop']);
        expect(within(table).getByTestId('trade-off-key')).toHaveTextContent(/anywhere until it is revoked/);
        expect(within(table).getByTestId('trade-off-workload')).toHaveTextContent(/until the token expires/);
        expect(within(table).getByTestId('trade-off-workload_pop')).toHaveTextContent(/Useless without the private key/);
    });

    it('does not offer the bound key level: no credential reaches it yet', () => {
        // Network-bound keys are not available: a policy at that level would
        // refuse every API key while reading as though one could meet it.
        expect(OFFERED_LEVELS).toEqual(['key', 'workload', 'workload_pop']);
        render(<LevelTradeOffs />);
        expect(screen.queryByTestId('trade-off-bound_key')).not.toBeInTheDocument();
        expect(screen.getByTestId('trade-offs-levels')).not.toHaveTextContent(/bound|allowed network/i);
    });

    it('still explains the bound key level where a stored policy holds it', () => {
        render(<LevelTradeOffs selected="bound_key" />);
        expect(screen.getByTestId('trade-off-bound_key')).toHaveAttribute('data-selected', 'true');
        expect(screen.getByTestId('trade-off-bound_key')).toHaveTextContent(/allowed network.*not which workload/);
    });

    it('marks the level the policy requires, and links the guide', () => {
        render(<LevelTradeOffs selected="workload" />);
        expect(screen.getByTestId('trade-off-workload')).toHaveAttribute('data-selected', 'true');
        expect(screen.getByTestId('trade-off-key')).toHaveAttribute('data-selected', 'false');
        const link = screen.getByRole('link', { name: /choosing how agents authenticate/i });
        expect(link).toHaveAttribute('href', CHOOSING_IDENTITY_URL);
        expect(link).toHaveAttribute('target', '_blank');
        expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
    });
});

describe('KeyTradeOffs', () => {
    it('compares an API key with workload identity and says what production needs', () => {
        expect(KEY_TRADE_OFFS.map((row) => row.option)).toEqual(['key', 'workload']);
        render(<KeyTradeOffs />);
        const box = screen.getByTestId('trade-offs-keys');
        expect(within(box).getByTestId('trade-off-key')).toHaveTextContent(/API key/);
        expect(within(box).getByTestId('trade-off-workload')).toHaveTextContent(/Workload identity/);
        const note = within(box).getByTestId('trade-offs-note');
        expect(note).toHaveTextContent(/production requires workload identity/i);
        expect(within(box).getByRole('link')).toHaveAttribute('href', CHOOSING_IDENTITY_URL);
    });

    it('offers nothing that is not available: no bound key, no network-bound keys in production', () => {
        render(<KeyTradeOffs />);
        const box = screen.getByTestId('trade-offs-keys');
        expect(within(box).queryByTestId('trade-off-bound_key')).not.toBeInTheDocument();
        expect(box).not.toHaveTextContent(/bound|allowed network|tenant setting|attributed by network/i);
    });
});

describe('TradeOffList', () => {
    it('renders any options it is given: the identity provider presets use it too', () => {
        render(
            <TradeOffList
                testId="trade-offs-presets"
                rows={[{ option: 'spiffe', label: 'SPIFFE', proves: 'p', copied: 'c' }]}
                note="n"
            />,
        );
        expect(screen.getByTestId('trade-offs-presets')).toHaveTextContent('SPIFFE');
        expect(screen.getByTestId('trade-offs-note')).toHaveTextContent('n');
        expect(screen.getByRole('link')).toHaveAttribute('href', CHOOSING_IDENTITY_URL);
    });
});
