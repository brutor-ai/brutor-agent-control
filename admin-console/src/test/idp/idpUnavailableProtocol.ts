/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of `IdPServersTab.unavailableProtocol.test` share: three
 * stored providers (OIDC, SAML and one of a protocol nothing contributes) and
 * the ways to reach a row's actions. The doubles are the list test's
 * (`idpServersTab`). The Community half is the list with no protocol
 * contributed; the half under `enterprise/src/host/` has SAML contributed.
 */

import { vi } from 'vitest';
import { screen, within } from '@testing-library/react';

import { gateway } from './idpServersTab';

export const OKTA = {
    id: 'okta',
    name: 'Okta',
    issuer: 'https://okta.test/',
    authorization_endpoint: 'https://okta.test/authorize',
    token_endpoint: 'https://okta.test/token',
    jwks_uri: 'https://okta.test/jwks',
    type: 'oidc_discoverable',
    protocol: 'oidc',
    enabled: true,
};
export const ADFS = {
    id: 'adfs',
    name: 'ADFS',
    issuer: 'https://adfs.test/',
    type: 'saml_generic',
    protocol: 'saml',
    enabled: true,
};
export const DIRECTORY = { id: 'dir', name: 'Directory', issuer: 'ldap://dir.test', protocol: 'ldap', enabled: true };
/** The provider's row in the table (a dialog may name the provider too). */
export const rowOf = (name: string) => within(screen.getByRole('table')).getByText(name).closest('tr') as HTMLElement;
export const action = (name: string, title: RegExp | string) => within(rowOf(name)).getByTitle(title);
/** How every case starts: the three providers stored, each answered by id. For a `beforeEach`. */
export function resetUnavailableProtocol(): void {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'log').mockImplementation(() => {});
    for (const fn of Object.values(gateway)) fn.mockReset();
    gateway.fetchIdPConfigs.mockResolvedValue({ idps: { okta: OKTA, adfs: ADFS, dir: DIRECTORY } });
    gateway.fetchIdPConfig.mockImplementation((id: string) =>
        Promise.resolve({ idp: [OKTA, ADFS, DIRECTORY].find((idp) => idp.id === id) }),
    );
}
