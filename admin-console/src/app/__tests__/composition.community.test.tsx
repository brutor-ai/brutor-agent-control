/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Community Edition is the console composed with no extension.
 *
 * `recorded/composition.community.json` and `recorded/apiClient.community.json`
 * are the records of the whole console with everything that names an
 * Enterprise feature left out (`composition.recorded.test.tsx`). Here the
 * built-in registries alone are held to those records: `composeAdmin([])`
 * is that console, with nothing left to leave out.
 *
 * Nothing in this file loads the Enterprise tree, so the API client class is
 * the one a Community console has: the test would not notice an Enterprise
 * module otherwise.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import {
    asRecord,
    describeApiClient,
    describeNavigation,
    describeResolution,
    describeScreens,
    describeSlots,
} from '../../test/describeComposition';
import { BUILTIN_API_MODULES } from '../../lib/gatewayApi/modules';
import { GatewayApiClient } from '../../lib/gatewayApiClient';
import { BUILTIN_SLOTS } from '../builtinSlots';
import { composeAdmin } from '../extensions';
import { NAV_SECTIONS } from '../navigation';
import { SCREENS } from '../screens';

const RECORDED = path.resolve(__dirname, 'recorded');
const recorded = <T,>(file: string): T => JSON.parse(readFileSync(path.join(RECORDED, file), 'utf8')) as T;

interface RecordedComposition {
    navigation: unknown[];
    screens: Array<{ name: string; edition: string | null }>;
    resolves: Record<string, string | null>;
    slots: Record<string, string[]>;
}

describe('the console composed with no extension', () => {
    const community = recorded<RecordedComposition>('composition.community.json');

    it.each([['composeAdmin()', () => composeAdmin()], ['composeAdmin([])', () => composeAdmin([])]])(
        '%s is the recorded Community navigation, screens, tab ids and slot items',
        async (_how, compose) => {
            const composition = compose();
            const described = {
                navigation: describeNavigation(composition),
                screens: describeScreens(composition.screens),
                // every tab id of the console that has the Enterprise extension is asked (the record keeps
                // them as its keys): an Enterprise one has no answer here
                resolves: describeResolution(composition.screens, Object.keys(community.resolves)),
                slots: describeSlots(composition),
            };
            await expect(asRecord(described)).toMatchFileSnapshot(path.join(RECORDED, 'composition.community.json'));
            expect(composition.extensions).toEqual([]);
        },
    );

    it('is the built-in registries themselves', () => {
        const composition = composeAdmin([]);
        expect(composition.screens).toEqual(SCREENS);
        expect(composition.navSections).toEqual(NAV_SECTIONS);
        expect(composition.slots).toEqual(BUILTIN_SLOTS);
    });

    it('has an API client of the Community modules alone: the recorded modules and methods', async () => {
        composeAdmin([]);
        await expect(asRecord(describeApiClient(GatewayApiClient))).toMatchFileSnapshot(path.join(RECORDED, 'apiClient.community.json'));
        expect(describeApiClient(GatewayApiClient).modules).toEqual(BUILTIN_API_MODULES.map((mod) => mod.name));
    });

    it('names no Enterprise feature anywhere: no screen, no slot item, no API module', () => {
        expect(SCREENS.filter((screen) => screen.edition !== undefined).map((screen) => screen.name)).toEqual([]);
        expect(Object.values(BUILTIN_SLOTS as Record<string, readonly unknown[]>).flat()).toEqual([]);
        expect(BUILTIN_API_MODULES.filter((mod) => mod.feature !== null).map((mod) => mod.name)).toEqual([]);
        expect(community.screens.filter((screen) => screen.edition !== null)).toEqual([]);
    });
});
