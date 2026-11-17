/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether a rendered page keeps fetching when nobody touches it.
 *
 * A page keys an effect on a function it gets from a hook. A test mock of
 * that hook which builds its functions (or the object holding them) anew on
 * every render gives the effect a new dependency each time: the effect
 * fetches, the answer sets state, the render makes new functions, and the
 * page fetches again for as long as it is mounted. The assertions still pass
 * on the first answer, so the loop only shows as a slow, flaky test.
 *
 * Mocks of hooks return the same function identities on every call. This
 * counts what a loop would add.
 */

interface Counted {
    mock: { calls: unknown[] };
}

const total = (mocks: Counted[]) => mocks.reduce((n, m) => n + m.mock.calls.length, 0);

/** The calls `mocks` receive while the page is left alone for `ms`. */
export async function callsWhileIdle(mocks: Counted[], ms = 150): Promise<number> {
    const before = total(mocks);
    // Not inside `act`: a looping page never lets `act` finish, and the test
    // would time out instead of saying how often the page fetched.
    await new Promise<void>((resolve) => setTimeout(resolve, ms));
    return total(mocks) - before;
}
