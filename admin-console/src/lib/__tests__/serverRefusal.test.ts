/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the control plane said when it refused a request (`serverRefusal.ts`).
 *
 * A refusal with a reason is `{detail: {error, message, ...}}`. It reaches a
 * page as the API client's error (the body under `details`), and the helper
 * also reads the body itself and the bare detail, so a page shows the
 * server's sentence whichever of the three it was handed.
 */

import { describe, expect, it } from 'vitest';

import { GatewayAPIError } from '../gatewayApiClient';
import { refusalMessage, serverRefusal } from '../serverRefusal';
import { CONTAINER_INSTALL_REFUSED, identityProtocolUnavailable, thrownByClient } from '../../test/serverAnswers';

const EDITION = CONTAINER_INSTALL_REFUSED.body.detail;
const IDP = identityProtocolUnavailable('ADFS', 'saml', 'deleted');

describe('serverRefusal', () => {
    it('reads the edition refusal off what the API client throws', async () => {
        const thrown = await thrownByClient(CONTAINER_INSTALL_REFUSED.status, CONTAINER_INSTALL_REFUSED.body);
        expect(thrown).toBeInstanceOf(GatewayAPIError);
        expect(serverRefusal(thrown)).toEqual({
            code: 'enterprise_edition_feature',
            feature: 'deployment',
            message: EDITION.message,
        });
        expect(refusalMessage(thrown, 'generic')).toBe(EDITION.message);
    });

    it('reads the identity protocol refusal off what the API client throws', async () => {
        const thrown = await thrownByClient(IDP.status, IDP.body);
        expect(serverRefusal(thrown)).toEqual({
            code: 'identity_protocol_unavailable',
            protocol: 'saml',
            message: IDP.body.detail.message,
        });
        expect(refusalMessage(thrown, 'generic')).toBe(
            "Identity Provider 'ADFS' uses the identity protocol 'saml', which is not available in this edition. It cannot be deleted here.",
        );
    });

    it.each([
        ['the response body', { detail: EDITION }],
        ['the bare detail', EDITION],
        ['an error carrying the body', Object.assign(new Error('HTTP 400: Bad Request'), { details: { detail: EDITION } })],
    ])('reads a structured detail from %s', (_name, value) => {
        expect(serverRefusal(value)).toEqual({
            code: 'enterprise_edition_feature',
            feature: 'deployment',
            message: EDITION.message,
        });
        expect(refusalMessage(value, 'generic')).toBe(EDITION.message);
    });

    it('prefers the detail to the message of the error that carries it', () => {
        const value = new GatewayAPIError('HTTP 400: Bad Request', 400, undefined, { detail: EDITION });
        expect(refusalMessage(value, 'generic')).toBe(EDITION.message);
    });

    it('shows a string detail as it is, and it is not a coded refusal', async () => {
        expect(serverRefusal({ detail: 'still used by 2 MCP servers' })).toBeNull();
        expect(refusalMessage({ detail: 'still used by 2 MCP servers' }, 'generic')).toBe('still used by 2 MCP servers');

        const thrown = await thrownByClient(404, { detail: 'Catalog entry not found' });
        expect(serverRefusal(thrown)).toBeNull();
        expect(refusalMessage(thrown, 'generic')).toBe('Catalog entry not found');
    });

    it('joins an array detail (a validation answer) field by field', async () => {
        const detail = [
            { loc: ['body', 'name'], msg: 'Field required', type: 'missing' },
            { loc: ['body', 'credentials', 'client_id'], msg: 'String too short', type: 'string_too_short' },
        ];
        expect(serverRefusal({ detail })).toBeNull();
        expect(refusalMessage({ detail }, 'generic')).toBe('name: Field required; credentials.client_id: String too short');

        const thrown = await thrownByClient(422, { detail });
        expect(refusalMessage(thrown, 'generic')).toBe('name: Field required; credentials.client_id: String too short');
    });

    it('falls back when an array detail has nothing readable in it', () => {
        expect(refusalMessage({ detail: [] }, 'generic')).toBe('generic');
        expect(refusalMessage({ detail: [1, null] }, 'generic')).toBe('generic');
    });

    it('never shows the code in place of a missing message', () => {
        const detail = { error: 'enterprise_edition_feature', feature: 'deployment' };
        expect(serverRefusal({ detail })).toEqual({ code: 'enterprise_edition_feature', feature: 'deployment' });
        expect(refusalMessage({ detail }, 'generic')).toBe('generic');
        expect(refusalMessage(detail, 'generic')).toBe('generic');
        expect(refusalMessage({ detail: { ...detail, message: '' } }, 'generic')).toBe('generic');
        expect(refusalMessage({ detail: { ...detail, message: 42 } }, 'generic')).toBe('generic');
        expect(
            refusalMessage(new GatewayAPIError('HTTP 400: Bad Request', 400, undefined, { detail }), 'generic'),
        ).toBe('generic');
    });

    it('reads a detail with a message and no code as a message only', () => {
        expect(serverRefusal({ detail: { message: 'No.' } })).toBeNull();
        expect(refusalMessage({ detail: { message: 'No.' } }, 'generic')).toBe('No.');
    });

    it('ignores fields of the wrong type', () => {
        expect(serverRefusal({ detail: { error: 7, message: 'm' } })).toBeNull();
        expect(serverRefusal({ detail: { error: 'code', feature: 3, protocol: null, message: 'm' } })).toEqual({
            code: 'code',
            message: 'm',
        });
    });

    it.each([
        ['undefined', undefined],
        ['null', null],
        ['a number', 409],
        ['an array', [EDITION]],
        ['an empty object', {}],
    ])('has nothing to say about %s', (_name, value) => {
        expect(serverRefusal(value)).toBeNull();
        expect(refusalMessage(value, 'generic')).toBe('generic');
    });

    it('keeps the message of an ordinary error and of a thrown string', () => {
        expect(serverRefusal(new Error('network down'))).toBeNull();
        expect(refusalMessage(new Error('network down'), 'generic')).toBe('network down');
        expect(refusalMessage('plain', 'generic')).toBe('plain');
    });
});
