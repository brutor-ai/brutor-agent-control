/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What `BodyExactly` refuses, held by the type check.
 *
 * Nothing here runs and nothing imports this file: `tsc` reads it with the
 * rest of `src/`. Each `@ts-expect-error` stands on a body the check must
 * refuse; where the check stops refusing it, the directive is unused and
 * the type check fails. The lines without one are bodies it must take.
 *
 * The contract below is a stand-in with every shape a real one has: a
 * scalar, a field with a default, an object the Control Plane models, a
 * list of such objects, and a free dictionary.
 */

import type { BodyExactly } from './controlPlaneTypes';

interface Contract {
    name?: string | null;
    enabled: boolean;
    /** An object the Control Plane models. */
    exclusion?: { reason: 'retired' | 'offline'; note: string; expires_at?: string | null } | null;
    /** A list of objects it models. */
    providers?: { provider: string; enabled: boolean; config?: { [key: string]: unknown } | null }[];
    /** A free dictionary: nothing of it is named. */
    settings?: { [key: string]: unknown } | null;
}

declare function update<Sent>(body: BodyExactly<Contract, Sent>): void;

declare const dictionary: Record<string, unknown>;
/** A value typed `any`: what `JSON.parse` returns. */
declare const anything: ReturnType<typeof JSON.parse>;
declare const stored: { reason: 'retired'; note: string; by: string; at: string };
declare const storedProviders: { provider: string; enabled: boolean; mode: string }[];
declare const typedExclusion: { reason: 'offline'; note: string };

export function bodiesTheCheckTakes(): void {
    update({ name: 'a' });
    update({ enabled: false, exclusion: null });
    update({ exclusion: { reason: 'retired', note: 'no longer run' } });
    update({ exclusion: typedExclusion });
    update({ providers: [{ provider: 'p', enabled: true, config: { anything: 1, nested: { goes: true } } }] });
    update({ providers: [] });
    // A free dictionary takes any object: the contract names nothing of it.
    update({ settings: dictionary });
    update({ settings: { a: 1, b: { c: 2 } } });
}

export function bodiesTheCheckRefuses(): void {
    // A key the contract does not name, at the top.
    // @ts-expect-error -- `colour` is not a field of the contract
    update({ name: 'a', colour: 'red' });
    // A free dictionary as the body, and a value typed `any`.
    // @ts-expect-error -- a dictionary could hold any key
    update(dictionary);
    // @ts-expect-error -- and so could `any`
    update(anything);

    // Below the top level, in an object the contract models.
    // @ts-expect-error -- `by` is not a field of the exclusion
    update({ exclusion: { reason: 'retired', note: 'n', by: 'someone' } });
    // @ts-expect-error -- nor are the fields a stored exclusion was read with
    update({ exclusion: stored });
    // @ts-expect-error -- a dictionary where the contract models an object
    update({ exclusion: dictionary });
    // @ts-expect-error -- `any` where the contract models an object
    update({ exclusion: anything });

    // In each item of a list the contract models.
    // @ts-expect-error -- `mode` is not a field of a provider
    update({ providers: [{ provider: 'p', enabled: true, mode: 'block' }] });
    // @ts-expect-error -- nor when the list is held in a variable
    update({ providers: storedProviders });
    // @ts-expect-error -- a list of dictionaries
    update({ providers: [dictionary] });
}

/**
 * What the compiler does not see. Each of these compiles, and is the
 * reader's to refuse: no body of the console is written this way.
 */
export function bodiesTheCompilerCannotRefuse(): void {
    // A dictionary spread into an object literal: the literal's type is the
    // type of its named keys alone, at the top and below it.
    update({ name: 'a', ...dictionary });
    update({ exclusion: { reason: 'retired', note: 'n', ...dictionary } });
    // A value asserted to a type that passes.
    update(dictionary as { name: string });
    // Inside a free dictionary of the contract, anything.
    update({ settings: { ...dictionary, whatever: anything } });
}
