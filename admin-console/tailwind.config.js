/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import brutorPreset from './src/ui-style/tailwind/preset.js';

/**
 * Everything theme- and colour-related lives in the shared preset
 * (`brutor-ui-style`), so the admin console and the user portal cannot
 * drift apart.
 *
 * Only `content` belongs here — the paths are app-relative.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
    presets: [brutorPreset],
    content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
};
