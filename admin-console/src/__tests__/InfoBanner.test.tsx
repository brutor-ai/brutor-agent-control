/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * InfoBanner — locks down the canonical shape and the
 * per-variant Tailwind token sets. The colour tokens are remapped
 * for dark mode by the shared theme tokens (`src/ui-style/css/tokens.css`),
 * so as long as the tokens land on the rendered DOM the dark-mode
 * appearance is guaranteed by the global CSS layer.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import InfoBanner from '../components/common/InfoBanner';

describe('InfoBanner', () => {
    it('defaults to the info variant (blue tokens) with an Info icon', () => {
        const { container } = render(
            <InfoBanner title="About this thing">
                <p>Some explanation.</p>
            </InfoBanner>
        );
        const outer = container.firstChild as HTMLElement;
        expect(outer.className).toMatch(/bg-blue-50/);
        expect(outer.className).toMatch(/border-blue-200/);
        expect(outer.className).toMatch(/rounded-md/);
        expect(outer.className).toMatch(/p-4/);
        // Body text token (blue-700) sits on the inner wrapper.
        expect(outer.innerHTML).toMatch(/text-blue-700/);
        expect(outer.innerHTML).toMatch(/text-blue-600/);
        // Title + children rendered.
        expect(screen.getByText('About this thing')).toBeTruthy();
        expect(screen.getByText('Some explanation.')).toBeTruthy();
    });

    it('warning variant uses amber tokens', () => {
        const { container } = render(
            <InfoBanner variant="warning" title="Heads up">Be careful.</InfoBanner>
        );
        const outer = container.firstChild as HTMLElement;
        expect(outer.className).toMatch(/bg-amber-50/);
        expect(outer.className).toMatch(/border-amber-200/);
        expect(outer.innerHTML).toMatch(/text-amber-700/);
    });

    it('error variant uses red tokens', () => {
        const { container } = render(
            <InfoBanner variant="error">Something broke.</InfoBanner>
        );
        const outer = container.firstChild as HTMLElement;
        expect(outer.className).toMatch(/bg-red-50/);
        expect(outer.className).toMatch(/border-red-200/);
    });

    it('success variant uses green tokens', () => {
        const { container } = render(
            <InfoBanner variant="success">Saved.</InfoBanner>
        );
        const outer = container.firstChild as HTMLElement;
        expect(outer.className).toMatch(/bg-green-50/);
        expect(outer.className).toMatch(/border-green-200/);
    });

    it('renders without a title when omitted', () => {
        render(<InfoBanner>Body only.</InfoBanner>);
        expect(screen.getByText('Body only.')).toBeTruthy();
        // No <p class="font-medium ..."> when title is absent.
        const headings = document.querySelectorAll('p.font-medium');
        expect(headings.length).toBe(0);
    });

    it('accepts a custom icon override (for domain glyphs like Layers)', () => {
        const CustomIcon = () => (
            <svg data-testid="custom-icon" aria-hidden="true" />
        );
        render(
            <InfoBanner icon={<CustomIcon />} title="VMCP">
                Body.
            </InfoBanner>
        );
        expect(screen.getByTestId('custom-icon')).toBeTruthy();
    });

    it('merges extra className onto the outer container', () => {
        const { container } = render(
            <InfoBanner className="mb-0">Body.</InfoBanner>
        );
        const outer = container.firstChild as HTMLElement;
        expect(outer.className).toMatch(/mb-0/);
    });
});
