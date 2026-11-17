/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type { SystemAlert } from './groupTypes';

/** System alerts that are also raised as assurance-inbox findings (liveness
 *  misses today). They stay in the store for paging and channels; the
 *  alerts drawer shows them through its Inbox line instead of twice. */
export function isMirroredInInbox(a: SystemAlert): boolean {
    return (
        a.category === 'ai_system_liveness' ||
        (a.resource_type === 'ai_system' && typeof a.source === 'string' && a.source.startsWith('ai_system.'))
    );
}
