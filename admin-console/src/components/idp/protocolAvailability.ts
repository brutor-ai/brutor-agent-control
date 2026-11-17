/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether this console has the protocol of a stored identity provider.
 *
 * The console has one protocol, OIDC. A stored provider of another protocol
 * (SAML, which is not in this release) is shown, and that is all: the
 * Control Plane refuses (409 `identity_protocol_unavailable`) to change,
 * delete, test or discover it. The form and the list ask here, so they
 * agree on which providers those are.
 *
 * A provider with no protocol is an OIDC one to the console.
 */

/** The protocol of `idp` when this console does not have it, else `null`. */
export function unavailableProtocol(idp: { protocol?: string | null } | null | undefined): string | null {
    const stored = idp?.protocol;
    return stored && stored !== 'oidc' ? stored : null;
}

/** Why nothing can be done with a provider of `protocol` here. */
export function unavailableProtocolReason(protocol: string): string {
    return `This identity provider uses the protocol ${protocol}, which is not available in this edition.`;
}
