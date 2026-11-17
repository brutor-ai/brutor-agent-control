/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A read a component says failed is shown by that component.
 *
 * Every part a Community component marks as failed (`useReadFailures`) is a
 * part it asks the failure of, by the same name: the sentence reaches the
 * page. The two pages that show their parts from a list are named, with the
 * list.
 */

import { describe, expect, it } from 'vitest';

import { filesHoldingReadFailures, partsMarkedNotShown, unshownReadFailures } from '../test/readFailures';
import { SRC_ROOT } from '../test/sourceRoots';

/** Pages that show the failures of their parts from a list of the parts, not one by one. */
const SHOWN_BY_LIST = [
    // `ANALYTICS_TAB_PARTS`: each tab shows the failures of the reads its cards rest on.
    'src/components/monitoring/MissionControl.tsx',
    // `ALERT_FEEDS`: the bell and the drawer are given the failure of each feed.
    'src/components/monitoring/MissionControlDashboard.tsx',
];

describe('read failures are shown (Community)', () => {
    it('sees a part that is marked and never shown', () => {
        const planted = "reads.failed('alerts', 'Failed', e); reads.lost('groups', 'Failed', e); <ReadFailureNotice failure={readFailure('groups')} />";
        expect(partsMarkedNotShown(planted)).toEqual(['alerts']);
        expect(partsMarkedNotShown("feeds.failed('usage', what, reason); feedFailure('usage')")).toEqual([]);
    });

    it('looks at the components that hold read failures', () => {
        // Not a floor to keep; only that the scan is not of nothing.
        expect(filesHoldingReadFailures(SRC_ROOT).length).toBeGreaterThan(40);
    });

    it('every part a component marks as failed, it shows', () => {
        expect(unshownReadFailures(SRC_ROOT, SHOWN_BY_LIST)).toEqual([]);
    });
});
