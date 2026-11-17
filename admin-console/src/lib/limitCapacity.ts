/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The arithmetic behind an inherited limit field: what this group may set,
 * given the ceiling merged from its ancestors and how much of the shared
 * budget the other groups drawing on it have already claimed.
 *
 * Kept free of React so the rules can be pinned without rendering, and so
 * `InheritedLimitField.tsx` exports components only.
 */

import { LimitCapacityInfo } from './groupTypes';

/** Read `section.field` out of a limits blob. */
export function getNestedLimit(
    limits: Record<string, unknown> | null | undefined,
    section: string,
    field: string,
): number | undefined {
    if (!limits) return undefined;
    const sec = (limits)[section] as Record<string, unknown> | undefined;
    const val = sec?.[field];
    return typeof val === 'number' ? val : undefined;
}

/** The capacity picture for one field, sliced out of the group-wide blob. */
export interface FieldCapacity {
    /** The limit set by the nearest ancestor that sets one. */
    holderLimit: number | undefined;
    holderName: string | null;
    /** False when the budget comes from a grandparent or higher. */
    holderIsParent: boolean;
    /** What the other groups drawing on that budget have claimed. */
    claimed: number | undefined;
    /** Holder limit minus claimed. */
    available: number | undefined;
    others: { id: string; name: string; value: number | undefined }[];
}

export function fieldCapacity(
    capacity: LimitCapacityInfo | null | undefined,
    section: string,
    field: string,
): FieldCapacity | null {
    if (!capacity) return null;
    const holderLimit = getNestedLimit(capacity.parent_limit, section, field);
    if (holderLimit === undefined) return null;
    return {
        holderLimit,
        holderName: capacity.parent_name,
        holderIsParent: capacity.parent_is_immediate,
        claimed: getNestedLimit(capacity.sibling_total, section, field),
        available: getNestedLimit(capacity.available, section, field),
        others: (capacity.siblings ?? []).map((s) => ({
            id: s.group_id,
            name: s.group_name,
            value: getNestedLimit(s.limits, section, field),
        })),
    };
}

export interface LimitFieldVerdict {
    /** Above the inherited floor — the API will refuse this on save. */
    overInherited: boolean;
    /** Above what is left of the shared budget — a warning, not a refusal. */
    overCapacity: boolean;
    /** The tightest ceiling worth showing in the input's `max`. */
    ceiling: number | undefined;
}

/**
 * Pure: how a typed value stands against its inherited floor and the
 * unclaimed capacity. Kept separate from the markup so the rules can be
 * pinned without rendering.
 */
export function limitFieldVerdict(
    current: number | undefined,
    inherited: number | undefined,
    capacity: FieldCapacity | null,
): LimitFieldVerdict {
    const available = capacity?.available;
    const ceilings = [inherited, available].filter(
        (n): n is number => typeof n === 'number',
    );
    return {
        overInherited:
            current !== undefined && inherited !== undefined && current > inherited,
        overCapacity:
            current !== undefined && available !== undefined && current > available,
        ceiling: ceilings.length ? Math.min(...ceilings) : undefined,
    };
}
