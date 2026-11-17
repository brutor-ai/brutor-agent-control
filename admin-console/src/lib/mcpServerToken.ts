/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The static token of an MCP server, as the console handles it.
 *
 * The gateway sends the token to the server as a bearer. The Control Plane
 * stores it encrypted and never returns it: a server answers
 * `has_auth_token`, whether one is stored. What an update's `auth_token`
 * means to the Control Plane:
 *
 *   not named                  the stored token is kept
 *   an empty string, or null   the stored token is removed
 *   anything else              it replaces the stored token
 *
 * The gateway sends a stored token whatever the server's authentication
 * type says. A server saved without bearer authentication therefore keeps
 * none: the save removes it.
 */

export type ServerAuthType = 'none' | 'bearer' | 'oauth';

/** The `auth_token` of an update: undefined is "not named". */
export function serverTokenForUpdate(form: {
    authType: ServerAuthType;
    typed: string;
    removeStored: boolean;
    stored: boolean;
}): string | undefined {
    if (form.authType !== 'bearer') return form.stored ? '' : undefined;
    const typed = form.typed.trim();
    if (typed) return typed;
    return form.removeStored ? '' : undefined;
}

/** The `auth_token` of a new server: the typed one, for a server that uses one. */
export function serverTokenForCreate(form: { authType: ServerAuthType; typed: string }): string | undefined {
    if (form.authType !== 'bearer') return undefined;
    return form.typed.trim() || undefined;
}
