/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether an MCP server is paired with an identity provider, in words.
 *
 * The gateway files an OAuth credential under a server only when the
 * identity provider that issued it is configured for that server: the
 * server's own identity provider (`idp_id` of the server), or the identity
 * provider of an active OAuth client attached to the server in a server
 * configuration (`oauth_client_id` of the configuration). A sign-in from
 * any other identity provider is refused.
 *
 * This states what is configured. The console refuses nothing on it: the
 * gateway does, when a sign-in arrives.
 *
 * Two readings are here. `oauthPairingOf` is the Control Plane's own
 * statement about a stored server, shown wherever a stored server is (the
 * server list, the server form, after a server is created).
 * `serverIdpPairing` is the console's reading of a choice that is not saved
 * yet (the OAuth client being chosen in a server configuration form).
 */

import { getArray, getBoolean, getRecord, getString } from './narrow';

/**
 * What the Control Plane states of a stored OAuth-protected server
 * (`oauth_pairing` of a server it answers with): whether the gateway
 * exchanges an authorization code for it, the identity providers it accepts
 * one for, and the Control Plane's own words when there is none.
 */
export interface StoredOAuthPairing {
    paired: boolean;
    idpIds: string[];
    /** The Control Plane's sentence when no identity provider is paired; null when one is. */
    warning: string | null;
}

/**
 * The pairing a server answer states; null for a server that is not
 * OAuth-protected and for an answer that does not state it (a server that
 * is not saved yet).
 */
export function oauthPairingOf(server: unknown): StoredOAuthPairing | null {
    const stated = getRecord(server, 'oauth_pairing');
    const paired = getBoolean(stated, 'paired');
    if (!stated || paired === undefined) return null;
    return {
        paired,
        idpIds: (getArray(stated, 'idp_ids') ?? []).filter((id): id is string => typeof id === 'string'),
        warning: getString(stated, 'warning') ?? null,
    };
}

/** The paired identity providers in words: "A", "A and B", "A, B and C". An identity provider without a name is its id. */
export function pairedIdpsInWords(idpIds: readonly string[], idpName: (id: string) => string | undefined): string {
    const names = idpIds.map((id) => idpName(id) ?? id);
    if (names.length <= 1) return names.join('');
    return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

export type PairingState = 'paired' | 'paired_with_two' | 'not_paired';

export interface ServerIdpPairing {
    state: PairingState;
    text: string;
}

export interface PairingInput {
    /** The identity provider set on the MCP server. */
    serverIdpId?: string | null;
    /** The OAuth client of the server configuration, when one is chosen. */
    client?: { name: string; idp_id?: string | null; is_active?: boolean | null } | null;
}

const ACCEPTS = 'The gateway accepts OAuth sign-ins for this server from';
const REFUSES = 'The gateway refuses OAuth sign-ins for this server.';

/** `idpName` gives the name of an identity provider, or nothing for one it does not know. */
export function serverIdpPairing({ serverIdpId, client }: PairingInput, idpName: (id: string) => string | undefined): ServerIdpPairing {
    const named = (id: string) => idpName(id) ?? id;
    const viaClient = client && client.is_active !== false && client.idp_id ? client.idp_id : null;

    if (serverIdpId && viaClient && viaClient !== serverIdpId) {
        return {
            state: 'paired_with_two',
            text: `Paired with two identity providers: ${named(serverIdpId)}, set on the MCP server, and ${named(viaClient)}, through the OAuth client ${client?.name ?? ''}. ${ACCEPTS} both.`,
        };
    }
    if (serverIdpId) {
        const also = viaClient ? `, and of the OAuth client ${client?.name ?? ''}` : '';
        return {
            state: 'paired',
            text: `Paired with identity provider ${named(serverIdpId)}: it is the identity provider set on the MCP server${also}. ${ACCEPTS} it.`,
        };
    }
    if (viaClient) {
        return {
            state: 'paired',
            text: `Paired with identity provider ${named(viaClient)} through the OAuth client ${client?.name ?? ''}. The MCP server names no identity provider of its own. ${ACCEPTS} it.`,
        };
    }
    const why = !client
        ? 'the MCP server names none and no OAuth client is chosen'
        : client.is_active === false
          ? `the MCP server names none, and the OAuth client ${client.name} is not active`
          : `the MCP server names none, and the OAuth client ${client.name} has no identity provider`;
    return { state: 'not_paired', text: `Not paired with an identity provider: ${why}. ${REFUSES}` };
}
