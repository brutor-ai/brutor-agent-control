/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Extensions of the Admin Console.
 *
 * An extension is a second distribution's contribution to the console:
 * screens, navigation sections and entries, slot items and
 * API client modules. `<App extensions={[...]} />` composes them with
 * the built-in registries through `composeAdmin`; with none, the console is
 * the built-in registries and nothing else.
 *
 * The interface is versioned: an extension names the version
 * it was written against, and one written against another is refused.
 *
 * Screens, navigation and slot items are composed per `App`: the
 * composition is a value the app renders from. API modules are installed on
 * the `GatewayApiClient` class, which every client instance shares, so they
 * are process-wide: the same module installed again is a no-op (the app
 * mounted twice), a different module under an installed name is refused.
 *
 * Every refusal is all-or-none and happens before anything is installed: an
 * extension half composed would be a console nobody described.
 */

import { GatewayApiClient } from '../lib/gatewayApiClient';
import { installApiModules, type ApiModuleSpec } from '../lib/gatewayApi/modules';
import { NAV_SECTIONS, type NavChild, type NavItem, type NavLeaf, type NavSection } from './navigation';
import { SCREENS, type ScreenRoute } from './screens';
import { BUILTIN_SLOTS } from './builtinSlots';
import type { SlotName, SlotRegistry } from './slots';

/**
 * The version of the extension interface: `AdminExtension`, what
 * `composeAdmin` does with each of its fields, and the types those fields are
 * made of (`ScreenRoute`, the navigation types, `SlotRegistry`,
 * `ApiModuleSpec`). It is its own number, not the product's.
 *
 * Raise it with any change an extension written against the current one
 * could notice: a field removed or given another meaning, a refusal added, a
 * slot's props changed. `composeAdmin` refuses an extension that names
 * another version, so a console and an extension that disagree fail at
 * start-up, by name, and not somewhere inside a page.
 */
export const ADMIN_EXTENSION_INTERFACE_VERSION = 1;

/**
 * A navigation entry placed inside the navigation that is already there
 * (the built-in one, or what an earlier extension or an earlier placement of
 * this one added): in a section, or under an entry that has children.
 *
 * `after` is the id of the sibling the entry follows, or null to come first.
 * Entries placed after the same sibling keep the order they were placed in.
 *
 * What the entry may carry depends on how deep it lands, because the sidebar
 * reads fewer gates the deeper an entry is (`navigation.tsx`): a permission
 * gate down to the third level, a deployment gate down to the second,
 * children down to the third. A field the sidebar would not read at the
 * entry's depth is refused, never ignored: an entry meant to be hidden would
 * be shown.
 */
export type NavPlacement =
    | {
          /** The label of the section the entry becomes a top-level item of. */
          readonly section: string;
          readonly after: string | null;
          readonly entry: NavItem;
      }
    | {
          /** The id of the entry it goes under. That entry has children already. */
          readonly parent: string;
          readonly after: string | null;
          readonly entry: NavChild;
      };

/** What one extension contributes. Every field but the first two may be absent. */
export interface AdminExtension {
    /** Unique, stable name. */
    readonly name: string;
    /** The interface version the extension was written against: `ADMIN_EXTENSION_INTERFACE_VERSION` at that time. */
    readonly interfaceVersion: number;
    /** Screens, resolved after the built-in ones. */
    readonly screens?: readonly ScreenRoute[];
    /** Navigation sections, shown after the built-in ones. */
    readonly navigation?: readonly NavSection[];
    /** Navigation entries placed inside the sections that are there, applied in order after `navigation`. */
    readonly navPlacements?: readonly NavPlacement[];
    /** Slot items, appended to each slot's built-in items. */
    readonly slots?: SlotRegistry;
    /** API client modules, installed on `GatewayApiClient`. */
    readonly apiModules?: readonly ApiModuleSpec[];
}

/** What one `App` is built from. */
export interface AdminComposition {
    readonly screens: readonly ScreenRoute[];
    readonly navSections: readonly NavSection[];
    readonly slots: SlotRegistry;
    /** The extensions composed, in order. */
    readonly extensions: readonly string[];
}

/** Every id in a navigation tree: items, children, leaves, sub-leaves. */
export function navIds(sections: readonly NavSection[]): string[] {
    const ids: string[] = [];
    const leaf = (l: NavLeaf): void => {
        ids.push(l.id);
        l.children?.forEach(leaf);
    };
    const child = (c: NavChild): void => {
        ids.push(c.id);
        c.children?.forEach(leaf);
    };
    const item = (i: NavItem): void => {
        ids.push(i.id);
        i.children?.forEach(child);
    };
    for (const section of sections) section.items.forEach(item);
    return ids;
}

function refuse(extension: string, what: string): never {
    throw new Error(`extension ${extension}: ${what}`);
}

/** A navigation entry at any level, as the placement code walks it. */
type NavNode = { id: string; children?: NavNode[] } & Record<string, unknown>;

/** The fields the sidebar reads on an entry, by the level it sits at (1: a section's item). */
const NAV_FIELDS_BY_DEPTH: readonly (readonly string[])[] = [
    ['id', 'label', 'icon', 'children', 'requiresTenantAdmin', 'requiresPrincipal', 'requiresPermission', 'requiresGate', 'dynamicChildren'],
    ['id', 'label', 'icon', 'children', 'requiresPermission', 'requiresGate'],
    ['id', 'label', 'icon', 'children', 'requiresPermission'],
    ['id', 'label', 'icon', 'requiresPermission'],
];

/** A copy of an entry and everything under it. */
function copyNavNode(node: NavNode): NavNode {
    return node.children ? { ...node, children: node.children.map(copyNavNode) } : { ...node };
}

/** A copy of the navigation whose arrays and entries can be changed without touching the original. */
function copyNavigation(sections: readonly NavSection[]): NavSection[] {
    return sections.map((section) => ({
        ...section,
        items: section.items.map((item) => copyNavNode(item as unknown as NavNode) as unknown as NavItem),
    }));
}

/** The entry with `id` and the level it sits at, or undefined. */
function findNavNode(sections: readonly NavSection[], id: string): { node: NavNode; depth: number } | undefined {
    const walk = (nodes: readonly NavNode[], depth: number): { node: NavNode; depth: number } | undefined => {
        for (const node of nodes) {
            if (node.id === id) return { node, depth };
            const below = node.children ? walk(node.children, depth + 1) : undefined;
            if (below) return below;
        }
        return undefined;
    };
    for (const section of sections) {
        const found = walk(section.items as unknown as NavNode[], 1);
        if (found) return found;
    }
    return undefined;
}

/** Refuse what the sidebar would not show as written: an entry too deep, or a field it does not read there. */
function checkNavEntry(extension: string, node: NavNode, depth: number): void {
    const fields = NAV_FIELDS_BY_DEPTH[depth - 1];
    if (!fields) refuse(extension, `navigation entry ${node.id} would be at level ${depth}; the sidebar has ${NAV_FIELDS_BY_DEPTH.length}`);
    for (const key of Object.keys(node)) {
        if (node[key] !== undefined && !fields.includes(key)) {
            refuse(extension, `navigation entry ${node.id} sets ${key}, which the sidebar does not read at level ${depth}`);
        }
    }
    node.children?.forEach((child) => checkNavEntry(extension, child, depth + 1));
}

/**
 * Put a copy of one placed entry into `sections` (itself a copy: it is
 * changed in place; the extension's own objects never are).
 * `placedAfter` remembers, per list and sibling, what was placed there
 * before, so entries placed after one sibling keep their order.
 */
function placeNavEntry(
    extension: string,
    sections: NavSection[],
    placement: NavPlacement,
    placedAfter: Map<readonly unknown[], Map<string | null, number>>,
): void {
    const entry = copyNavNode(placement.entry as unknown as NavNode);
    let siblings: NavNode[];
    let depth: number;
    let where: string;
    if ('section' in placement) {
        const matching = sections.filter((section) => section.label === placement.section);
        if (matching.length === 0) refuse(extension, `navigation entry ${entry.id} names an unknown section: ${placement.section}`);
        if (matching.length > 1) refuse(extension, `navigation entry ${entry.id} names a section label that ${matching.length} sections have: ${placement.section}`);
        siblings = matching[0].items as unknown as NavNode[];
        depth = 1;
        where = `section ${placement.section}`;
    } else {
        const parent = findNavNode(sections, placement.parent);
        if (!parent) refuse(extension, `navigation entry ${entry.id} names an unknown parent: ${placement.parent}`);
        if (parent.node.dynamicChildren) refuse(extension, `navigation entry ${entry.id} names a parent whose children are not listed: ${placement.parent}`);
        if (!parent.node.children) refuse(extension, `navigation entry ${entry.id} names a parent that is a destination, not a group: ${placement.parent}`);
        siblings = parent.node.children;
        depth = parent.depth + 1;
        where = `parent ${placement.parent}`;
    }
    checkNavEntry(extension, entry, depth);

    let index = 0;
    if (placement.after !== null) {
        const sibling = siblings.findIndex((node) => node.id === placement.after);
        if (sibling < 0) refuse(extension, `navigation entry ${entry.id} names an unknown sibling in ${where}: ${placement.after}`);
        index = sibling + 1;
    }
    const counts = placedAfter.get(siblings) ?? new Map<string | null, number>();
    const before = counts.get(placement.after) ?? 0;
    siblings.splice(index + before, 0, entry);
    counts.set(placement.after, before + 1);
    placedAfter.set(siblings, counts);
}

/**
 * Compose the built-in registries with `extensions`.
 *
 * Everything is checked first; the first problem throws and nothing is
 * installed. With no extensions the result is the built-in registries.
 */
export function composeAdmin(extensions: readonly AdminExtension[] = []): AdminComposition {
    const names: string[] = [];
    for (const ext of extensions) {
        if (!ext.name || ext.name !== ext.name.trim()) throw new Error(`extension name is empty or padded: ${JSON.stringify(ext.name)}`);
        if (names.includes(ext.name)) throw new Error(`two extensions are named ${ext.name}`);
        names.push(ext.name);
    }
    for (const ext of extensions) {
        if (ext.interfaceVersion !== ADMIN_EXTENSION_INTERFACE_VERSION) {
            refuse(
                ext.name,
                `written against version ${String(ext.interfaceVersion)} of the extension interface; this console has version ${ADMIN_EXTENSION_INTERFACE_VERSION}`,
            );
        }
    }

    const screens: ScreenRoute[] = [...SCREENS];
    const screenNames = new Set(screens.map((s) => s.name));
    const claimedIds = new Set(screens.flatMap((s) => [...(s.ids ?? [])]));
    const claimedPrefixes = new Set(screens.flatMap((s) => [...(s.prefixes ?? [])]));
    // Copied only when an entry is placed: without one the sections are the
    // built-in objects themselves.
    const placing = extensions.some((ext) => (ext.navPlacements ?? []).length > 0);
    const navSections: NavSection[] = placing ? copyNavigation(NAV_SECTIONS) : [...NAV_SECTIONS];
    const placedAfter = new Map<readonly unknown[], Map<string | null, number>>();
    const takenNavIds = new Set(navIds(NAV_SECTIONS));
    const slots: { [K in SlotName]?: SlotRegistry[K] } = { ...BUILTIN_SLOTS };
    const apiModules: ApiModuleSpec[] = [];
    const apiModuleNames = new Set<string>();

    for (const ext of extensions) {
        for (const screen of ext.screens ?? []) {
            if (!screen.name) refuse(ext.name, 'a screen has no name');
            if (screenNames.has(screen.name)) refuse(ext.name, `screen name already taken: ${screen.name}`);
            for (const id of screen.ids ?? []) {
                if (claimedIds.has(id)) refuse(ext.name, `tab id already claimed by a screen: ${id}`);
            }
            for (const prefix of screen.prefixes ?? []) {
                if (claimedPrefixes.has(prefix)) refuse(ext.name, `deep-link prefix already claimed by a screen: ${prefix}`);
            }
            screenNames.add(screen.name);
            screen.ids?.forEach((id) => claimedIds.add(id));
            screen.prefixes?.forEach((p) => claimedPrefixes.add(p));
            screens.push(screen);
        }
        for (const section of ext.navigation ?? []) {
            for (const id of navIds([section])) {
                if (takenNavIds.has(id)) refuse(ext.name, `navigation id already taken: ${id}`);
                takenNavIds.add(id);
            }
            navSections.push(placing ? copyNavigation([section])[0] : section);
        }
        for (const placement of ext.navPlacements ?? []) {
            for (const id of navIds([{ label: null, items: [placement.entry] }])) {
                if (takenNavIds.has(id)) refuse(ext.name, `navigation id already taken: ${id}`);
                takenNavIds.add(id);
            }
            placeNavEntry(ext.name, navSections, placement, placedAfter);
        }
        for (const [slotName, items] of Object.entries(ext.slots ?? {}) as [SlotName, readonly { id: string }[]][]) {
            const present = (slots[slotName] ?? []) as readonly { id: string }[];
            const ids = new Set(present.map((i) => i.id));
            for (const item of items) {
                if (ids.has(item.id)) refuse(ext.name, `slot ${slotName} already has an item ${item.id}`);
                ids.add(item.id);
            }
            // The registry maps each slot name to items of that slot's type;
            // the extension's items are typed by the same map.
            (slots as Record<string, readonly unknown[]>)[slotName] = [...present, ...items];
        }
        for (const mod of ext.apiModules ?? []) {
            if (apiModuleNames.has(mod.name)) refuse(ext.name, `API module named twice: ${mod.name}`);
            apiModuleNames.add(mod.name);
            apiModules.push(mod);
        }
    }

    // Last, and all-or-none itself: the installer refuses a different module
    // under an installed name and skips an identical one.
    installApiModules(GatewayApiClient, apiModules);

    return { screens, navSections, slots, extensions: names };
}
