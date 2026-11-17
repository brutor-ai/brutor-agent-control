/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { notServedReasons, notServedText } from '../modelServed';

const TODAY = new Date('2026-11-14T15:00:00Z');
const PROVIDERS = [
    { key: 'openai', enabled: true },
    { key: 'acme', enabled: false },
];
const model = (over: Record<string, unknown> = {}) => ({ provider: 'openai', mode: 'chat', retire_date: null, ...over });

describe('whether the gateway serves a model', () => {
    it('serves a model with no retire date and an enabled provider', () => {
        expect(notServedReasons(model(), PROVIDERS, TODAY)).toEqual([]);
    });

    it('does not serve a model from its retire date on (UTC)', () => {
        expect(notServedReasons(model({ retire_date: '2026-11-15' }), PROVIDERS, TODAY)).toEqual([]);
        expect(notServedReasons(model({ retire_date: '2026-11-14' }), PROVIDERS, TODAY)).toEqual([{ why: 'retired', retireDate: '2026-11-14' }]);
        expect(notServedReasons(model({ retire_date: '2026-11-01' }), PROVIDERS, TODAY)).toEqual([{ why: 'retired', retireDate: '2026-11-01' }]);
    });

    it('reads the date of a retire date that carries a time', () => {
        expect(notServedReasons(model({ retire_date: '2026-11-14T00:00:00' }), PROVIDERS, TODAY)).toEqual([
            { why: 'retired', retireDate: '2026-11-14' },
        ]);
    });

    it('judges the retire date by the UTC day, not the local one', () => {
        // 23:30 UTC on the 13th: the 14th has not begun in UTC.
        expect(notServedReasons(model({ retire_date: '2026-11-14' }), PROVIDERS, new Date('2026-11-13T23:30:00Z'))).toEqual([]);
    });

    it('does not serve a model of a disabled provider', () => {
        expect(notServedReasons(model({ provider: 'acme' }), PROVIDERS, TODAY)).toEqual([{ why: 'provider_disabled', provider: 'acme' }]);
    });

    it('gives both reasons when both hold', () => {
        expect(notServedReasons(model({ provider: 'acme', retire_date: '2020-01-01' }), PROVIDERS, TODAY)).toEqual([
            { why: 'retired', retireDate: '2020-01-01' },
            { why: 'provider_disabled', provider: 'acme' },
        ]);
    });

    it('does not judge a routing group by a provider: its members are judged one by one', () => {
        expect(notServedReasons(model({ provider: 'acme', mode: 'routing' }), PROVIDERS, TODAY)).toEqual([]);
    });

    it('says nothing of a provider it has not been told about', () => {
        expect(notServedReasons(model({ provider: 'unknown' }), PROVIDERS, TODAY)).toEqual([]);
        expect(notServedReasons(model({ provider: 'acme' }), [], TODAY)).toEqual([]);
    });

    it('says why in plain words', () => {
        expect(notServedText([])).toBe('');
        expect(notServedText([{ why: 'retired', retireDate: '2026-11-01' }])).toBe(
            'The gateway does not serve this model. Retire date: 2026-11-01. From its retire date on, a model takes no call.',
        );
        expect(notServedText([{ why: 'provider_disabled', provider: 'acme' }])).toBe(
            'The gateway does not serve this model: its provider (acme) is disabled. A model of a disabled provider takes no call.',
        );
        expect(
            notServedText([
                { why: 'retired', retireDate: '2026-11-01' },
                { why: 'provider_disabled', provider: 'acme' },
            ]),
        ).toBe(
            'The gateway does not serve this model. Retire date: 2026-11-01. From its retire date on, a model takes no call. Its provider (acme) is also disabled.',
        );
    });
});
