/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Links from a page of the console to a page it does not own.
 *
 * A Community page that would link to a page another distribution
 * contributes does not name that page's tab. It names what it wants to link
 * to (`CONTRIBUTED_LINKS`, `lib/contributedLinks.ts`), and asks. Whoever contributes the page answers
 * with its tab id (slot `app.links`); with nobody answering there is no
 * page, the resolver returns null, and the Community page shows no link.
 */

import { useMemo } from 'react';

import { NO_CONTRIBUTED_LINKS, type LinkResolver } from '../lib/contributedLinks';
import { useSlot, type LinkSlotItem } from './slots';

export { CONTRIBUTED_LINKS, NO_CONTRIBUTED_LINKS, type LinkResolver } from '../lib/contributedLinks';

/** The resolver over the items contributed. The first item answering a link wins. */
export function linkResolver(items: readonly LinkSlotItem[]): LinkResolver {
    if (items.length === 0) return NO_CONTRIBUTED_LINKS;
    return (link) => items.find((item) => item.id === link)?.tab ?? null;
}

/** The resolver of the console this component is rendered in. */
export function useContributedLinks(): LinkResolver {
    const items = useSlot('app.links');
    return useMemo(() => linkResolver(items), [items]);
}
