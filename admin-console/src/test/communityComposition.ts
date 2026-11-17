/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console as the Community Edition composes it: the built-in
 * registries and no extension.
 *
 * These are the built-in registries themselves, under the names the tests
 * use. `app/__tests__/composition.community.test.tsx` holds them to their
 * record (`app/__tests__/recorded/composition.community.json`).
 */

import { composeAdmin, type AdminComposition } from '../app/extensions';
import { BUILTIN_SLOTS } from '../app/builtinSlots';
import { NAV_SECTIONS, type NavSection } from '../app/navigation';
import { SCREENS, type ScreenRoute } from '../app/screens';
import type { SlotRegistry } from '../app/slots';
import { GatewayApiBase } from '../lib/gatewayApi/base';
import { BUILTIN_API_MODULES, installApiModules } from '../lib/gatewayApi/modules';

export function communityScreens(): ScreenRoute[] {
    return [...SCREENS];
}

export function communityNavigation(): NavSection[] {
    return [...NAV_SECTIONS];
}

export function communitySlots(): SlotRegistry {
    return { ...BUILTIN_SLOTS };
}

export function communityComposition(): AdminComposition {
    return composeAdmin([]);
}

/** An API client of the Community modules alone, whatever is installed on the shared client class. */
export class CommunityApiClient extends GatewayApiBase {}
installApiModules(CommunityApiClient, BUILTIN_API_MODULES);
