/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the tests of the identity provider form share (the Community ones and
 * those of the Enterprise composition under `enterprise/src/host/`): the
 * gateway double, the payloads and the stored rows, and the ways to fill the
 * form in.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file mocks `useGateway` itself and takes the module from here.
 * This module does not import the form, so that the mock factory can await it.
 */

import { expect, vi } from 'vitest';
import { fireEvent, screen, type render } from '@testing-library/react';

import { GatewayAPIError } from '../../lib/gatewayApi/base';
import type { IdPConfig } from '../../lib/types';

export const createIdPConfig = vi.fn().mockResolvedValue({});
export const updateIdPConfig = vi.fn().mockResolvedValue({});

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => ({ createIdPConfig, updateIdPConfig }) });

/** The error the API client throws for an answer of `status` with `body`. */
export const refusedByServer = (status: number, body: unknown) => new GatewayAPIError(`HTTP ${status}`, status, undefined, body);

/** What the form sends for the fields no test touched. */
export const CORE_DEFAULTS = {
    type: 'oidc_discoverable',
    well_known_url: '',
    authorization_endpoint: '',
    token_endpoint: '',
    jwks_uri: '',
    userinfo_endpoint: '',
    revocation_endpoint: '',
    introspection_endpoint: '',
    supports_pkce: true,
    supports_refresh_tokens: true,
    supports_client_credentials: false,
    scopes_supported: [],
    token_endpoint_auth_methods: ['client_secret_post'],
    response_types_supported: ['code'],
    grant_types_supported: ['authorization_code', 'refresh_token'],
    discovery_cache_ttl: 3600,
    enabled: true,
    verified: false,
    description: '',
    logo_url: '',
    workload_audiences: [],
};

/** A stored SAML provider, as the Control Plane lists one (its protocol, and no SAML field): the console shows it locked. */
export const SAML_ROW = {
    id: 'idp-1',
    name: 'Okta',
    issuer: 'https://acme.okta.com',
    enabled: true,
    protocol: 'saml',
    type: 'okta',
    // A stored SAML row may carry OAuth flags; the form must not show them set.
    supports_pkce: true,
    supports_refresh_tokens: true,
    supports_client_credentials: true,
    workload_audiences: ['aud-a', 'aud-b'],
} as IdPConfig;

export const OIDC_ROW = {
    id: 'idp-2',
    name: 'Google',
    issuer: 'https://accounts.google.com',
    enabled: true,
    protocol: 'oidc',
    type: 'oidc',
    authorization_endpoint: 'https://accounts.google.com/auth',
    token_endpoint: 'https://accounts.google.com/token',
    supports_pkce: true,
    supports_refresh_tokens: false,
    supports_client_credentials: true,
    scopes_supported: ['openid', 'email'],
} as IdPConfig;

export type Rendered = ReturnType<typeof render>;

export function field(r: Rendered, name: string): HTMLInputElement {
    const el = r.container.querySelector(`[name="${name}"]`);
    if (!el) throw new Error(`no field named ${name}`);
    return el as HTMLInputElement;
}
export const hasField = (r: Rendered, name: string) => r.container.querySelector(`[name="${name}"]`) !== null;
export const set = (r: Rendered, name: string, value: string) => fireEvent.change(field(r, name), { target: { value } });
export const tabLabels = (r: Rendered) =>
    Array.from(r.container.querySelectorAll('.flex.border-b.flex-shrink-0 button')).map((b) => b.textContent);
export const openTab = (label: string) => fireEvent.click(screen.getByRole('button', { name: label }));
export const save = (label = 'Create Provider') => fireEvent.click(screen.getByRole('button', { name: label }));

export function sent(mock: typeof createIdPConfig, argument = 0): Record<string, unknown> {
    const calls = mock.mock.calls as unknown[][];
    return calls[calls.length - 1][argument] as Record<string, unknown>;
}

/** Fill what an OIDC provider needs to be saved. */
export function fillOidc(r: Rendered) {
    set(r, 'name', 'Google');
    set(r, 'issuer', 'https://accounts.google.com');
    openTab('Endpoints');
    set(r, 'authorization_endpoint', 'https://accounts.google.com/auth');
    set(r, 'token_endpoint', 'https://accounts.google.com/token');
}

/** Wait long enough for a save to have gone out, had it been allowed. */
export async function expectNotSaved() {
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(createIdPConfig).not.toHaveBeenCalled();
    expect(updateIdPConfig).not.toHaveBeenCalled();
}

/** How every case starts: nothing saved yet, the console quiet. For a `beforeEach`. */
export function resetIdPServerForm(): void {
    createIdPConfig.mockReset().mockResolvedValue({});
    updateIdPConfig.mockReset().mockResolvedValue({});
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
}
