/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the control plane said when it refused a request.
 *
 * A refusal with a reason is answered `{detail: {error, message, ...}}`:
 * `error` is a code for programs (`enterprise_edition_feature`,
 * `identity_protocol_unavailable`, `feature_disabled`, ...), `message` the
 * sentence for the person, and the rest says what the refusal is about
 * (`feature`, `protocol`). Other answers carry a string `detail`, or an array
 * of field errors (a validation answer).
 *
 * A page catches that as the API client's error, which keeps the response
 * body under `details`. The body itself and the bare detail are read the same
 * way, so a page shows the server's sentence whichever it was handed.
 *
 * `refusalMessage` is `errorMessage` (`narrow.ts`) for a failed request: use
 * it where the text shown is the server's reason. It never shows the code.
 */

import { errorMessage, getArray, getRecord, getString, isRecord } from './narrow';

/** A feature of the Enterprise Edition was asked of a composition without it. */
export const ENTERPRISE_EDITION_FEATURE = 'enterprise_edition_feature';
/** An action on a stored identity provider whose protocol this composition does not have. */
export const IDENTITY_PROTOCOL_UNAVAILABLE = 'identity_protocol_unavailable';

export interface ServerRefusal {
    /** The `error` code of the detail. */
    code: string;
    /** The sentence for the person, when the server sent one. */
    message?: string;
    /** The feature the refusal is about, when it names one. */
    feature?: string;
    /** The identity protocol the refusal is about, when it names one. */
    protocol?: string;
}

/** The `detail` of a caught value: on the error's body, on the body, or the value itself. */
function detailOf(value: unknown): unknown {
    if (!isRecord(value)) return undefined;
    const body = getRecord(value, 'details');
    if (body && 'detail' in body) return body.detail;
    if ('detail' in value) return value.detail;
    return value;
}

/** A sentence per field error of a validation answer: `field: what is wrong`. */
function fieldErrors(detail: readonly unknown[]): string | undefined {
    const lines = detail.flatMap((item) => {
        if (typeof item === 'string' && item) return [item];
        const msg = getString(item, 'msg');
        if (!msg) return [];
        // `loc` starts with where the field was looked for (`body`, `query`).
        const field = (getArray(item, 'loc') ?? []).slice(1).join('.');
        return [`${field || 'unknown'}: ${msg}`];
    });
    return lines.length > 0 ? lines.join('; ') : undefined;
}

/** The coded refusal a caught value carries, or `null` when it carries none. */
export function serverRefusal(value: unknown): ServerRefusal | null {
    const detail = detailOf(value);
    const code = getString(detail, 'error');
    if (!code) return null;
    const refusal: ServerRefusal = { code };
    const message = getString(detail, 'message');
    const feature = getString(detail, 'feature');
    const protocol = getString(detail, 'protocol');
    if (message) refusal.message = message;
    if (feature) refusal.feature = feature;
    if (protocol) refusal.protocol = protocol;
    return refusal;
}

/**
 * The text to show for a failed request: the server's sentence when it sent
 * one, else what `errorMessage` makes of the value, else `fallback`. A coded
 * refusal without a sentence gives `fallback`, not the code.
 */
export function refusalMessage(value: unknown, fallback: string): string {
    const detail = detailOf(value);
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) return fieldErrors(detail as unknown[]) ?? fallback;
    const message = getString(detail, 'message');
    if (message) return message;
    if (getString(detail, 'error')) return fallback;
    return errorMessage(value, fallback);
}
