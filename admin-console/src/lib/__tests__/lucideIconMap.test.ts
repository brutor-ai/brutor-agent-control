/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Group iconography. The ordering that matters: an AI System's KIND is more
 * specific than its group type, so kind wins — an agent must read as an
 * agent in the tree rather than as a generic stack of layers.
 */

import { describe, it, expect, afterEach } from 'vitest';
import {
    COLOR_PALETTE,
    DEFAULT_COLOR_KEY,
    GROUP_TYPE_DEFAULT_ICONS,
    ICON_CATEGORIES,
    ICON_REGISTRY,
    SYSTEM_KIND_DEFAULT_ICONS,
    getAllColorKeys,
    getAllIconNames,
    getIconEntry,
    getIconsByCategory,
    resolveGroupColor,
    resolveGroupIcon,
} from '../lucideIconMap';

afterEach(() => {
    document.documentElement.removeAttribute('data-theme');
});

describe('resolveGroupIcon', () => {
    it('prefers an explicitly chosen icon over every default', () => {
        expect(resolveGroupIcon('rocket', 'team', 'agent')).toBe(ICON_REGISTRY.rocket.component);
    });

    it('prefers the AI System kind over the group type', () => {
        expect(resolveGroupIcon(undefined, 'ai_system', 'agent')).toBe(ICON_REGISTRY.bot.component);
        expect(resolveGroupIcon(undefined, 'ai_system', undefined)).toBe(
            ICON_REGISTRY.layers.component,
        );
    });

    it('uses the group-type default when no kind is set', () => {
        expect(resolveGroupIcon(undefined, 'team')).toBe(ICON_REGISTRY.users.component);
        expect(resolveGroupIcon(undefined, 'organization')).toBe(ICON_REGISTRY.building2.component);
    });

    it('falls back to Folder when nothing resolves', () => {
        const folder = ICON_REGISTRY.folder.component;
        expect(resolveGroupIcon()).toBe(folder);
        expect(resolveGroupIcon('no-such-icon', 'no-such-type', 'no-such-kind')).toBe(folder);
        expect(resolveGroupIcon(undefined, undefined, null)).toBe(folder);
    });

    it('skips an unknown icon name and falls through to the defaults', () => {
        expect(resolveGroupIcon('no-such-icon', 'team')).toBe(ICON_REGISTRY.users.component);
    });

    it('resolves every declared group-type and system-kind default', () => {
        // A default pointing at a key the registry does not hold would fall
        // through to Folder — every group of that type silently generic.
        const danglingTypes = Object.entries(GROUP_TYPE_DEFAULT_ICONS)
            .filter(([type, key]) => resolveGroupIcon(undefined, type) !== ICON_REGISTRY[key]?.component)
            .map(([type]) => type);
        expect(danglingTypes).toEqual([]);

        const danglingKinds = Object.entries(SYSTEM_KIND_DEFAULT_ICONS)
            .filter(
                ([kind, key]) =>
                    resolveGroupIcon(undefined, 'ai_system', kind) !== ICON_REGISTRY[key]?.component,
            )
            .map(([kind]) => kind);
        expect(danglingKinds).toEqual([]);
    });
});

describe('icon registry queries', () => {
    it('returns the entry for a known name and undefined for anything else', () => {
        expect(getIconEntry('bot')?.label).toBe('Bot');
        expect(getIconEntry('Bot')).toBeUndefined();
        expect(getIconEntry('')).toBeUndefined();
    });

    it('lists every registry key', () => {
        expect(getAllIconNames()).toEqual(Object.keys(ICON_REGISTRY));
        expect(getAllIconNames()).toContain('sparkles');
    });

    it('filters by category and is empty for an unknown one', () => {
        const tech = getIconsByCategory('Tech');
        expect(tech).toContain('bot');
        expect(tech).not.toContain('shield');
        expect(getIconsByCategory('Nonexistent')).toEqual([]);
    });

    it('accounts for every icon under exactly one declared category', () => {
        const counted = ICON_CATEGORIES.flatMap((c) => getIconsByCategory(c));
        expect(counted.sort()).toEqual(getAllIconNames().sort());
    });
});

describe('resolveGroupColor', () => {
    it('returns the named palette entry', () => {
        expect(resolveGroupColor('emerald').fg).toBe(COLOR_PALETTE.emerald.fg);
    });

    it('falls back to the default key for an unknown or absent colour', () => {
        const fallback = COLOR_PALETTE[DEFAULT_COLOR_KEY];
        expect(resolveGroupColor().fg).toBe(fallback.fg);
        expect(resolveGroupColor('chartreuse').fg).toBe(fallback.fg);
        expect(resolveGroupColor('').fg).toBe(fallback.fg);
    });

    it('swaps in the dark background when the theme says dark', () => {
        const entry = resolveGroupColor('emerald', 'dark');
        expect(entry.bg).toBe(COLOR_PALETTE.emerald.bgDark);
        expect(entry.fg).toBe(COLOR_PALETTE.emerald.fg);
    });

    it('reads the document theme when none is passed', () => {
        document.documentElement.setAttribute('data-theme', 'dark');
        expect(resolveGroupColor('emerald').bg).toBe(COLOR_PALETTE.emerald.bgDark);
        document.documentElement.setAttribute('data-theme', 'light');
        expect(resolveGroupColor('emerald').bg).toBe(COLOR_PALETTE.emerald.bg);
    });

    it('lets an explicit light theme override a dark document', () => {
        document.documentElement.setAttribute('data-theme', 'dark');
        expect(resolveGroupColor('emerald', 'light').bg).toBe(COLOR_PALETTE.emerald.bg);
    });

    it('does not mutate the shared palette entry when swapping backgrounds', () => {
        resolveGroupColor('emerald', 'dark');
        expect(COLOR_PALETTE.emerald.bg).not.toBe(COLOR_PALETTE.emerald.bgDark);
    });
});

describe('COLOR_PALETTE', () => {
    it('lists every palette key', () => {
        expect(getAllColorKeys()).toEqual(Object.keys(COLOR_PALETTE));
    });

    it('gives every entry a distinct light and dark background and a hex foreground', () => {
        const broken = Object.entries(COLOR_PALETTE)
            .filter(([, e]) => e.bg === e.bgDark || !/^#[0-9a-f]{6}$/.test(e.fg))
            .map(([key]) => key);
        expect(broken).toEqual([]);
    });

    it('has a default key that exists in the palette', () => {
        expect(COLOR_PALETTE[DEFAULT_COLOR_KEY]).toBeDefined();
    });
});
