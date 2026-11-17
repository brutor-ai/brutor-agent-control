/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The record of the screen registry, shared by the tests of the two editions.
 *
 * A record is a JSON list with one row per tab id a registry lists: the
 * component the screen renders, the tenant flag it is behind and whether it
 * is padded. The Community record (`app/__tests__/screens.recorded.json`)
 * holds the screens of `app/screens.tsx` and is checked by
 * `app/__tests__/screens.test.tsx`; the Enterprise record, beside the
 * extension, holds the screens the extension adds and is checked by the
 * Enterprise test. A screen is added, removed or changed together with its
 * row.
 */

import React from 'react';
import { expect, it, vi } from 'vitest';

import { renderScreen, type ScreenContext, type ScreenRoute } from '../app/screens';
import communityRecord from '../app/__tests__/screens.recorded.json';

export interface Recorded {
    id: string;
    component: string;
    /** Tenant feature flag the screen is behind, if any. */
    flag: string | null;
    /** `featureName` shown on the disabled page when the flag is off. */
    disabledName: string | null;
    /** Wrapped in the padded full-height container. */
    padded: boolean;
}

/** The rows of the Community registry. */
export const COMMUNITY_ROWS = communityRecord as Recorded[];

export function ctx(activeTab: string, enabled: (flag: string) => boolean = () => true): ScreenContext {
    return {
        activeTab,
        setActiveTab: vi.fn(),
        user: { username: 'admin' } as ScreenContext['user'],
        isFeatureEnabled: enabled,
        refreshGroupTree: vi.fn(),
    };
}

export function element(node: React.ReactNode): React.ReactElement<Record<string, unknown>> {
    expect(React.isValidElement(node)).toBe(true);
    return node as React.ReactElement<Record<string, unknown>>;
}

/** The component name of an element, through `memo` and `forwardRef`. */
export function nameOf(el: React.ReactElement): string {
    const type = el.type as unknown;
    if (typeof type === 'string') return type;
    const t = type as {
        displayName?: string;
        name?: string;
        type?: { displayName?: string; name?: string };
        render?: { displayName?: string; name?: string };
    };
    return (
        t.displayName || t.name || t.type?.displayName || t.type?.name || t.render?.displayName || t.render?.name || ''
    );
}

/** A function that renders the screen of `screens` a tab id resolves to. */
export function rendererOf(screens: readonly ScreenRoute[]) {
    return (tab: string, enabled?: (flag: string) => boolean) => element(renderScreen(screens, ctx(tab, enabled)));
}

/** The screen component inside the padded full-height container. */
export function unwrapPadded(el: React.ReactElement<Record<string, unknown>>) {
    return element(el.props.children as React.ReactNode);
}

/**
 * The cases every recorded row gets, against `screens`. Called inside a
 * `describe`. `page` is how to get from what a screen renders to its page
 * where an edition puts something around it (the Enterprise screens of a
 * licensed feature); by default what the screen renders is the page.
 */
export function recordedScreenCases(
    screens: readonly ScreenRoute[],
    rows: Recorded[],
    page: (el: React.ReactElement<Record<string, unknown>>, row: Recorded) => React.ReactElement<Record<string, unknown>> = (el) => el,
): void {
    const rendered = rendererOf(screens);
    const render = (id: string, enabled?: (flag: string) => boolean) => {
        const row = rows.find((r) => r.id === id);
        const el = rendered(id, enabled);
        // The disabled page is shown bare: nothing is put around it.
        return row && nameOf(el) !== 'FeatureDisabled' ? page(el, row) : el;
    };

    it.each(rows.filter((r) => !r.padded))('$id renders $component', (row) => {
        expect(nameOf(render(row.id))).toBe(row.component);
    });

    it.each(rows.filter((r) => r.padded))('$id renders $component in the padded container', (row) => {
        const el = render(row.id);
        expect(el.type).toBe('div');
        expect(el.props.className).toBe('p-6 h-full');
        expect(nameOf(unwrapPadded(el))).toBe(row.component);
    });

    it.each(rows.filter((r) => r.flag))('$id shows the disabled page when $flag is off', (row) => {
        const el = render(row.id, (flag) => flag !== row.flag);
        expect(nameOf(el)).toBe('FeatureDisabled');
        expect(el.props.featureName).toBe(row.disabledName);
    });

    it.each(rows.filter((r) => !r.flag))('$id ignores tenant feature flags', (row) => {
        const el = render(row.id, () => false);
        expect(nameOf(row.padded ? unwrapPadded(el) : el)).toBe(row.component);
    });
}

/** A navigation entry, as far as these tests read it. */
export type NavNode = { id: string; children?: NavNode[] };

/** The entries the sidebar navigates to: the ones without children, at every depth. */
export function navLeaves(nodes: NavNode[]): string[] {
    return nodes.flatMap((n) => (n.children?.length ? navLeaves(n.children) : [n.id]));
}

/** The entries that have children, at every depth. */
export function navParents(nodes: NavNode[]): string[] {
    return nodes.flatMap((n) => (n.children?.length ? [n.id, ...navParents(n.children)] : []));
}
