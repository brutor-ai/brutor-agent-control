/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether an MCP server is paired with an identity provider.
 *
 * The gateway files an OAuth credential under a server only when the
 * identity provider that issued it is configured for that server: the
 * server's own identity provider (`idp_id`), or the identity provider of an
 * active OAuth client attached to the server in a server configuration
 * (`oauth_client_id`). It refuses a sign-in from any other. The console
 * shows which of the two holds; it refuses nothing itself.
 */

import { describe, expect, it } from 'vitest';

import { oauthPairingOf, serverIdpPairing } from '../oauthPairing';

const idpName = (id: string) => ({ 'idp-a': 'Acme SSO', 'idp-b': 'Globex SSO' })[id];

describe('serverIdpPairing', () => {
    it('is paired through the server\'s own identity provider', () => {
        expect(serverIdpPairing({ serverIdpId: 'idp-a' }, idpName)).toEqual({
            state: 'paired',
            text: 'Paired with identity provider Acme SSO: it is the identity provider set on the MCP server. The gateway accepts OAuth sign-ins for this server from it.',
        });
    });

    it('is paired through an active OAuth client of the same identity provider, and says both ways hold', () => {
        const pairing = serverIdpPairing({ serverIdpId: 'idp-a', client: { name: 'Web app', idp_id: 'idp-a', is_active: true } }, idpName);
        expect(pairing.state).toBe('paired');
        expect(pairing.text).toBe(
            'Paired with identity provider Acme SSO: it is the identity provider set on the MCP server, and of the OAuth client Web app. The gateway accepts OAuth sign-ins for this server from it.',
        );
    });

    it('is paired through the OAuth client alone when the server names no identity provider', () => {
        expect(serverIdpPairing({ client: { name: 'Web app', idp_id: 'idp-b', is_active: true } }, idpName)).toEqual({
            state: 'paired',
            text: 'Paired with identity provider Globex SSO through the OAuth client Web app. The MCP server names no identity provider of its own. The gateway accepts OAuth sign-ins for this server from it.',
        });
    });

    it('says two identity providers are paired when the client is of another than the server\'s', () => {
        const pairing = serverIdpPairing({ serverIdpId: 'idp-a', client: { name: 'Web app', idp_id: 'idp-b', is_active: true } }, idpName);
        expect(pairing.state).toBe('paired_with_two');
        expect(pairing.text).toBe(
            'Paired with two identity providers: Acme SSO, set on the MCP server, and Globex SSO, through the OAuth client Web app. The gateway accepts OAuth sign-ins for this server from both.',
        );
    });

    it('does not count an OAuth client that is not active', () => {
        expect(serverIdpPairing({ client: { name: 'Web app', idp_id: 'idp-b', is_active: false } }, idpName)).toEqual({
            state: 'not_paired',
            text: 'Not paired with an identity provider: the MCP server names none, and the OAuth client Web app is not active. The gateway refuses OAuth sign-ins for this server.',
        });
    });

    it('is not paired when there is neither', () => {
        expect(serverIdpPairing({}, idpName)).toEqual({
            state: 'not_paired',
            text: 'Not paired with an identity provider: the MCP server names none and no OAuth client is chosen. The gateway refuses OAuth sign-ins for this server.',
        });
    });

    it('names an identity provider it has no name for by its id', () => {
        expect(serverIdpPairing({ serverIdpId: 'idp-x' }, idpName).text).toMatch(/^Paired with identity provider idp-x:/);
    });
});

describe('oauthPairingOf', () => {
    // What the Control Plane states of a stored server (`oauth_pairing`): the
    // identity providers the gateway accepts a code exchange for, and what
    // to tell the administrator when there is none.
    const WARNING =
        'No identity provider is paired with this OAuth-protected server: the gateway refuses the sign-in (403 "Identity Provider is not configured for this MCP server") until the server names an identity provider, or a server configuration of it attaches an active OAuth client.';

    it('reads a server with no identity provider paired, with the Control Plane\'s warning', () => {
        expect(oauthPairingOf({ auth_type: 'oauth', oauth_pairing: { paired: false, idp_ids: [], warning: WARNING } })).toEqual({
            paired: false,
            idpIds: [],
            warning: WARNING,
        });
    });

    it('reads a paired server, with its identity providers and no warning', () => {
        expect(oauthPairingOf({ oauth_pairing: { paired: true, idp_ids: ['idp-a', 'idp-b'], warning: null } })).toEqual({
            paired: true,
            idpIds: ['idp-a', 'idp-b'],
            warning: null,
        });
    });

    it('is null for a server that is not OAuth-protected, and for an answer that does not state it', () => {
        expect(oauthPairingOf({ auth_type: 'bearer', oauth_pairing: null })).toBeNull();
        expect(oauthPairingOf({ auth_type: 'oauth' })).toBeNull();
        expect(oauthPairingOf(null)).toBeNull();
        expect(oauthPairingOf({ oauth_pairing: 'yes' })).toBeNull();
    });
});
