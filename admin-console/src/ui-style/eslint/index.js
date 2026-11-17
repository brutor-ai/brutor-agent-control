/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Flat-config ESLint plugin for the shared style system.
 *
 *     import brutorStyle from './src/ui-style/eslint/index.js';
 *
 *     export default [
 *       { plugins: { 'brutor-style': brutorStyle },
 *         rules: { 'brutor-style/prefer-layout-primitives': 'error' } },
 *     ];
 */

import noHardcodedColor from './no-hardcoded-color.js';
import preferLayoutPrimitives from './prefer-layout-primitives.js';

const plugin = {
    meta: { name: 'brutor-style', version: '0.1.0' },
    rules: {
        'prefer-layout-primitives': preferLayoutPrimitives,
        'no-hardcoded-color': noHardcodedColor,
    },
};

export default plugin;
