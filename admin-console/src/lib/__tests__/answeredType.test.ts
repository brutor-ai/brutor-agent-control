/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * `Answered<T>`: a response model as the Control Plane sends it. A field
 * that has a default is optional in the generated types (a request may leave
 * it out), and always present in an answer, where the default is filled in.
 * Held at the type check: each assignment below compiles only while the
 * helper does what its name says.
 */

import { describe, expect, it } from 'vitest';

import type { Answered } from '../api/controlPlaneTypes';
import type { components } from '../generated/control-plane-api';

type Schema = { count?: number; nested?: { on?: boolean; note?: string | null } | null; list?: Array<{ id?: string }> };

describe('Answered', () => {
    it('makes every field required, at every depth of a modelled object, and keeps null where the contract has it', () => {
        const answered: Answered<Schema> = { count: 1, nested: { on: true, note: null }, list: [{ id: 'a' }] };
        const count: number = answered.count;
        const on: boolean | undefined = answered.nested?.on;
        const id: string = answered.list[0].id;
        expect([count, on, id]).toEqual([1, true, 'a']);
    });

    it('reads a defaulted field of a generated response without a guard', () => {
        const metrics = { avg_duration_ms: 3 } as Answered<components['schemas']['A2AUsageMetrics']>;
        const avg: number = metrics.avg_duration_ms;
        expect(avg).toBe(3);
    });
});
