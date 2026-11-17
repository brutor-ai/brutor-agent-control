/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console as shipped, and its Community half, written down.
 *
 * `recorded/composition.community.json` is the whole console with everything
 * that names an Enterprise feature taken out. The built-in registries are
 * the Community Edition, and the Enterprise pieces are one extension. What
 * is held here is the Community client; the Community composition itself is
 * held to its record in `composition.community.test.tsx`, and the shipped
 * composition and its record are Enterprise tests.
 *
 * What is recorded: the navigation as an ordered tree (ids, labels, icons,
 * gates), the screens and what each claims, the screen that answers every
 * tab id, the slot items in order, and the API client's modules and methods.
 */

import path from 'node:path';

import { describe, expect, it } from 'vitest';

import {
    asRecord,
    describeApiClient,
} from '../../test/describeComposition';
import { CommunityApiClient } from '../../test/communityComposition';

const RECORDED = path.resolve(__dirname, 'recorded');

describe('the Community half of the console as shipped', () => {

    it('has the recorded API client: the Community modules in order, and their methods', async () => {
        await expect(asRecord(describeApiClient(CommunityApiClient))).toMatchFileSnapshot(
            path.join(RECORDED, 'apiClient.community.json'),
        );
    });
});
