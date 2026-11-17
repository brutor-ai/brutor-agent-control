/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

import { ThemeSelector } from '../../ui-style/react/index';

/**
 * Appearance settings. The picker itself is the shared `ThemeSelector`, so
 * the admin console and the portal present themes identically and a new
 * theme appears in both without either app being edited — it renders one
 * card per theme the provider offers.
 */
const AppearanceSettings: React.FC = () => (
    <div className="space-y-6">
        <ThemeSelector description="Choose the appearance of the admin console." />
    </div>
);

export default AppearanceSettings;
