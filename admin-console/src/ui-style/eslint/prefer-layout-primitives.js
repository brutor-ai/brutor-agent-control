/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Flags ad-hoc utility combinations that only lay out correctly when a
 * global rule props them up, and points at the named primitive to use for
 * each one.
 *
 * Background: no stylesheet here redefines a Tailwind utility globally
 * (`.h-full.flex { overflow: hidden }`, `.flex-1 { min-height: 0 }`). Code
 * written against such rules reads as if it lays itself out but actually
 * depends on a stylesheet nobody opens — and when the stylesheet does not
 * match the markup (a shell written `flex h-screen` against a rule for
 * `h-full flex`) the result is content silently clipped with no scrollbar.
 * The primitives in `css/layout.css` say what they do at the call site.
 *
 * This rule is intentionally syntactic: it only reads static class strings
 * and never guesses at layout intent.
 */

/** Extract the static class tokens from a className value node. */
function staticClasses(node) {
    if (!node) return null;
    if (node.type === 'Literal' && typeof node.value === 'string') {
        return node.value.split(/\s+/).filter(Boolean);
    }
    if (node.type === 'JSXExpressionContainer') return staticClasses(node.expression);
    if (node.type === 'TemplateLiteral') {
        // Only the literal chunks are knowable; conditional branches are not.
        return node.quasis.flatMap((q) => q.value.cooked?.split(/\s+/).filter(Boolean) ?? []);
    }
    return null;
}

const RULES = [
    {
        id: 'app-column',
        test: (c) => c.includes('flex') && c.includes('flex-col') && c.includes('h-full'),
        message:
            '`flex flex-col h-full` relied on the deleted global `.h-full.flex` rules. Use the `app-column` primitive (adds min-height:0 without clipping), and give its scrolling child `scroll-region`.',
    },
    {
        id: 'app-shell',
        test: (c) => c.includes('flex') && c.includes('h-screen') && !c.includes('flex-col'),
        message:
            '`flex h-screen` is the app shell. Use the `app-shell` primitive so the full-viewport + clipping behaviour is declared, not inherited from a global rule.',
    },
    {
        id: 'scroll-region',
        // An explicit `min-h-0` means the author has declared the shrink
        // behaviour at the call site — that is the point, so it is not a
        // finding.
        test: (c) =>
            c.includes('flex-1') &&
            (c.includes('overflow-y-auto') || c.includes('overflow-auto')) &&
            !c.includes('min-h-0'),
        message:
            '`flex-1` + `overflow-y-auto` is the `scroll-region` primitive. Use it so the min-height:0 that makes scrolling work is part of the class, not of a global override.',
    },
    {
        id: 'overflow-auto-height',
        test: (c) =>
            c.includes('overflow-auto') &&
            !c.includes('flex-1') &&
            !c.includes('h-full') &&
            !c.includes('h-screen') &&
            !c.some((x) => x.startsWith('max-h-')) &&
            !c.includes('scroll-region'),
        message:
            'bare `overflow-auto` used to get `height: 100%; min-height: 0` from a global rule. Declare the height it needs (`h-full`), or use `scroll-region` inside an `app-column`.',
    },
];

/** @type {import('eslint').Rule.RuleModule} */
export default {
    meta: {
        type: 'problem',
        docs: {
            description:
                'Use the shared layout primitives instead of utility combinations that depended on global Tailwind overrides',
        },
        schema: [],
        messages: Object.fromEntries(RULES.map((r) => [r.id, '{{ message }}'])),
    },
    create(context) {
        return {
            JSXAttribute(node) {
                if (node.name?.name !== 'className') return;
                const classes = staticClasses(node.value);
                if (!classes?.length) return;
                // A className that uses a primitive needs no report.
                if (classes.some((c) => ['app-shell', 'app-column', 'scroll-region', 'app-region'].includes(c))) {
                    return;
                }
                for (const rule of RULES) {
                    if (rule.test(classes)) {
                        context.report({
                            node,
                            messageId: rule.id,
                            data: { message: rule.message },
                        });
                        return; // one finding per attribute is enough
                    }
                }
            },
        };
    },
};
