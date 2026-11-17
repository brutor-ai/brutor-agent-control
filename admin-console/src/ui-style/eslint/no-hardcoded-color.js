/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Flags hard-coded colours in JSX `style` props and Tailwind arbitrary
 * values (`bg-[#1BB5E8]`).
 *
 * A literal colour cannot follow the theme: it stays put when the user
 * switches to dark or high contrast, and it ignores the tenant's brand
 * colour entirely. Every colour in the apps should come from a token —
 * a `*-primary-*`/`*-surface*`/`text-default` utility, or `var(--color-*)`
 * where a raw CSS value is unavoidable (canvas, SVG gradients, charts).
 */

const HEX = /#[0-9a-fA-F]{3,8}\b/;
const FUNCTIONAL = /\b(?:rgba?|hsla?)\(\s*\d/;

function isColorish(text) {
    return HEX.test(text) || FUNCTIONAL.test(text);
}

/** @type {import('eslint').Rule.RuleModule} */
export default {
    meta: {
        type: 'suggestion',
        docs: { description: 'Colours must come from theme tokens, not literals' },
        schema: [],
        messages: {
            inStyle:
                'Hard-coded colour in a style prop: it will not follow the theme or the tenant brand. Use a token utility, or `var(--color-*)` if a raw value is unavoidable.',
            inClass:
                'Hard-coded colour in an arbitrary Tailwind value: it will not follow the theme. Use a palette or token utility instead.',
        },
    },
    create(context) {
        return {
            JSXAttribute(node) {
                const name = node.name?.name;
                if (name !== 'style' && name !== 'className') return;

                const source = context.sourceCode ?? context.getSourceCode();
                const value = node.value;
                if (!value) return;

                // Ignore values that already reference a CSS variable: that
                // is the sanctioned escape hatch.
                const text = source.getText(value);
                if (text.includes('var(--')) return;
                if (!isColorish(text)) return;

                context.report({ node, messageId: name === 'style' ? 'inStyle' : 'inClass' });
            },
        };
    },
};
