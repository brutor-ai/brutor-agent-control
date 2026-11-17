/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A failure that is caught is shown with what the other side said, or thrown.
 *
 * Every `catch` of the Community tree that drops the caught value
 * (`src/test/caughtValues.ts` says what that is) is in
 * `recorded/caughtValuesDropped.community.json` with the reason it may
 * (`REASONS`). A new one fails here until it shows the failure, throws it,
 * or is recorded with a reason; one that was fixed fails here until its
 * entry is taken out, so the list only says what is true.
 *
 * A rejected member of a `Promise.allSettled` is caught too, by the call
 * itself: each one's `reason` is read (handed to the read-failure marks or
 * to `failureSentence`) or thrown. None is recorded as dropped: the list of
 * such places is empty in both editions.
 *
 * A read that fails says so where its data would be. The few that may fail
 * quietly fetch decoration (`DECORATION`: a badge, the theme, help
 * text); their count only goes down.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { DECORATION, REASONS, byFile, droppedCatches, droppedSettled, droppedSettledIn, type RecordedDrops } from '../test/caughtValues';
import { SRC_ROOT } from '../test/sourceRoots';

const recorded = JSON.parse(readFileSync(path.join(__dirname, 'recorded', 'caughtValuesDropped.community.json'), 'utf8')) as RecordedDrops;

/** The most reads of the Community tree that may fail quietly because what they fetch is decoration (`DECORATION`). Only goes down. */
const MAX_DECORATION_READS = 3;

describe('caught values dropped (Community)', () => {
    it('every catch that drops the caught value is recorded, and every recorded one is still there', () => {
        const places = Object.fromEntries(
            Object.entries(recorded).map(([file, list]) => [file, list.map(({ in: where, guards }) => ({ in: where, guards }))]),
        );
        expect(byFile(droppedCatches(SRC_ROOT))).toEqual(places);
    });

    it('every recorded place names a reason', () => {
        const unknown = Object.entries(recorded).flatMap(([file, list]) =>
            list.filter((place) => !(place.why in REASONS)).map((place) => `${file}: ${place.guards} (${String(place.why)})`),
        );
        expect(unknown).toEqual([]);
    });

    it('no read fails without a sentence: the reason for that is not one a place may name', () => {
        expect(Object.keys(REASONS)).not.toContain('quiet read');
    });

    it('the reads left quiet as decoration only get fewer', () => {
        const quiet = Object.values(recorded).flat().filter((place) => DECORATION.includes(place.why)).length;
        expect(quiet).toBeLessThanOrEqual(MAX_DECORATION_READS);
        // A cap with slack in it permits what it is there to prevent.
        expect(MAX_DECORATION_READS - quiet).toBe(0);
    });
});

describe('rejected members of Promise.allSettled (Community)', () => {
    const where = (text: string) => droppedSettledIn(path.join(SRC_ROOT, 'planted.tsx'), text).map(({ in: fn, members }) => `${fn}: ${members.join(', ')}`);

    it('none is ignored anywhere in the tree', () => {
        expect(droppedSettled(SRC_ROOT)).toEqual([]);
    });

    it('a member whose rejection is never read is found, by its name', () => {
        expect(
            where(`
                async function load() {
                    const [a, b] = await Promise.allSettled([one(), two()]);
                    if (a.status === 'fulfilled') setA(a.value);
                    if (b.status === 'fulfilled') setB(b.value); else marks.failed('b', 'Failed to load b', b.reason);
                }
            `),
        ).toEqual(['load: a']);
    });

    it('so is one read only into the console, one left out of the pattern, and a result nobody takes', () => {
        expect(
            where(`
                const quiet = async () => {
                    const [a] = await Promise.allSettled([one(), two()]);
                    if (a.status === 'rejected') console.error('one failed', a.reason);
                };
                function fire() {
                    void Promise.allSettled([one(), two()]);
                }
                const chained = () => Promise.allSettled([one()]).then(([a]) => { if (a.status === 'fulfilled') setA(a.value); });
            `),
        ).toEqual(['quiet: a, #2', 'fire: (all)', 'chained: a']);
    });

    it('a list of results that is never asked for a reason is found whole', () => {
        expect(
            where(`
                async function removeAll() {
                    const results = await Promise.allSettled(ids.map(remove));
                    setRemoved(results.filter((r) => r.status === 'fulfilled').length);
                }
            `),
        ).toEqual(['removeAll: results']);
    });

    it('a member is read where its reason is marked, said, thrown, or handed on to code that reads it', () => {
        expect(
            where(`
                async function load() {
                    const [a, b, c, d] = await Promise.allSettled([one(), two(), three(), four()]);
                    if (a.status === 'rejected') marks.failed('a', 'Failed to load a', a.reason);
                    if (b.status === 'rejected') setError(failureSentence('Failed to load b', b.reason));
                    if (c.status === 'rejected') throw c.reason;
                    for (const [part, result] of [['d', d]] as const) {
                        if (result.status === 'rejected') marks.failed(part, 'Failed to load d', result.reason);
                    }
                }
                function chained() {
                    void Promise.allSettled(ids.map(remove)).then((results) => {
                        for (const r of results) if (r.status === 'rejected') marks.failed('remove', 'Failed to remove', r.reason);
                    });
                }
            `),
        ).toEqual([]);
    });
});
