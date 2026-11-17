/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What a page catches when the control plane refuses a request.
 *
 * A component test mocks the gateway hook, so the rejection it hands the page
 * is made up by the test. `thrownByClient` makes it the real one instead: the
 * real API client is given the server's answer (status and JSON body, exactly
 * as the control plane sends them) and whatever it throws is returned.
 *
 *     gateway.deleteIdPConfig.mockRejectedValue(await thrownByClient(409, { detail: {...} }));
 */

import { GatewayApiClient } from '../lib/gatewayApiClient';

/** The control plane's refusals a Community composition answers with. */
export const CONTAINER_INSTALL_REFUSED = {
    status: 400,
    body: {
        detail: {
            error: 'enterprise_edition_feature',
            feature: 'deployment',
            message:
                'Containerised install is part of the Brutor Enterprise Edition. This installation runs ' +
                'the Community Edition, which does not deploy MCP servers: import the entry without ' +
                'deploying it, run the server yourself and set its URL on the imported MCP server.',
        },
    },
} as const;

/** `action` is the server's word for what was asked: changed, deleted, tested, discovered. */
export function identityProtocolUnavailable(name: string, protocol: string, action: string) {
    return {
        status: 409,
        body: {
            detail: {
                error: 'identity_protocol_unavailable',
                protocol,
                message:
                    `Identity Provider '${name}' uses the identity protocol '${protocol}', ` +
                    `which is not available in this edition. It cannot be ${action} here.`,
            },
        },
    };
}

/** What the real API client throws when the server answers `status` with `body`. */
export async function thrownByClient(status: number, body: unknown): Promise<unknown> {
    const realFetch = globalThis.fetch;
    globalThis.fetch = () =>
        Promise.resolve(
            new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
        );
    try {
        await new GatewayApiClient('http://control-plane.test', () => {}).request('/v1/refused', { method: 'POST' });
    } catch (error) {
        return error;
    } finally {
        globalThis.fetch = realFetch;
    }
    throw new Error(`the client did not throw on HTTP ${status}`);
}
