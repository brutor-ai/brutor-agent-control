/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The links a Community page asks for, to pages it does not own. `app/links.ts` says who answers; this module is the names and the
 * shape of an answer, for code that builds a model outside React.
 */

/** What the Community pages ask for. An item of `app.links` answers one by its id. */
export const CONTRIBUTED_LINKS = {
    /** Where the cache policies of the tenant are managed. */
    cachePolicies: 'cache-policies',
    /** Where the knowledge bases of the tenant are managed. */
    knowledgeBases: 'knowledge-bases',
} as const;

/** The tab id to open for a link, or null when no page answers it. */
export type LinkResolver = (link: string) => string | null;

/** No page answers anything: the console with nothing contributed. */
export const NO_CONTRIBUTED_LINKS: LinkResolver = () => null;
