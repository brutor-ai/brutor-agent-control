/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The feeds the Mission Control bell counts and its drawer lists. Each is
 * read on its own, and each can fail on its own.
 */

export type AlertFeed = 'system' | 'usage' | 'drift' | 'inbox';

export const ALERT_FEEDS: readonly AlertFeed[] = ['system', 'usage', 'drift', 'inbox'];

/** What is said of a feed that could not be read, before what the other side said. */
export const ALERT_FEED_FAILED: Record<AlertFeed, string> = {
    system: 'Failed to load the system alerts',
    usage: 'Failed to load the usage alerts',
    drift: 'Failed to load the open drift findings',
    inbox: 'Failed to load the count of open assurance findings',
};
