/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';
import {
    errorKind,
    errorMessage,
    groupedReasons,
    errorStatus,
    failureSentence,
    getArray,
    getBoolean,
    getNumber,
    getRecord,
    getString,
    isRecord,
    parseJson,
} from '../narrow';

describe('isRecord', () => {
    it('accepts plain objects only', () => {
        expect(isRecord({ a: 1 })).toBe(true);
        expect(isRecord(Object.create(null))).toBe(true);
    });

    it('rejects null, arrays and primitives', () => {
        // `typeof null === 'object'` is the classic trap this guard exists for.
        expect(isRecord(null)).toBe(false);
        expect(isRecord([1, 2])).toBe(false);
        expect(isRecord('x')).toBe(false);
        expect(isRecord(undefined)).toBe(false);
    });
});

describe('typed property readers', () => {
    const obj = { s: 'a', n: 3, b: false, nested: { k: 1 }, arr: [1], nan: NaN };

    it('returns the value when the type matches', () => {
        expect(getString(obj, 's')).toBe('a');
        expect(getNumber(obj, 'n')).toBe(3);
        expect(getBoolean(obj, 'b')).toBe(false);
        expect(getRecord(obj, 'nested')).toEqual({ k: 1 });
        expect(getArray(obj, 'arr')).toEqual([1]);
    });

    it('returns undefined on type mismatch rather than coercing', () => {
        expect(getString(obj, 'n')).toBeUndefined();
        expect(getNumber(obj, 's')).toBeUndefined();
        expect(getBoolean(obj, 'n')).toBeUndefined();
        expect(getRecord(obj, 'arr')).toBeUndefined();
        expect(getArray(obj, 'nested')).toBeUndefined();
    });

    it('treats NaN as absent, so callers never propagate NaN into arithmetic', () => {
        expect(getNumber(obj, 'nan')).toBeUndefined();
    });

    it('returns undefined for non-records instead of throwing', () => {
        expect(getString(null, 's')).toBeUndefined();
        expect(getNumber(undefined, 'n')).toBeUndefined();
        expect(getBoolean('str', 'b')).toBeUndefined();
    });

    it('distinguishes a present false/0 from an absent key', () => {
        expect(getBoolean({ b: false }, 'b')).toBe(false);
        expect(getNumber({ n: 0 }, 'n')).toBe(0);
    });
});

describe('errorMessage', () => {
    it('prefers a thrown string', () => {
        expect(errorMessage('boom')).toBe('boom');
    });

    it('reads Error.message', () => {
        expect(errorMessage(new Error('failed'))).toBe('failed');
    });

    it('falls back through message -> detail -> error', () => {
        expect(errorMessage({ message: 'm' })).toBe('m');
        expect(errorMessage({ detail: 'd' })).toBe('d');
        expect(errorMessage({ error: 'e' })).toBe('e');
    });

    it('returns the fallback rather than "undefined" for unusable values', () => {
        expect(errorMessage(null)).toBe('Unknown error');
        expect(errorMessage({})).toBe('Unknown error');
        expect(errorMessage('')).toBe('Unknown error');
        expect(errorMessage(undefined, 'custom')).toBe('custom');
    });
});

describe('errorStatus', () => {
    it('reads any of the shapes the gateway and fetch produce', () => {
        expect(errorStatus({ status: 401 })).toBe(401);
        expect(errorStatus({ code: 403 })).toBe(403);
        expect(errorStatus({ statusCode: 500 })).toBe(500);
    });

    it('prefers status over code when both are present', () => {
        expect(errorStatus({ status: 401, code: 999 })).toBe(401);
    });

    it('ignores non-numeric codes, which the proxy also uses for strings', () => {
        expect(errorStatus({ code: 'TOKEN_EXPIRED' })).toBeUndefined();
        expect(errorStatus(null)).toBeUndefined();
    });
});

describe('parseJson', () => {
    it('parses valid JSON', () => {
        expect(parseJson('{"a":1}')).toEqual({ a: 1 });
    });

    it('returns undefined for invalid JSON instead of throwing', () => {
        expect(parseJson('not json')).toBeUndefined();
        expect(parseJson('')).toBeUndefined();
    });
});

describe('errorKind', () => {
    class RefusedError extends Error {
        constructor(message: string, public status: number, public details?: unknown) {
            super(message);
            this.name = 'RefusedError';
        }
    }

    it('is the class name and the numeric status of an error that carries one', () => {
        expect(errorKind(new RefusedError('token sk-live-123 is not valid', 403))).toBe('RefusedError 403');
    });

    it('is the class name alone where there is no numeric status', () => {
        expect(errorKind(new TypeError('Failed to fetch'))).toBe('TypeError');
        expect(errorKind(new Error('boom'))).toBe('Error');
    });

    it('reads the status from status, code or statusCode, and only a number', () => {
        expect(errorKind({ code: -32601, message: 'no such method' })).toBe('object -32601');
        expect(errorKind({ statusCode: 502 })).toBe('object 502');
        expect(errorKind({ code: 'E_REFUSED' })).toBe('object');
    });

    it('is the type of a value that is not an error', () => {
        expect(errorKind('the answer of the server')).toBe('string');
        expect(errorKind(undefined)).toBe('undefined');
        expect(errorKind(null)).toBe('object');
    });

    it('never holds the message, the details or any other field', () => {
        const kind = errorKind(new RefusedError('password hunter2 refused', 422, { detail: 'hunter2', token: 'abc' }));
        expect(kind).toBe('RefusedError 422');
        expect(kind).not.toContain('hunter2');
        expect(kind).not.toContain('abc');
    });
});

describe('failureSentence', () => {
    it('is what failed, then what the other side said', () => {
        expect(failureSentence('Failed to load the models', new Error('The gateway is not reachable'))).toBe(
            'Failed to load the models: The gateway is not reachable',
        );
        expect(failureSentence('Save failed', { detail: 'name: already taken' })).toBe('Save failed: name: already taken');
    });

    it('is what failed alone where the failure carries no sentence', () => {
        expect(failureSentence('Failed to load the models', undefined)).toBe('Failed to load the models');
        expect(failureSentence('Failed to load the models', new Error(''))).toBe('Failed to load the models');
        expect(failureSentence('Failed to load the models', { status: 500 })).toBe('Failed to load the models');
    });
});

describe('groupedReasons', () => {
    it('tells each reason once with its count, the most frequent first', () => {
        expect(groupedReasons([new Error('b'), new Error('a'), new Error('b')])).toBe('2× b; 1× a');
    });

    it('names no more kinds than its limit, and says how many it left out', () => {
        const failures = ['a', 'a', 'b', 'c', 'd', 'e'].map((m) => new Error(m));
        expect(groupedReasons(failures, 2)).toBe('2× a; 1× b; and 3 other reasons');
        expect(groupedReasons(failures, 4)).toBe('2× a; 1× b; 1× c; 1× d; and 1 other reason');
    });

    it('a failure that says nothing is counted under "no reason given"', () => {
        expect(groupedReasons([{}, new Error('a'), null])).toBe('2× no reason given; 1× a');
    });

    it('is empty for no failure', () => {
        expect(groupedReasons([])).toBe('');
    });
});
