/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The password rule the forms apply before a password is sent: the Control
 * Plane's own (at least 8 characters, not blank), so a form does not send
 * one that is refused.
 */

import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { documentForTest } from '../../../scripts/control-plane-documents.mjs';
import { getArray, getNumber, getRecord, parseJson } from '../narrow';

import { MIN_PASSWORD_LENGTH, PASSWORD_RULE, passwordMeetsRule } from '../passwordRule';

describe('passwordMeetsRule', () => {
    it('takes a password of eight characters or more', () => {
        expect(MIN_PASSWORD_LENGTH).toBe(8);
        expect(passwordMeetsRule('correct-horse')).toBe(true);
        expect(passwordMeetsRule('12345678')).toBe(true);
        expect(passwordMeetsRule(' a      ')).toBe(true);
    });

    it('refuses a password shorter than eight characters', () => {
        expect(passwordMeetsRule('')).toBe(false);
        expect(passwordMeetsRule('short12')).toBe(false);
    });

    it('refuses a blank password, however long: only spaces, tabs or line ends', () => {
        expect(passwordMeetsRule('        ')).toBe(false);
        expect(passwordMeetsRule('            ')).toBe(false);
        expect(passwordMeetsRule('\t\t\t\t\t\t\t\t')).toBe(false);
        expect(passwordMeetsRule(' \n \t    ')).toBe(false);
    });

    it('counts characters, not the units a string is stored in', () => {
        // Seven characters, fourteen UTF-16 units.
        expect(passwordMeetsRule('😀😀😀😀😀😀😀')).toBe(false);
        expect(passwordMeetsRule('😀😀😀😀😀😀😀😀')).toBe(true);
    });

    it('states the rule in one sentence', () => {
        expect(PASSWORD_RULE).toBe('A password has at least 8 characters and is not blank.');
    });
});

describe('the rule is the Control Plane document\'s', () => {
    // Skipped where the Control Plane is not beside this repository (as
    // ../brutor-gateway-control or ../control-plane) and no directory of
    // documents is named.
    const document = documentForTest('community', 'the password rule is the Control Plane document\'s');
    it.skipIf(document === null)('every password a request sets has the minimum length and the not-blank pattern applied here', () => {
        const schemas = getRecord(getRecord(parseJson(readFileSync(document ?? '', 'utf8')), 'components'), 'schemas') ?? {};
        const passwordFields: Array<Record<string, unknown>> = [];
        // A sign-in sends what it is given: its password is no password being set.
        for (const [schemaName, schema] of Object.entries(schemas)) {
            if (/Login/.test(schemaName)) continue;
            for (const [name, field] of Object.entries(getRecord(schema, 'properties') ?? {})) {
                if (!/password$/.test(name) || name === 'current_password') continue;
                const variants = getArray(field, 'anyOf') ?? [field];
                for (const variant of variants) {
                    if (getNumber(variant, 'minLength') !== undefined) passwordFields.push(variant as Record<string, unknown>);
                }
            }
        }
        expect(passwordFields.length).toBeGreaterThanOrEqual(3);
        for (const field of passwordFields) {
            expect(field.minLength).toBe(MIN_PASSWORD_LENGTH);
            expect(field.pattern).toBe('\\S');
        }
    });
});
