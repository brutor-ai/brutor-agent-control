/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The control plane's validation answers (422), as shown to the user.
 *
 * Each error of a 422 carries the `input` that failed. The control plane
 * never repeats a secret there: an input that was one, and the value of every
 * secret-bearing key inside an input that is an object or a list, is the
 * marker below. The marker is the server's word, not something the user
 * typed, so it is left out of what the console prints.
 */

import { errorMessage, getArray, getNumber, getRecord, getString, isRecord } from './narrow';

/** What the control plane puts in place of a submitted secret. */
export const REDACTED_INPUT = '[redacted]';

/** `input` without the values the server redacted; `undefined` when the whole input was one. */
function shownInput(input: unknown): unknown {
    if (input === REDACTED_INPUT) return undefined;
    if (Array.isArray(input)) {
        return input.filter((item) => item !== REDACTED_INPUT).map(shownInput);
    }
    if (isRecord(input)) {
        return Object.fromEntries(
            Object.entries(input)
                .filter(([, value]) => value !== REDACTED_INPUT)
                .map(([key, value]) => [key, shownInput(value)])
        );
    }
    return input;
}

/**
 * An error answer for display: the same value, except that in a 422's
 * `detail` list no `input` shows the server's redaction marker. Anything that
 * is not such an answer is returned as it came. The argument is not changed.
 */
export function withoutRedactedInput(answer: unknown): unknown {
    const errors = getArray(answer, 'detail');
    if (!isRecord(answer) || !errors) return answer;
    return {
        ...answer,
        detail: errors.map((error) => {
            if (!isRecord(error) || !('input' in error)) return error;
            const { input, ...rest } = error;
            const shown = shownInput(input);
            return shown === undefined ? rest : { ...rest, input: shown };
        }),
    };
}

/** One error of a 422, as a form shows it: where and what. Never the `input` that failed. */
export interface FieldValidationError {
    loc: Array<string | number>;
    msg: string;
}

/**
 * The validation errors of a refusal the API client threw: `detail` of the
 * answer it carries (`GatewayAPIError.details`), reduced to where and what.
 * `undefined` for anything that is not a 422's list. The client is `fetch`
 * based: its error has no axios `response`, and none is read here.
 */
export function validationErrorsOf(error: unknown): FieldValidationError[] | undefined {
    const detail = getArray(getRecord(error, 'details'), 'detail');
    if (!detail) return undefined;
    return detail.filter(isRecord).map((entry) => ({
        loc: (getArray(entry, 'loc') ?? []).filter(
            (part): part is string | number => typeof part === 'string' || typeof part === 'number',
        ),
        msg: getString(entry, 'msg') ?? 'invalid',
    }));
}

/**
 * What a list hands back to the form whose save was refused: an `Error` with
 * one line per validation error (`field: message`, the field without the
 * leading `body`), which also carries those errors; otherwise the server's
 * message, the thrown message, or `fallback`. Either way it carries the
 * `status` the control plane answered with, when it answered: a form tells a
 * refusal of what was typed (422) from anything else by it.
 */
export function saveFailure(
    error: unknown,
    fallback: string,
): Error & { validationErrors?: FieldValidationError[]; status?: number } {
    const status = getNumber(error, 'status');
    const answered = status === undefined ? {} : { status };
    const errors = validationErrorsOf(error);
    if (!errors || errors.length === 0) return Object.assign(new Error(errorMessage(error, fallback)), answered);
    const lines = errors.map((e) => `${e.loc.slice(1).join('.') || 'field'}: ${e.msg}`);
    return Object.assign(new Error(lines.join('\n')), { validationErrors: errors }, answered);
}
