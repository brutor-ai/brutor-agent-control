/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Links to pages the console may not have (`app/links.ts`).
 */

import { describe, expect, it } from 'vitest';
import { renderHook } from '@testing-library/react';
import type React from 'react';

import { BUILTIN_SLOTS } from '../builtinSlots';
import { composeAdmin } from '../extensions';
import { CONTRIBUTED_LINKS, NO_CONTRIBUTED_LINKS, linkResolver, useContributedLinks } from '../links';
import { SlotProvider } from '../SlotProvider';

describe('links to contributed pages', () => {
    it('nothing is answered with nothing contributed, in or out of a provider', () => {
        expect(linkResolver([])).toBe(NO_CONTRIBUTED_LINKS);
        for (const link of Object.values(CONTRIBUTED_LINKS)) expect(NO_CONTRIBUTED_LINKS(link)).toBeNull();

        const bare = renderHook(() => useContributedLinks());
        expect(bare.result.current(CONTRIBUTED_LINKS.knowledgeBases)).toBeNull();

        const wrapper = ({ children }: { children: React.ReactNode }) => <SlotProvider slots={BUILTIN_SLOTS}>{children}</SlotProvider>;
        const builtin = renderHook(() => useContributedLinks(), { wrapper });
        for (const link of Object.values(CONTRIBUTED_LINKS)) expect(builtin.result.current(link)).toBeNull();
    });

    it('the Community console answers none: it contributes no link', () => {
        expect(composeAdmin([]).slots['app.links'] ?? []).toEqual([]);
    });

    it('a contributed item answers its link with its tab; the first to answer wins; others stay unanswered', () => {
        const resolve = linkResolver([
            { id: CONTRIBUTED_LINKS.knowledgeBases, tab: 'first' },
            { id: CONTRIBUTED_LINKS.knowledgeBases, tab: 'second' },
        ]);
        expect(resolve(CONTRIBUTED_LINKS.knowledgeBases)).toBe('first');
        expect(resolve(CONTRIBUTED_LINKS.cachePolicies)).toBeNull();
        expect(resolve('anything-else')).toBeNull();
    });

    it('the hook answers from the nearest provider, and keeps one resolver while the items are the same', () => {
        const slots = { 'app.links': [{ id: CONTRIBUTED_LINKS.cachePolicies, tab: 'their-cache-page' }] };
        const wrapper = ({ children }: { children: React.ReactNode }) => <SlotProvider slots={slots}>{children}</SlotProvider>;
        const { result, rerender } = renderHook(() => useContributedLinks(), { wrapper });
        expect(result.current(CONTRIBUTED_LINKS.cachePolicies)).toBe('their-cache-page');
        const first = result.current;
        rerender();
        expect(result.current).toBe(first);
    });

    it('the names asked for are distinct', () => {
        const names = Object.values(CONTRIBUTED_LINKS);
        expect(new Set(names).size).toBe(names.length);
    });
});
