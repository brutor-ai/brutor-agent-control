/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * For tests of a page that has slots: renders the page the way the Community
 * console does, with the console's own slot items provided.
 *
 *     render(<UserManagementTab />, { wrapper: WithBuiltinSlots });
 *
 * The console's own registry (`app/builtinSlots.tsx`) holds no item:
 * every slot item belongs to an Enterprise feature and comes from the
 * Enterprise extension. So a page rendered with
 * this wrapper shows what the Community Edition shows — the page with
 * nothing contributed — and will show a Community item the day the console
 * has one.
 *
 * The cases of a page that need a real Enterprise item are Enterprise tests: they are under `enterprise/src/host/`, beside a wrapper that
 * provides the shipped Enterprise items (`enterprise/src/test/shippedSlots`).
 */

import React from 'react';

import { BUILTIN_SLOTS } from '../app/builtinSlots';
import { SlotProvider } from '../app/SlotProvider';

export const WithBuiltinSlots: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <SlotProvider slots={BUILTIN_SLOTS}>{children}</SlotProvider>
);
