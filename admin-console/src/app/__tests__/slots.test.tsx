/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Slots (`app/slots.ts`) and the items this application ships in them
 * (`app/builtinSlots.tsx`).
 *
 * The host pages have their own tests for what they show with the built-in
 * items and with none. Here: how contributed tabs are placed, what a page
 * gets outside a provider, and that the built-in items are well formed and
 * are the only way the pieces they wrap reach a page.
 */


import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { SlotProvider } from '../SlotProvider';
import { mergeTabs, useSlot, type SlotName, type SlotRegistry } from '../slots';
import { scanRoots } from '../../test/sourceRoots';
import { importers, sources } from '../../test/slotImporters';

const tab = (id: string, after?: string) => ({ id, after });
const ids = (tabs: { id: string }[]) => tabs.map((t) => t.id);

describe('mergeTabs', () => {
    const host = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];

    it('with nothing contributed the tabs are the host tabs', () => {
        const merged = mergeTabs(host, []);
        expect(ids(merged.tabs)).toEqual(['a', 'b', 'c']);
        expect(merged.contributed.size).toBe(0);
    });

    it('places a tab directly after the tab it names', () => {
        expect(ids(mergeTabs(host, [tab('x', 'a')]).tabs)).toEqual(['a', 'x', 'b', 'c']);
        expect(ids(mergeTabs(host, [tab('x', 'c')]).tabs)).toEqual(['a', 'b', 'c', 'x']);
    });

    it('tabs naming the same tab keep the order they were contributed in', () => {
        expect(ids(mergeTabs(host, [tab('x', 'a'), tab('y', 'a'), tab('z', 'a')]).tabs)).toEqual([
            'a', 'x', 'y', 'z', 'b', 'c',
        ]);
    });

    it('a tab can be placed after a contributed tab', () => {
        expect(ids(mergeTabs(host, [tab('x', 'a'), tab('y', 'x')]).tabs)).toEqual(['a', 'x', 'y', 'b', 'c']);
    });

    it('a tab with no anchor, or one that does not exist, goes last', () => {
        expect(ids(mergeTabs(host, [tab('x'), tab('y', 'nope'), tab('z', 'a')]).tabs)).toEqual([
            'a', 'z', 'b', 'c', 'x', 'y',
        ]);
    });

    it('a contributed tab cannot take the id of a tab that is already there', () => {
        const merged = mergeTabs(host, [tab('b', 'a'), tab('x', 'a'), tab('x', 'c')]);
        expect(ids(merged.tabs)).toEqual(['a', 'x', 'b', 'c']);
        expect([...merged.contributed.keys()]).toEqual(['x']);
        expect(merged.contributed.get('x')).toEqual(tab('x', 'a'));
    });

    it('does not change the lists it is given', () => {
        const items = [tab('x', 'a')];
        mergeTabs(host, items);
        expect(ids(host)).toEqual(['a', 'b', 'c']);
        expect(items).toEqual([tab('x', 'a')]);
    });
});

describe('useSlot', () => {
    const Probe: React.FC<{ name: SlotName }> = ({ name }) => (
        <div data-testid="ids">{useSlot(name).map((item) => item.id).join(',')}</div>
    );

    it('is empty outside a provider', () => {
        render(<Probe name="settings.tabs" />);
        expect(screen.getByTestId('ids')).toHaveTextContent(/^$/);
    });

    // With the Enterprise extension's own items: `enterprise/src/host/app/__tests__/slots.test.tsx`.
    it('returns the items a provider holds for that slot (a fake one here), and nothing for another', () => {
        const slots: SlotRegistry = {
            'settings.tabs': [{ id: 'fake-tab', label: 'Fake', icon: () => null, render: () => null }],
        };
        const { rerender } = render(<SlotProvider slots={slots}><Probe name="settings.tabs" /></SlotProvider>);
        expect(screen.getByTestId('ids')).toHaveTextContent('fake-tab');
        rerender(<SlotProvider slots={slots}><Probe name="llm-routing.tabs" /></SlotProvider>);
        expect(screen.getByTestId('ids')).toHaveTextContent(/^$/);
    });
});

describe('a slotted piece reaches a page only through its slot', () => {

    // Both editions' trees: an import of a slotted piece is found wherever it is written.
    const files = scanRoots((root) => sources(root));

    it('reads the source tree', () => {
        expect(files.length).toBeGreaterThan(500);
        // The scan finds an import that is known to exist.
        expect(importers(files, 'app/slots')).toContain('components/ConfigurationTab.tsx');
    });
});
