/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
  * Which system alerts the drawer must NOT list a second time, because the
  * assurance inbox already shows them. Over-matching hides an alert nothing
  * else is showing; under-matching shows it twice.
  */

import { describe, it, expect } from 'vitest';
import { isMirroredInInbox } from '../systemAlerts';
import type { SystemAlert } from '../groupTypes';

const alert = (over: Partial<SystemAlert> = {}): SystemAlert => ({
    id: 'a1',
    tenant_id: 't1',
    severity: 'high',
    category: 'downstream_provider',
    source: 'proxy.llm',
    resource_type: null,
    resource_id: null,
    resource_name: null,
    error_code: null,
    title: 'Upstream 503',
    message: 'provider unavailable',
    context: null,
    status: 'active',
    first_seen_at: null,
    last_seen_at: null,
    occurrence_count: 1,
    acknowledged_at: null,
    acknowledged_by: null,
    resolved_at: null,
    resolved_by: null,
    resolution_note: null,
    dedup_key: 'k1',
    ...over,
});

describe('isMirroredInInbox', () => {
    it('mirrors liveness alerts by category alone', () => {
        expect(isMirroredInInbox(alert({ category: 'ai_system_liveness' }))).toBe(
            true
        );
    });

    it('mirrors an ai_system alert raised by an ai_system.* source', () => {
        expect(
            isMirroredInInbox(
                alert({ resource_type: 'ai_system', source: 'ai_system.drift' })
            )
        ).toBe(true);
    });

    it('needs BOTH the resource type and the source prefix', () => {
        expect(
            isMirroredInInbox(
                alert({ resource_type: 'ai_system', source: 'proxy.llm' })
            )
        ).toBe(false);
        expect(
            isMirroredInInbox(
                alert({ resource_type: 'llm_model', source: 'ai_system.drift' })
            )
        ).toBe(false);
    });

    it('does not mirror an ordinary downstream alert', () => {
        expect(isMirroredInInbox(alert())).toBe(false);
    });

    it('does not match a source that merely contains "ai_system."', () => {
        expect(
            isMirroredInInbox(
                alert({ resource_type: 'ai_system', source: 'proxy.ai_system.drift' })
            )
        ).toBe(false);
    });

    it('survives a null source rather than throwing', () => {
        expect(
            isMirroredInInbox(
                alert({ resource_type: 'ai_system', source: null as unknown as string })
            )
        ).toBe(false);
    });
});
