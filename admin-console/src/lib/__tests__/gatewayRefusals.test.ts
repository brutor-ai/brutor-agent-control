/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Refusals of the gateway that reach the console as recorded text or as an
 * answer handed on by the Control Plane, in words an administrator can act
 * on. The sentences below are the gateway's own.
 */

import { describe, expect, it } from 'vitest';

import { explainGatewayRefusal, isTenantKeyUnavailable, TENANT_KEY_UNAVAILABLE_TEXT } from '../gatewayRefusals';
import { thrownByClient } from '../../test/serverAnswers';

describe('explainGatewayRefusal', () => {
    it('says the arguments of a call were too large to check, with the limits and the policy', () => {
        // A proxy log's error message for a call refused by an argument policy.
        const recorded =
            'Argument policy (payments-threshold/arguments_truncated): Refused: the arguments are too large or too deeply nested to be checked against this policy';
        expect(explainGatewayRefusal(recorded)).toBe(
            'The call was refused because its arguments are too large to check against argument policy "payments-threshold". Unless a policy sets its own limits, the gateway reads at most 512 values, 8 levels of nesting and 64 items of a list in the arguments of one call. It refuses a call beyond the limits of a JSON policy that applies to it, whatever the rules say. Nothing was sent. Send the call with smaller arguments, or raise the limits of the policy (Advanced: up to 4096 values, 32 levels and 1024 items).',
        );
    });

    it('says the argument policies could not be read, and that the call was refused rather than let through', () => {
        const recorded = 'Argument policy (argument policy unavailable/policy_unavailable): Refused: argument policies could not be loaded';
        expect(explainGatewayRefusal(recorded)).toBe(
            'The call was refused because the gateway could not read the argument policies that apply to it, and it does not let a call through unchecked. Nothing was sent. The usual cause is a stored argument policy whose rules the gateway cannot read: open the argument policies of the group, save the one that fails again or delete it. The gateway log names it.',
        );
    });

    it('says the gateway could not read the token stored for an MCP server, and what to do', () => {
        // The gateway's answer (502) to a call for an MCP server whose stored
        // token does not decrypt, as a proxy log records it.
        expect(explainGatewayRefusal('Configured auth token for this server cannot be read; re-enter it')).toBe(
            "The gateway could not read the token stored for this MCP server, so it did not call the server. Nothing was sent. Open the MCP server, enter its token again and save. The token becomes unreadable when the installation's encryption key (ENCRYPTION_KEY) changes; restoring the key also makes it readable again.",
        );
    });

    it('says a stored MCP server token is not a usable header value', () => {
        expect(explainGatewayRefusal('Configured auth token for this server is malformed; re-enter it')).toBe(
            'The token stored for this MCP server cannot be sent as it is (it holds a character a request header cannot carry), so the gateway did not call the server. Nothing was sent. Open the MCP server, enter its token again and save.',
        );
    });

    it('finds the rule wherever it is in the text, and is null for anything else', () => {
        expect(explainGatewayRefusal('{"rule":"arguments_truncated"}')).toMatch(/^The call was refused because its arguments are too large to check\./);
        expect(explainGatewayRefusal('Argument policy (payments/field_gt): amount 8000 exceeds 1000')).toBeNull();
        expect(explainGatewayRefusal('')).toBeNull();
        expect(explainGatewayRefusal(undefined)).toBeNull();
    });
});

describe('a tenant signing key the gateway cannot read', () => {
    const DETAIL =
        "this tenant's own evidence signing key cannot be used; nothing is sealed until it is rotated (POST /v1/evidence/keys/{tenant}/rotate) or ENCRYPTION_KEY is restored";

    it('is recognised by its code', async () => {
        expect(isTenantKeyUnavailable(await thrownByClient(503, { detail: { error: 'tenant_key_unavailable', detail: DETAIL } }))).toBe(true);
    });

    it('is recognised by its code only: the same sentence under another code is another failure', async () => {
        const thrown = await thrownByClient(503, { detail: { error: 'evidence_disabled', detail: DETAIL } });
        expect(isTenantKeyUnavailable(thrown)).toBe(false);
        expect((thrown as Error).message).toBe(DETAIL);
    });

    it('is not read into another failure', async () => {
        expect(isTenantKeyUnavailable(await thrownByClient(503, { detail: { error: 'evidence_disabled', detail: 'evidence is disabled' } }))).toBe(false);
        expect(isTenantKeyUnavailable(new Error('down'))).toBe(false);
    });

    it('is said in plain words, with what to do', () => {
        expect(TENANT_KEY_UNAVAILABLE_TEXT).toBe(
            "The gateway cannot read this tenant's signing key, so it seals no evidence for the tenant. Rotate the key: the new key signs from then on, and what was not sealed is sealed afterwards. The key becomes unreadable when the installation's encryption key (ENCRYPTION_KEY) changes; restoring it also makes the key readable again.",
        );
    });

    it('is what the API client throws, whichever page asked', async () => {
        const thrown = await thrownByClient(503, { detail: { error: 'tenant_key_unavailable', detail: DETAIL } });
        expect((thrown as Error).message).toBe(TENANT_KEY_UNAVAILABLE_TEXT);
    });
});

describe('an answer the Control Plane hands on from the gateway', () => {
    it('shows the gateway\'s sentence, not the status line', async () => {
        // `{detail: {error, detail}}`: the code and the gateway's own words.
        const thrown = await thrownByClient(502, { detail: { error: 'core_error', detail: 'the log has no head yet' } });
        expect((thrown as Error).message).toBe('the log has no head yet');
    });
});
