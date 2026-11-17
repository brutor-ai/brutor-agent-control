/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

import { SlotContext, type SlotRegistry } from './slots';

/** Provides the slot items to every page below it. See `slots.ts`. */
export const SlotProvider: React.FC<{ slots: SlotRegistry; children: React.ReactNode }> = ({ slots, children }) => (
    <SlotContext.Provider value={slots}>{children}</SlotContext.Provider>
);
