/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * `<App extensions={[...]} />`.
 *
 * A test-only extension, defined here and nowhere in `src/app`, contributes
 * a screen, a navigation section, a slot item and an API client module;
 * each takes effect where the built-in one is read. With no extensions the
 * composition is the built-in registries. A contribution that collides with
 * what exists is refused as a whole, with nothing installed.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import React from 'react';
import { render, renderHook, screen as dom } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: { primary_color: '#1bb5e8', secondary_color: '#87ceeb' } }),
}));
vi.mock('../../hooks/useTheme', () => ({
    useTheme: () => ({ theme: 'light', setTheme: vi.fn() }),
}));

import AppSidebar from '../../components/AppSidebar';
import { GatewayApiBase } from '../../lib/gatewayApi/base';
import { installedApiModules, BUILTIN_API_MODULES } from '../../lib/gatewayApi/modules';
import { GatewayApiClient } from '../../lib/gatewayApiClient';
import { BUILTIN_SLOTS } from '../builtinSlots';
import {
    ADMIN_EXTENSION_INTERFACE_VERSION,
    composeAdmin,
    navIds,
    type AdminExtension,
    type NavPlacement,
} from '../extensions';
import { NAV_SECTIONS, type NavSection } from '../navigation';
import { SCREENS, renderScreen, resolveScreen, type ScreenContext } from '../screens';
import { SlotProvider } from '../SlotProvider';
import { useSlot } from '../slots';

// ---------------------------------------------------------------------------
// The test-only extension
// ---------------------------------------------------------------------------

class ReportsApi extends GatewayApiBase {
    fetchReports(): Promise<string[]> {
        return this.request<string[]>('/v1/ext/reports');
    }
}

const Icon: React.FC<{ className?: string }> = () => <span data-testid="ext-icon" />;

const REPORTS_NAV: NavSection = {
    label: 'Ext Reports Section',
    items: [{ id: 'ext-reports', label: 'Ext Reports Item', icon: Icon }],
};

/** The version every extension of this file declares, unless the test is about the version. */
const V = ADMIN_EXTENSION_INTERFACE_VERSION;

const REPORTS: AdminExtension = {
    name: 'reports',
    interfaceVersion: V,
    screens: [{ name: 'ext-reports', ids: ['ext-reports'], prefixes: ['ext-report:'], render: ({ activeTab }) => <div data-testid="ext-screen">{activeTab}</div> }],
    navigation: [REPORTS_NAV],
    slots: { 'settings.tabs': [{ id: 'ext-reports', label: 'Reports', icon: Icon, render: () => <div>reports</div> }] },
    apiModules: [{ name: 'reports', feature: null, mixin: ReportsApi }],
};

const ctx = (activeTab: string): ScreenContext => ({
    activeTab,
    setActiveTab: vi.fn(),
    user: { username: 'admin' } as unknown as ScreenContext['user'],
    isFeatureEnabled: () => true,
    refreshGroupTree: vi.fn(),
});

function sidebar(sections?: readonly NavSection[]) {
    return (
        <AppSidebar
            sections={sections}
            activeTab="mission-control"
            onTabChange={vi.fn()}
            username="admin"
            tenantId="t1"
            isTenantAdmin
            onLogout={vi.fn()}
            hasErrors={false}
            errorCount={0}
            hasCriticalErrors={false}
            groupTree={[]}
            groupTreeLoading={false}
            onRefreshGroupTree={vi.fn()}
            hasPermission={() => true}
        />
    );
}

// ---------------------------------------------------------------------------

describe('composeAdmin', () => {
    it('with no extensions is the built-in registries and nothing else', () => {
        for (const composition of [composeAdmin(), composeAdmin([])]) {
            expect(composition.screens).toEqual(SCREENS);
            expect(composition.navSections).toEqual(NAV_SECTIONS);
            expect(composition.slots).toEqual(BUILTIN_SLOTS);
            expect(composition.extensions).toEqual([]);
        }
        expect(installedApiModules(GatewayApiClient)).toEqual(BUILTIN_API_MODULES.map((m) => m.name));
    });

    it('an extension’s screen is resolved after the built-in ones, by id and by prefix', () => {
        const { screens } = composeAdmin([REPORTS]);
        expect(screens.slice(0, SCREENS.length)).toEqual(SCREENS);
        expect(screens.map((s) => s.name).slice(SCREENS.length)).toEqual(['ext-reports']);
        expect(resolveScreen(screens, 'ext-reports')?.name).toBe('ext-reports');
        expect(resolveScreen(screens, 'ext-report:42')?.name).toBe('ext-reports');
        // and a built-in id still answers as before
        expect(resolveScreen(screens, 'mission-control')?.name).toBe(resolveScreen(SCREENS, 'mission-control')?.name);
        render(<>{renderScreen(screens, ctx('ext-report:42'))}</>);
        expect(dom.getByTestId('ext-screen')).toHaveTextContent('ext-report:42');
        // without the extension the id falls to the fallback screen, not to this one
        expect(resolveScreen(SCREENS, 'ext-reports')).toBeUndefined();
    });

    it('two extensions cannot claim one tab id, and an id of a built-in parent entry is free to claim', () => {
        // `async-jobs` is a navigation parent: no built-in screen answers it.
        expect(resolveScreen(composeAdmin([]).screens, 'async-jobs')).toBeUndefined();
        const jobs: AdminExtension = {
            name: 'jobs',
            interfaceVersion: V,
            screens: [{ name: 'ext-jobs', ids: ['async-jobs', 'ext-jobs'], render: () => <div data-testid="ext-jobs" /> }],
        };
        const { screens } = composeAdmin([REPORTS, jobs]);
        expect(resolveScreen(screens, 'async-jobs')?.name).toBe('ext-jobs');
        expect(resolveScreen(screens, 'video-gen-jobs')?.name).toBe('video-gen-jobs');
        const again: AdminExtension = { name: 'again', interfaceVersion: V, screens: [{ name: 'again', ids: ['async-jobs'], render: () => null }] };
        expect(() => composeAdmin([jobs, again])).toThrow(/tab id already claimed by a screen: async-jobs/);
    });

    it('a deep-link prefix wins over a listed id, whatever the order', () => {
        const route = (name: string, claims: { ids?: string[]; prefixes?: string[] }) => ({
            name,
            ...claims,
            render: () => null,
        });
        const listed = route('listed', { ids: ['deep:link'] });
        const deep = route('deep', { prefixes: ['deep:'] });
        expect(resolveScreen([listed, deep], 'deep:link')?.name).toBe('deep');
        expect(resolveScreen([deep, listed], 'deep:link')?.name).toBe('deep');
        expect(resolveScreen([listed], 'deep:link:more')).toBeUndefined();
    });

    it('an extension’s navigation section is shown after the built-in ones', () => {
        const { navSections } = composeAdmin([REPORTS]);
        expect(navSections.slice(0, NAV_SECTIONS.length)).toEqual(NAV_SECTIONS);
        expect(navSections[navSections.length - 1]).toBe(REPORTS_NAV);
        render(sidebar(navSections));
        expect(dom.getByText('Ext Reports Section')).toBeInTheDocument();
        expect(dom.getByText('Ext Reports Item')).toBeInTheDocument();
        expect(dom.getByTestId('ext-icon')).toBeInTheDocument();
    });

    it('the sidebar without a sections prop shows the built-in navigation only', () => {
        render(sidebar());
        expect(dom.queryByText('Ext Reports Item')).toBeNull();
        expect(dom.getByText('Mission Control')).toBeInTheDocument();
    });

    it('an extension’s slot item is appended to the slot’s built-in items', () => {
        const { slots } = composeAdmin([REPORTS]);
        const builtin = BUILTIN_SLOTS['settings.tabs'] ?? [];
        const { result } = renderHook(() => useSlot('settings.tabs'), {
            wrapper: ({ children }) => <SlotProvider slots={slots}>{children}</SlotProvider>,
        });
        expect(result.current.slice(0, builtin.length)).toEqual(builtin);
        expect(result.current.map((i) => i.id).slice(builtin.length)).toEqual(['ext-reports']);
        // other slots are the built-in ones
        const untouched = Object.entries(BUILTIN_SLOTS)
            .filter(([name]) => name !== 'settings.tabs')
            .map(([name, items]) => [name, slots[name as keyof typeof slots] === items]);
        expect(untouched.filter(([, same]) => !same)).toEqual([]);
    });

    it('an extension’s API module is installed on the client once, however many times it is composed', () => {
        composeAdmin([REPORTS]);
        composeAdmin([REPORTS]);
        const names = installedApiModules(GatewayApiClient);
        expect(names.filter((n) => n === 'reports')).toEqual(['reports']);
        expect(names[names.length - 1]).toBe('reports');
        const client = new GatewayApiClient('http://cp.test') as GatewayApiClient & ReportsApi;
        expect(typeof client.fetchReports).toBe('function');
    });

    it('navIds walks every level of the tree', () => {
        const ids = navIds([
            {
                label: null,
                items: [{ id: 'a', label: 'a', icon: Icon, children: [{ id: 'b', label: 'b', icon: Icon, children: [{ id: 'c', label: 'c', icon: Icon, children: [{ id: 'd', label: 'd', icon: Icon }] }] }] }],
            },
        ]);
        expect(ids).toEqual(['a', 'b', 'c', 'd']);
        expect(new Set(navIds(NAV_SECTIONS)).size).toBe(navIds(NAV_SECTIONS).length);
    });
});

describe('App.tsx renders from the composition', () => {
    it('provides the composed slots, screens and navigation, and nothing built in directly', () => {
        const app = readFileSync(resolve(__dirname, '../../App.tsx'), 'utf8');
        expect(app).toMatch(/<SlotProvider slots=\{composition\.slots\}>/);
        expect(app).toMatch(/renderScreen\(composition\.screens, /);
        expect(app).toMatch(/sections=\{composition\.navSections\}/);
        expect(app).not.toMatch(/BUILTIN_SLOTS|NAV_SECTIONS|\bSCREENS\b/);
    });
});

describe('composeAdmin refusals', () => {
    const firstBuiltinNavId = navIds(NAV_SECTIONS)[0];
    const firstScreen = SCREENS[0];

    const cases: Array<[string, AdminExtension[], RegExp]> = [
        ['two extensions of one name', [REPORTS, { name: 'reports', interfaceVersion: V }], /two extensions are named reports/],
        ['an empty name', [{ name: '', interfaceVersion: V }], /name is empty or padded/],
        ['a padded name', [{ name: ' x', interfaceVersion: V }], /name is empty or padded/],
        ['a screen name a built-in screen has', [{ name: 'x', interfaceVersion: V, screens: [{ name: firstScreen.name, render: () => null }] }], /screen name already taken/],
        ['a tab id a built-in screen claims', [{ name: 'x', interfaceVersion: V, screens: [{ name: 'x-screen', ids: ['mission-control'], render: () => null }] }], /tab id already claimed/],
        ['a nav id a built-in item has', [{ name: 'x', interfaceVersion: V, navigation: [{ label: 'X', items: [{ id: firstBuiltinNavId, label: 'x', icon: Icon }] }] }], /navigation id already taken/],
        ['a nav id another extension has', [REPORTS, { name: 'y', interfaceVersion: V, navigation: [{ label: 'Y', items: [{ id: 'ext-reports', label: 'y', icon: Icon }] }] }], /navigation id already taken: ext-reports/],
        ['a slot item id the slot has', [REPORTS, { name: 'y', interfaceVersion: V, slots: { 'settings.tabs': [{ id: 'ext-reports', label: 'y', icon: Icon, render: () => null }] } }], /slot settings.tabs already has an item ext-reports/],
        ['an API module name a built-in module has, with another mixin', [{ name: 'x', interfaceVersion: V, apiModules: [{ name: 'auth', feature: null, mixin: ReportsApi }] }], /registered twice: auth/],
        ['an API module named twice across extensions', [REPORTS, { name: 'y', interfaceVersion: V, apiModules: [{ name: 'reports', feature: null, mixin: ReportsApi }] }], /API module named twice: reports/],
    ];

    it.each(cases)('refuses %s and installs nothing', (_what, extensions, message) => {
        const before = installedApiModules(GatewayApiClient);
        const methodsBefore = Object.getOwnPropertyNames(GatewayApiClient.prototype);
        expect(() => composeAdmin(extensions)).toThrow(message);
        expect(installedApiModules(GatewayApiClient)).toEqual(before);
        expect(Object.getOwnPropertyNames(GatewayApiClient.prototype)).toEqual(methodsBefore);
    });

    it('a refused second extension leaves the first’s API module uninstalled too', () => {
        // a client class of its own, so the process-wide one is untouched
        class Other extends GatewayApiBase {
            other(): number {
                return 1;
            }
        }
        const before = installedApiModules(GatewayApiClient);
        expect(() =>
            composeAdmin([
                { name: 'a', interfaceVersion: V, apiModules: [{ name: 'ext-a', feature: null, mixin: Other }] },
                { name: 'b', interfaceVersion: V, navigation: [{ label: 'B', items: [{ id: firstBuiltinNavId, label: 'b', icon: Icon }] }] },
            ])
        ).toThrow(/navigation id already taken/);
        expect(installedApiModules(GatewayApiClient)).toEqual(before);
        expect('other' in GatewayApiClient.prototype).toBe(false);
    });
});

// ---------------------------------------------------------------------------
// The interface version
// ---------------------------------------------------------------------------

describe('the extension interface version', () => {
    it('is 1', () => {
        // Pinned: raising it is a decision (every extension must follow), not a side effect.
        expect(ADMIN_EXTENSION_INTERFACE_VERSION).toBe(1);
    });

    it('an extension written against this version is composed', () => {
        expect(composeAdmin([{ name: 'current', interfaceVersion: ADMIN_EXTENSION_INTERFACE_VERSION }]).extensions).toEqual(['current']);
    });

    it.each<[string, unknown]>([
        ['an older version', ADMIN_EXTENSION_INTERFACE_VERSION - 1],
        ['a newer version', ADMIN_EXTENSION_INTERFACE_VERSION + 1],
        ['no version', undefined],
        ['the version as text', String(ADMIN_EXTENSION_INTERFACE_VERSION)],
    ])('an extension that declares %s is refused by name, with both versions, and nothing is installed', (_what, declared) => {
        const before = installedApiModules(GatewayApiClient);
        const ext = { ...REPORTS, name: 'stale', interfaceVersion: declared } as unknown as AdminExtension;
        expect(() => composeAdmin([ext])).toThrow(
            `extension stale: written against version ${String(declared)} of the extension interface; this console has version ${ADMIN_EXTENSION_INTERFACE_VERSION}`,
        );
        expect(installedApiModules(GatewayApiClient)).toEqual(before);
    });

    it('one stale extension refuses the whole list, whatever its place in it', () => {
        const stale = { name: 'stale', interfaceVersion: ADMIN_EXTENSION_INTERFACE_VERSION + 1 };
        const fine: AdminExtension = { name: 'fine', interfaceVersion: V };
        expect(() => composeAdmin([fine, stale])).toThrow(/extension stale: written against version/);
        expect(() => composeAdmin([stale, fine])).toThrow(/extension stale: written against version/);
    });
});

// ---------------------------------------------------------------------------
// Navigation entries placed inside the navigation that is there
// ---------------------------------------------------------------------------

/** A navigation tree as nested ids: `section > [id, [children…]]`. */
type IdTree = Array<string | [string, IdTree]>;
function idTree(nodes: readonly { id: string; children?: readonly { id: string }[] }[]): IdTree {
    return nodes.map((n) => (n.children ? [n.id, idTree(n.children)] : n.id));
}
const sectionIds = (sections: readonly NavSection[], label: string): IdTree =>
    idTree(sections.find((s) => s.label === label)?.items ?? []);
/** The children of the entry with `id`, as ids. */
function childIds(sections: readonly NavSection[], id: string): string[] | undefined {
    const walk = (nodes: readonly { id: string; children?: readonly { id: string }[] }[]): string[] | undefined => {
        for (const n of nodes) {
            if (n.id === id) return n.children?.map((c) => c.id);
            const below = n.children ? walk(n.children) : undefined;
            if (below) return below;
        }
        return undefined;
    };
    for (const s of sections) {
        const found = walk(s.items);
        if (found) return found;
    }
    return undefined;
}

const entry = (id: string, more: Record<string, unknown> = {}) => ({ id, label: `Label of ${id}`, icon: Icon, ...more });
const placing = (navPlacements: NavPlacement[], name = 'placer'): AdminExtension => ({ name, interfaceVersion: V, navPlacements });

describe('composeAdmin: placed navigation entries', () => {
    // Anchors in the built-in navigation that exist in every composition:
    // a section, a top-level item with children, a second-level entry with
    // children, and a leaf of it.
    const SECTION = 'Administration';
    const [firstItem, secondItem] = sectionIds(NAV_SECTIONS, SECTION).map((n) => (typeof n === 'string' ? n : n[0]));
    const GROUP = 'resources'; // level 1, has children
    const SUBGROUP = 'ai-models'; // level 2, has children
    const LEAF = 'llm-providers'; // level 3, a destination

    it('the anchors are what the tests take them for', () => {
        expect([firstItem, secondItem]).toEqual(['users', 'config']);
        expect(childIds(NAV_SECTIONS, GROUP)).toContain(SUBGROUP);
        expect(childIds(NAV_SECTIONS, SUBGROUP)?.[0]).toBe(LEAF);
        expect(childIds(NAV_SECTIONS, LEAF)).toBeUndefined();
    });

    it('with no placement the sections are the built-in objects themselves', () => {
        const { navSections } = composeAdmin([REPORTS]);
        NAV_SECTIONS.forEach((section, i) => expect(navSections[i]).toBe(section));
    });

    it('places a top-level item first in a section, or directly after the sibling it names', () => {
        const before = sectionIds(NAV_SECTIONS, SECTION);
        const first = composeAdmin([placing([{ section: SECTION, after: null, entry: entry('p-first') }])]);
        expect(sectionIds(first.navSections, SECTION)).toEqual(['p-first', ...before]);
        const after = composeAdmin([placing([{ section: SECTION, after: firstItem, entry: entry('p-after') }])]);
        expect(sectionIds(after.navSections, SECTION)).toEqual([before[0], 'p-after', ...before.slice(1)]);
        const last = composeAdmin([placing([{ section: SECTION, after: 'system', entry: entry('p-last') }])]);
        expect(sectionIds(last.navSections, SECTION)).toEqual([...before, 'p-last']);
    });

    it('places an entry under a parent at the second, third and fourth level', () => {
        const { navSections } = composeAdmin([
            placing([
                { parent: GROUP, after: SUBGROUP, entry: entry('p-2', { requiresGate: 'g', requiresPermission: 'p:read' }) },
                { parent: SUBGROUP, after: LEAF, entry: entry('p-3', { requiresPermission: 'p:read', children: [entry('p-3-a')] }) },
                { parent: 'p-3', after: 'p-3-a', entry: entry('p-4') },
            ]),
        ]);
        const resources = childIds(NAV_SECTIONS, GROUP) ?? [];
        expect(childIds(navSections, GROUP)).toEqual([resources[0], 'p-2', ...resources.slice(1)]);
        const models = childIds(NAV_SECTIONS, SUBGROUP) ?? [];
        expect(childIds(navSections, SUBGROUP)).toEqual([models[0], 'p-3', ...models.slice(1)]);
        expect(childIds(navSections, 'p-3')).toEqual(['p-3-a', 'p-4']);
    });

    it('a field that is present but undefined is not a field set', () => {
        // An entry built with `{ requiresGate: cond ? 'g' : undefined }` sets nothing where the condition is false.
        const { navSections } = composeAdmin([
            placing([{ parent: SUBGROUP, after: null, entry: entry('p-3', { requiresGate: undefined, children: undefined }) }]),
        ]);
        expect(childIds(navSections, SUBGROUP)?.[0]).toBe('p-3');
    });

    it('entries placed after the same sibling keep the order they were placed in, across extensions', () => {
        const { navSections } = composeAdmin([
            placing([
                { section: SECTION, after: firstItem, entry: entry('p-a') },
                { section: SECTION, after: null, entry: entry('p-first-a') },
                { section: SECTION, after: firstItem, entry: entry('p-b') },
            ]),
            placing(
                [
                    { section: SECTION, after: firstItem, entry: entry('p-c') },
                    { section: SECTION, after: null, entry: entry('p-first-b') },
                ],
                'second',
            ),
        ]);
        expect(sectionIds(navSections, SECTION).slice(0, 7).map((n) => (typeof n === 'string' ? n : n[0]))).toEqual([
            'p-first-a', 'p-first-b', firstItem, 'p-a', 'p-b', 'p-c', secondItem,
        ]);
    });

    it('an entry can be placed after, or under, one placed before it, and in a section an extension adds', () => {
        const { navSections } = composeAdmin([
            {
                name: 'both',
                interfaceVersion: V,
                navigation: [{ label: 'Ext Section', items: [entry('e-1', { children: [entry('e-1-a')] })] }],
                navPlacements: [
                    { section: 'Ext Section', after: 'e-1', entry: entry('e-2') },
                    { section: 'Ext Section', after: 'e-2', entry: entry('e-3') },
                    { parent: 'e-1', after: null, entry: entry('e-1-first') },
                ],
            },
        ]);
        expect(sectionIds(navSections, 'Ext Section')).toEqual([['e-1', ['e-1-first', 'e-1-a']], 'e-2', 'e-3']);
    });

    it('changes neither the built-in navigation nor the extension’s own objects', () => {
        const builtinBefore = JSON.stringify(NAV_SECTIONS.map((s) => [s.label, idTree(s.items)]));
        const section: NavSection = { label: 'Ext Section', items: [entry('e-1', { children: [entry('e-1-a')] })] };
        const group = entry('p-group', { children: [entry('p-group-a')] });
        const ext: AdminExtension = {
            name: 'careful',
            interfaceVersion: V,
            navigation: [section],
            navPlacements: [
                { parent: GROUP, after: null, entry: group },
                { parent: 'p-group', after: null, entry: entry('p-group-first') },
                { parent: 'e-1', after: null, entry: entry('e-1-first') },
            ],
        };
        const one = composeAdmin([ext]);
        const two = composeAdmin([ext]);
        expect(JSON.stringify(NAV_SECTIONS.map((s) => [s.label, idTree(s.items)]))).toBe(builtinBefore);
        expect(idTree([group])).toEqual([['p-group', ['p-group-a']]]);
        expect(idTree(section.items)).toEqual([['e-1', ['e-1-a']]]);
        // composing twice gives the same navigation, not a doubled one
        expect(childIds(one.navSections, 'p-group')).toEqual(['p-group-first', 'p-group-a']);
        expect(JSON.stringify(two.navSections.map((s) => [s.label, idTree(s.items)]))).toBe(
            JSON.stringify(one.navSections.map((s) => [s.label, idTree(s.items)])),
        );
        // every built-in entry is still there, with its own label, icon and gates
        const builtin = NAV_SECTIONS.find((s) => s.label === SECTION)?.items ?? [];
        const composed = one.navSections.find((s) => s.label === SECTION)?.items ?? [];
        expect(composed).toEqual(builtin);
    });

    it('the sidebar shows a placed entry where it was placed', () => {
        const { navSections } = composeAdmin([
            placing([
                { section: SECTION, after: firstItem, entry: entry('p-shown') },
                { section: SECTION, after: null, entry: entry('p-hidden', { requiresPermission: 'nobody:has' }) },
            ]),
        ]);
        render(
            <AppSidebar
                sections={navSections}
                activeTab="mission-control"
                onTabChange={vi.fn()}
                username="admin"
                tenantId="t1"
                isTenantAdmin
                onLogout={vi.fn()}
                hasErrors={false}
                errorCount={0}
                hasCriticalErrors={false}
                groupTree={[]}
                groupTreeLoading={false}
                onRefreshGroupTree={vi.fn()}
                hasPermission={(p) => p !== 'nobody:has'}
            />,
        );
        const labels = dom.getAllByRole('button').map((b) => b.textContent ?? '');
        const at = (label: string) => labels.findIndex((l) => l.includes(label));
        expect(at('Label of p-shown')).toBeGreaterThan(at('Users'));
        expect(at('Label of p-shown')).toBeLessThan(at('Settings'));
        // its gate is read like a built-in entry's
        expect(dom.queryByText('Label of p-hidden')).toBeNull();
    });
});

describe('composeAdmin: placed navigation entries it refuses', () => {
    const firstBuiltinNavId = navIds(NAV_SECTIONS)[0];
    const cases: Array<[string, NavPlacement[], string]> = [
        ['an unknown parent', [{ parent: 'no-such-parent', after: null, entry: entry('p') }], 'extension placer: navigation entry p names an unknown parent: no-such-parent'],
        ['an unknown sibling under a parent', [{ parent: 'resources', after: 'no-such-sibling', entry: entry('p') }], 'extension placer: navigation entry p names an unknown sibling in parent resources: no-such-sibling'],
        ['a sibling that is elsewhere in the tree', [{ parent: 'resources', after: 'users', entry: entry('p') }], 'extension placer: navigation entry p names an unknown sibling in parent resources: users'],
        ['a sibling that is a grandchild of the parent', [{ parent: 'resources', after: 'llm-providers', entry: entry('p') }], 'extension placer: navigation entry p names an unknown sibling in parent resources: llm-providers'],
        ['an unknown section', [{ section: 'No Such Section', after: null, entry: entry('p') }], 'extension placer: navigation entry p names an unknown section: No Such Section'],
        ['an unknown sibling in a section', [{ section: 'Administration', after: 'no-such-sibling', entry: entry('p') }], 'extension placer: navigation entry p names an unknown sibling in section Administration: no-such-sibling'],
        ['a parent whose children come from the live group tree', [{ parent: 'groups', after: null, entry: entry('p') }], 'extension placer: navigation entry p names a parent whose children are not listed: groups'],
        ['a parent that is a destination', [{ parent: 'users', after: null, entry: entry('p') }], 'extension placer: navigation entry p names a parent that is a destination, not a group: users'],
        ['an id a built-in entry has', [{ section: 'Administration', after: null, entry: entry(firstBuiltinNavId) }], `extension placer: navigation id already taken: ${firstBuiltinNavId}`],
        ['a child id a built-in entry has', [{ section: 'Administration', after: null, entry: entry('p', { children: [entry('users')] }) }], 'extension placer: navigation id already taken: users'],
        ['an id placed before in the same extension', [{ section: 'Administration', after: null, entry: entry('p') }, { section: 'Operations', after: null, entry: entry('p') }], 'extension placer: navigation id already taken: p'],
        ['a deployment gate at the third level', [{ parent: 'ai-models', after: null, entry: entry('p', { requiresGate: 'g' }) }], 'extension placer: navigation entry p sets requiresGate, which the sidebar does not read at level 3'],
        ['a tenant-admin gate at the second level', [{ parent: 'resources', after: null, entry: entry('p', { requiresTenantAdmin: true }) as never }], 'extension placer: navigation entry p sets requiresTenantAdmin, which the sidebar does not read at level 2'],
        ['a principal gate on a child of the placed entry', [{ section: 'Administration', after: null, entry: entry('p', { children: [entry('q', { requiresPrincipal: 'system' })] }) as never }], 'extension placer: navigation entry q sets requiresPrincipal, which the sidebar does not read at level 2'],
        ['a deployment gate at the fourth level', [{ parent: 'ai-models', after: null, entry: entry('p', { children: [entry('q', { requiresGate: 'g' })] }) }], 'extension placer: navigation entry q sets requiresGate, which the sidebar does not read at level 4'],
        ['children at the fourth level', [{ parent: 'ai-models', after: null, entry: entry('p', { children: [entry('q', { children: [entry('r')] })] }) }], 'extension placer: navigation entry q sets children, which the sidebar does not read at level 4'],
        ['an entry under a fourth-level parent', [{ parent: 'ai-models', after: null, entry: entry('p', { children: [entry('q')] }) }, { parent: 'q', after: null, entry: entry('r') }], 'extension placer: navigation entry r names a parent that is a destination, not a group: q'],
    ];

    it.each(cases)('refuses %s, by name, and installs nothing', (_what, navPlacements, message) => {
        const before = installedApiModules(GatewayApiClient);
        const builtinBefore = JSON.stringify(navIds(NAV_SECTIONS));
        class PlacerApi extends GatewayApiBase {
            placerMethod(): number {
                return 1;
            }
        }
        const ext: AdminExtension = {
            ...placing(navPlacements),
            apiModules: [{ name: 'placer', feature: null, mixin: PlacerApi }],
        };
        expect(() => composeAdmin([ext])).toThrow(new Error(message));
        expect(installedApiModules(GatewayApiClient)).toEqual(before);
        expect('placerMethod' in GatewayApiClient.prototype).toBe(false);
        expect(JSON.stringify(navIds(NAV_SECTIONS))).toBe(builtinBefore);
    });

    it('accepts a permission gate at the fourth level: the sidebar reads it there', () => {
        const composed = composeAdmin([
            placing([{ parent: 'ai-models', after: null, entry: entry('p', { children: [entry('q', { requiresPermission: 'x:read' })] }) }]),
        ]);
        expect(JSON.stringify(composed.navSections)).toContain('"requiresPermission":"x:read"');
    });

    it('a section label two sections share cannot be placed into', () => {
        const twice: AdminExtension = {
            name: 'twice',
            interfaceVersion: V,
            navigation: [{ label: 'Administration', items: [entry('t-1')] }],
            navPlacements: [{ section: 'Administration', after: null, entry: entry('p') }],
        };
        expect(() => composeAdmin([twice])).toThrow(
            'extension twice: navigation entry p names a section label that 2 sections have: Administration',
        );
    });
});
