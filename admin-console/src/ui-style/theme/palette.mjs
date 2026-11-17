/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Property-scoped, theme-aware palette.
 *
 * ── Why this exists ─────────────────────────────────────────────────────
 * The alternative is a hand-written table of `!important` overrides
 * remapping individual Tailwind palette shades per theme
 * (`[data-theme="dark"] .bg-blue-50 { ... !important }`). Such a table is
 * an allowlist over an open set: every colour a developer reaches for that
 * nobody remembered to add renders light-on-light in dark mode, and two
 * apps each keeping their own table disagree by a shade on the same class.
 *
 * Here the palette itself is theme-aware instead. Every colour resolves
 * through a CSS variable, so one definition covers every theme, no
 * `!important` is needed, and a new theme is a new variable block rather
 * than a new table.
 *
 * ── Why the transform is property-scoped ────────────────────────────────
 * The same shade needs opposite treatment depending on what it paints:
 *
 *   `text-red-900`          light-mode dark text  → must LIGHTEN in dark mode
 *   `dark:bg-red-900/20`    deliberate dark tint  → must STAY DARK in dark mode
 *
 * One variable per colour cannot satisfy both, so `bg`, `tx` (text) and
 * `bd` (border) get separate variables, configured through Tailwind's
 * separate `backgroundColor` / `textColor` / `borderColor` scales.
 *
 * ── Why alpha is a variable too ─────────────────────────────────────────
 * Dark themes tint light backgrounds with a low alpha (`rgba(... , 0.1)`)
 * so a chip blends with whatever surface it sits on. Baking that into a
 * solid colour would break on raised surfaces, so the alpha stays a
 * variable and the colour is emitted as `rgb(var(--bg-x) / var(--bg-x-a, 1))`.
 * An explicit opacity modifier (`bg-blue-500/20`) still overrides it,
 * because Tailwind substitutes the modifier for the alpha.
 */

/** Shades Tailwind ships for every palette family. */
export const SHADES = ['50', '100', '200', '300', '400', '500', '600', '700', '800', '900', '950'];

/**
 * Families we emit variables for. Deliberately the full Tailwind set rather
 * than only what is used today: the whole point is that reaching for a new
 * colour cannot introduce a dark-mode bug. Generating all of them costs
 * ~4 KB gzipped of variable declarations.
 */
export const FAMILIES = [
    'slate', 'gray', 'zinc', 'neutral', 'stone',
    'red', 'orange', 'amber', 'yellow', 'lime', 'green', 'emerald',
    'teal', 'cyan', 'sky', 'blue', 'indigo', 'violet', 'purple',
    'fuchsia', 'pink', 'rose',
];

/** `#rrggbb` → `"r g b"` channel triple (the space-separated form `rgb()` needs). */
export function hexToChannels(hex) {
    const h = hex.replace('#', '').trim();
    const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
    if (!/^[0-9a-fA-F]{6}$/.test(full)) throw new Error(`not a hex colour: ${hex}`);
    const n = parseInt(full, 16);
    return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`;
}

/**
 * Per-theme, per-role transform.
 *
 * `shadeMap` remaps a shade onto another shade of the same family.
 * `alpha` gives the default alpha for a shade (omitted ⇒ fully opaque).
 * A shade absent from both is left exactly as Tailwind ships it — that is
 * what keeps author-written `dark:` variants meaning what they say.
 */
export const THEME_TRANSFORMS = {
    light: {
        // Light mode is the palette as published: no remapping at all.
        bg: {}, tx: {}, bd: {},
    },
    dark: {
        // Light chip/panel fills become translucent tints of the family's
        // mid shade, so they blend with whichever surface they sit on.
        // Applies to every family.
        bg: {
            shadeMap: { 50: '500', 100: '500', 200: '500' },
            alpha: { 50: 0.1, 100: 0.15, 200: 0.22 },
        },
        // Dark text was authored to sit on a light background. On a dark
        // surface it has to come up the ramp.
        tx: {
            shadeMap: { 600: '400', 700: '300', 800: '200', 900: '100', 950: '50' },
        },
        bd: {
            shadeMap: { 50: '500', 100: '500', 200: '500' },
            alpha: { 50: 0.2, 100: 0.25, 200: 0.3 },
        },
    },
    'high-contrast': {
        // Same shape as dark, pushed further: denser fills, brighter text,
        // more present borders. Mirrors the portal's high-contrast table.
        bg: {
            shadeMap: { 50: '500', 100: '500', 200: '500' },
            alpha: { 50: 0.2, 100: 0.25, 200: 0.3 },
        },
        tx: {
            shadeMap: { 600: '300', 700: '200', 800: '100', 900: '50', 950: '50' },
        },
        bd: {
            shadeMap: { 50: '500', 100: '500', 200: '500' },
            alpha: { 50: 0.4, 100: 0.4, 200: 0.4 },
        },
    },
};

/**
 * Resolve one (theme, role, family, shade) to the channels + alpha that the
 * theme's variable block should declare, or `null` when the theme leaves
 * Tailwind's published value alone (nothing to emit).
 *
 * @param {Record<string, Record<string, string>>} palette Tailwind colour object
 */
export function resolve(palette, theme, role, family, shade) {
    const transform = THEME_TRANSFORMS[theme]?.[role];
    if (!transform) throw new Error(`no transform for ${theme}/${role}`);
    const mappedShade = transform.shadeMap?.[shade];
    const alpha = transform.alpha?.[shade];
    if (mappedShade === undefined && alpha === undefined) return null;
    const sourceShade = mappedShade ?? shade;
    const hex = palette[family]?.[sourceShade];
    if (!hex) throw new Error(`palette has no ${family}-${sourceShade}`);
    return { channels: hexToChannels(hex), alpha };
}

/** Variable name for a role/family/shade, e.g. `--bg-blue-50`. */
export function varName(role, family, shade) {
    return `--${role}-${family}-${shade}`;
}

/** Variable name holding the *base* (light) channels, shared by all roles. */
export function baseVarName(family, shade) {
    return `--p-${family}-${shade}`;
}
