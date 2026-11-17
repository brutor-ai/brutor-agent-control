/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { CountersDeepLinkButton } from '../CountersDeepLinkButton';

describe('<CountersDeepLinkButton />', () => {
    it('renders nothing when input is null', () => {
        const { container } = render(<CountersDeepLinkButton input={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders an anchor with the deep-link URL when input is set', () => {
        render(
            <CountersDeepLinkButton
                input={{
                    scope: 'model',
                    subject: 'gpt-4o',
                    kind: 'llm_request_rate',
                    openDrawer: true,
                }}
                testId="t"
            />,
        );
        const a = screen.getByTestId('t');
        expect(a.tagName).toBe('A');
        const href = a.getAttribute('href') ?? '';
        expect(href.startsWith('/admin/system/counters?')).toBe(true);
        expect(href).toContain('cnt-scope=model');
        expect(href).toContain('cnt-subject=gpt-4o');
        expect(href).toContain('cnt-kind=llm_request_rate');
        expect(href).toContain('cnt-drawer=open');
    });

    it('honours custom label + title overrides', () => {
        render(
            <CountersDeepLinkButton
                input={{ scope: 'model', subject: 'gpt-4o' }}
                label="Open bucket"
                title="Custom hint"
                testId="t"
            />,
        );
        const a = screen.getByTestId('t');
        expect(a).toHaveTextContent('Open bucket');
        expect(a.getAttribute('title')).toBe('Custom hint');
    });

    it('uses default label when none supplied', () => {
        render(
            <CountersDeepLinkButton
                input={{ scope: 'model', subject: 'gpt-4o' }}
                testId="t"
            />,
        );
        expect(screen.getByTestId('t')).toHaveTextContent('View counters');
    });
});
