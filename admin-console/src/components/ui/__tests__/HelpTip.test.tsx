/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * HelpTip — the click-to-open help marker. The load-bearing behaviour
 * is that help arrives on CLICK (a title attribute only works on
 * hover, so a "?" carrying one reads as broken).
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import { HelpTip } from '../HelpTip';

describe('HelpTip', () => {
    it('opens on click and shows the help text', async () => {
        render(<HelpTip title="Liveness" text="The only signal that fires when a system stops calling." />);
        expect(screen.queryByTestId('help-tip-popover')).toBeNull();
        fireEvent.click(screen.getByTestId('help-tip'));
        const pop = await screen.findByTestId('help-tip-popover');
        expect(pop).toHaveTextContent('Liveness');
        expect(pop).toHaveTextContent('stops calling');
    });

    it('closes on outside click', async () => {
        render(
            <div>
                <button type="button">elsewhere</button>
                <HelpTip text="hint" />
            </div>
        );
        fireEvent.click(screen.getByTestId('help-tip'));
        await screen.findByTestId('help-tip-popover');
        fireEvent.mouseDown(screen.getByText('elsewhere'));
        await waitFor(() => {
            expect(screen.queryByTestId('help-tip-popover')).toBeNull();
        });
    });

    it('escapes clipping ancestors by rendering into the body', async () => {
        // Absolutely positioned inside the marker, the panel on the rightmost
        // column of a table would be clipped by the overflow-x-auto
        // wrapper — unreachable at any scroll position.
        render(
            <div style={{ overflowX: 'auto' }} data-testid="clipper">
                <HelpTip text="hint" />
            </div>
        );
        fireEvent.click(screen.getByTestId('help-tip'));
        const pop = await screen.findByTestId('help-tip-popover');
        expect(screen.getByTestId('clipper').contains(pop)).toBe(false);
        expect(pop.parentElement).toBe(document.body);
        expect(pop).toHaveStyle({ position: 'fixed' });
    });

    it('flips to right-aligned rather than opening past the viewport edge', async () => {
        render(<HelpTip text="hint" />);
        const marker = screen.getByTestId('help-tip');
        // A marker 40px from the right edge: opening rightward would put a
        // 288px panel 248px off-screen.
        vi.spyOn(marker.parentElement as HTMLElement, 'getBoundingClientRect').mockReturnValue({
            left: 984, right: 1000, top: 40, bottom: 56,
        } as DOMRect);

        fireEvent.click(marker);
        const pop = await screen.findByTestId('help-tip-popover');
        const left = parseFloat(pop.style.left);
        expect(left + 288).toBeLessThanOrEqual(window.innerWidth);
        expect(left).toBeGreaterThanOrEqual(8);
    });

    it('closes on Escape', async () => {
        render(<HelpTip text="hint" />);
        fireEvent.click(screen.getByTestId('help-tip'));
        await screen.findByTestId('help-tip-popover');
        fireEvent.keyDown(document, { key: 'Escape' });
        await waitFor(() => {
            expect(screen.queryByTestId('help-tip-popover')).toBeNull();
        });
    });
});
