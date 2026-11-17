/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The name of the console, for code both editions share.
 *
 * Each edition has a page of its own (`index.html`, `enterprise/index.html`)
 * and the two differ by the name alone: "Brutor Agent Control Admin Console"
 * and "Brutor Agent Control Enterprise Admin Console". Shared code (the entry
 * point's loading and error screens, the title of the tab) reads the name
 * from the page it runs in, so it needs no knowledge of the editions. It
 * reads `<meta name="title">`, not `document.title`: the tab's title becomes
 * a tenant's company name once one is loaded.
 */

/** The name of the Community console, and of a page that declares none. */
export const COMMUNITY_CONSOLE_NAME = 'Brutor Agent Control Admin Console';

/** The product the console administers. */
export const PRODUCT_NAME = 'Brutor Agent Control';

const CONSOLE = ' Admin Console';

/** The name of the console this page is. */
export function consoleName(): string {
    const declared = document.querySelector('meta[name="title"]')?.getAttribute('content')?.trim();
    return declared || COMMUNITY_CONSOLE_NAME;
}

/**
 * The product, as the edition this page is names it: "Brutor Agent Control",
 * or "Brutor Agent Control Enterprise". It is the console's name without the
 * words "Admin Console" (the sign-in form's heading, and the line with the
 * version under it).
 */
export function productName(): string {
    const name = consoleName();
    return name.endsWith(CONSOLE) ? name.slice(0, -CONSOLE.length) : name;
}
