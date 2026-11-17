/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A composition of the console written down as plain data, so that it can be
 * recorded in a file and compared: the navigation as an ordered tree with
 * every field the sidebar reads, the screens with what each claims, which
 * screen answers which tab id, the slot items in order, and the API client's
 * modules and methods.
 */

import type React from 'react';

import type { AdminComposition } from '../app/extensions';
import { resolveScreen, type ScreenRoute } from '../app/screens';
import type { GatewayApiBase } from '../lib/gatewayApi/base';
import { installedApiModules } from '../lib/gatewayApi/modules';

type Entry = {
    id: string;
    label: string;
    icon: React.ComponentType<never>;
    children?: readonly Entry[];
    requiresTenantAdmin?: boolean;
    requiresPrincipal?: string;
    requiresPermission?: string;
    requiresGate?: string;
    dynamicChildren?: boolean;
};

export interface RecordedEntry {
    id: string;
    label: string;
    icon: string;
    requiresTenantAdmin?: boolean;
    requiresPrincipal?: string;
    requiresPermission?: string;
    requiresGate?: string;
    dynamicChildren?: boolean;
    children?: RecordedEntry[];
}

/** The name of an icon component, through `forwardRef` and `memo`. */
function iconName(icon: unknown): string {
    const i = icon as { displayName?: string; name?: string; render?: { displayName?: string; name?: string } };
    return i.displayName || i.name || i.render?.displayName || i.render?.name || '(unnamed)';
}

function describeEntry(entry: Entry): RecordedEntry {
    const out: RecordedEntry = { id: entry.id, label: entry.label, icon: iconName(entry.icon) };
    if (entry.requiresTenantAdmin !== undefined) out.requiresTenantAdmin = entry.requiresTenantAdmin;
    if (entry.requiresPrincipal !== undefined) out.requiresPrincipal = entry.requiresPrincipal;
    if (entry.requiresPermission !== undefined) out.requiresPermission = entry.requiresPermission;
    if (entry.requiresGate !== undefined) out.requiresGate = entry.requiresGate;
    if (entry.dynamicChildren !== undefined) out.dynamicChildren = entry.dynamicChildren;
    if (entry.children !== undefined) out.children = entry.children.map(describeEntry);
    return out;
}

/** The navigation in order: sections, and under each its entries with their ids, labels, icons and gates. */
export function describeNavigation(composition: AdminComposition): Array<{ section: string | null; items: RecordedEntry[] }> {
    return composition.navSections.map((section) => ({
        section: section.label,
        items: (section.items as unknown as Entry[]).map(describeEntry),
    }));
}

const byName = (a: { name: string }, b: { name: string }): number => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);

/**
 * The screens by name, each with what it claims. Sorted: the order of the
 * list is not something a tab id can observe (no two screens claim one id or
 * one deep-link prefix); what a tab id observes is `describeResolution`.
 */
export function describeScreens(screens: readonly ScreenRoute[]): Array<{ name: string; ids: string[]; prefixes: string[]; edition: string | null }> {
    return screens
        .map((screen) => ({
            name: screen.name,
            ids: [...(screen.ids ?? [])],
            prefixes: [...(screen.prefixes ?? [])],
            edition: screen.edition ?? null,
        }))
        .sort(byName);
}

/** The name of the screen that answers each of `tabs`, or null where the fallback would. */
export function describeResolution(screens: readonly ScreenRoute[], tabs: readonly string[]): Record<string, string | null> {
    return Object.fromEntries([...tabs].sort().map((tab) => [tab, resolveScreen(screens, tab)?.name ?? null]));
}

/** Per slot (by name), its items in order as `id: feature`. A slot nothing is contributed to is absent. */
export function describeSlots(composition: AdminComposition): Record<string, string[]> {
    const entries = Object.entries(composition.slots as Record<string, readonly { id: string; edition?: string }[]>)
        .filter(([, items]) => items.length > 0)
        .map(([name, items]): [string, string[]] => [name, items.map((item) => `${item.id}: ${item.edition ?? 'community'}`)]);
    return Object.fromEntries(entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));
}

/** The modules installed on a client class, in order, and the methods it answers, sorted. */
export function describeApiClient(target: abstract new (...args: never[]) => GatewayApiBase): { modules: string[]; methods: string[] } {
    return {
        modules: [...installedApiModules(target)],
        methods: Object.getOwnPropertyNames(target.prototype as object)
            .filter((name) => name !== 'constructor')
            .sort(),
    };
}

/** Every tab id a composition names: navigation ids, the ids screens claim, and each deep-link prefix with a target. */
export function tabIdsOf(composition: AdminComposition): string[] {
    const ids = new Set<string>();
    const walk = (entries: readonly RecordedEntry[]): void => {
        for (const entry of entries) {
            ids.add(entry.id);
            if (entry.children) walk(entry.children);
        }
    };
    for (const section of describeNavigation(composition)) walk(section.items);
    for (const screen of composition.screens) {
        screen.ids?.forEach((id) => ids.add(id));
        screen.prefixes?.forEach((prefix) => ids.add(`${prefix}target`));
    }
    return [...ids].sort();
}

export const asRecord = (value: unknown): string => `${JSON.stringify(value, null, 2)}\n`;
