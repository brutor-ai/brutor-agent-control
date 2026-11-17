/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What of a 422 answer is shown back to the user.
 *
 * The control plane never repeats a secret in a validation error: where the
 * submitted value was one, `detail[].input` is the string `[redacted]` (and so
 * is the value of a secret-bearing key inside an `input` that is an object).
 * That marker is the server's, not something the user typed, so it is left
 * out of what the console shows; everything else is shown as it came.
 */

import { describe, it, expect } from 'vitest';

import { REDACTED_INPUT, saveFailure, validationErrorsOf, withoutRedactedInput } from '../validationErrors';
import { thrownByClient } from '../../test/serverAnswers';

describe('withoutRedactedInput', () => {
    it('leaves out an input the server redacted and keeps the rest of the error', () => {
        const body = {
            detail: [
                { type: 'string_too_short', loc: ['body', 'new_password'], msg: 'String should have at least 8 characters', input: REDACTED_INPUT },
            ],
        };
        expect(withoutRedactedInput(body)).toEqual({
            detail: [{ type: 'string_too_short', loc: ['body', 'new_password'], msg: 'String should have at least 8 characters' }],
        });
    });

    it('keeps an input that is the user\'s own value', () => {
        const body = { detail: [{ loc: ['body', 'name'], msg: 'String should match pattern', input: 'two words' }] };
        expect(withoutRedactedInput(body)).toEqual(body);
    });

    it('leaves out redacted values inside an input that is an object or a list, at any depth', () => {
        const body = {
            detail: [
                {
                    loc: ['body', 'username'],
                    msg: 'Field required',
                    input: { password: REDACTED_INPUT, tenant_id: 'acme', nested: { api_key: REDACTED_INPUT, region: 'eu' }, list: ['a', REDACTED_INPUT] },
                },
            ],
        };
        expect(withoutRedactedInput(body)).toEqual({
            detail: [
                { loc: ['body', 'username'], msg: 'Field required', input: { tenant_id: 'acme', nested: { region: 'eu' }, list: ['a'] } },
            ],
        });
    });

    it('does not change the object it was given', () => {
        const body = { detail: [{ loc: ['body', 'password'], msg: 'too short', input: REDACTED_INPUT }] };
        withoutRedactedInput(body);
        expect(body.detail[0].input).toBe(REDACTED_INPUT);
    });

    it.each([
        ['a string detail', { detail: 'Not found' }],
        ['an object detail', { detail: { code: 'name_conflict', message: 'exists' } }],
        ['a body with no detail', { message: 'boom' }],
        ['text', 'boom'],
        ['nothing', null],
    ])('returns %s as it came', (_name, value) => {
        expect(withoutRedactedInput(value)).toEqual(value);
    });

    it('touches only the input: the word in a message or a location stays', () => {
        const body = { detail: [{ loc: ['body', REDACTED_INPUT], msg: `value ${REDACTED_INPUT} refused` }] };
        expect(withoutRedactedInput(body)).toEqual(body);
    });
});

describe('validationErrorsOf', () => {
    it('is where and what of each error the client caught, never the input', async () => {
        const caught = await thrownByClient(422, {
            detail: [
                { type: 'string_too_long', loc: ['body', 'api_key'], msg: 'too long', input: 'typed-api-key-value', ctx: { max_length: 500 } },
                { type: 'missing', loc: ['body', 'provider_config', 'aws_region'], msg: 'Field required', input: { aws_secret_access_key: 'typed-secret' } },
            ],
        });
        const errors = validationErrorsOf(caught);
        expect(errors).toEqual([
            { loc: ['body', 'api_key'], msg: 'too long' },
            { loc: ['body', 'provider_config', 'aws_region'], msg: 'Field required' },
        ]);
        expect(JSON.stringify(errors)).not.toMatch(/typed|input/);
    });

    it('is undefined for an answer that is not a list of validation errors', async () => {
        expect(validationErrorsOf(await thrownByClient(400, { detail: 'Name already taken' }))).toBeUndefined();
        expect(validationErrorsOf(new Error('down'))).toBeUndefined();
        expect(validationErrorsOf(undefined)).toBeUndefined();
    });

    it('does not read an axios-shaped error: the client throws none', () => {
        const axiosShaped = Object.assign(new Error('Request failed'), {
            response: { data: { detail: [{ loc: ['body', 'name'], msg: 'too short' }] } },
        });
        expect(validationErrorsOf(axiosShaped)).toBeUndefined();
        expect(saveFailure(axiosShaped, 'Failed to save').message).toBe('Request failed');
    });
});

describe('saveFailure', () => {
    it('is one line per validation error, and carries them for the form', async () => {
        const failure = saveFailure(
            await thrownByClient(422, {
                detail: [
                    { loc: ['body', 'name'], msg: 'too short', input: 'typed-name' },
                    { msg: 'bad' },
                ],
            }),
            'Failed to create model',
        );
        expect(failure.message).toBe('name: too short\nfield: bad');
        expect(failure.validationErrors).toEqual([{ loc: ['body', 'name'], msg: 'too short' }, { loc: [], msg: 'bad' }]);
    });

    it('is the server detail, the thrown message, or the fallback', async () => {
        expect(saveFailure(await thrownByClient(400, { detail: 'Name already taken' }), 'Failed').message).toBe('Name already taken');
        expect(saveFailure(new Error('network down'), 'Failed').message).toBe('network down');
        expect(saveFailure(undefined, 'Failed to create model').message).toBe('Failed to create model');
        expect(saveFailure(new Error('network down'), 'Failed').validationErrors).toBeUndefined();
    });

    it('carries the status the control plane answered with, and none when there was no answer', async () => {
        expect(saveFailure(await thrownByClient(422, { detail: 'api_url is required' }), 'Failed').status).toBe(422);
        expect(saveFailure(await thrownByClient(422, { detail: [{ loc: ['body', 'name'], msg: 'x' }] }), 'Failed').status).toBe(422);
        expect(saveFailure(await thrownByClient(400, { detail: 'Name already taken' }), 'Failed').status).toBe(400);
        expect(saveFailure(new Error('network down'), 'Failed').status).toBeUndefined();
    });
});
