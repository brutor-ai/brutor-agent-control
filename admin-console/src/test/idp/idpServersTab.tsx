/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the identity providers list test share: the gateway
 * and toast doubles, the stub of the provider form, and the two providers the
 * list starts with. The list's own cases are Community tests
 * (`components/idp/__tests__/IdPServersTab.test.tsx`); the Enterprise package
 * runs the same cases on its composition (`enterprise/src/host/`).
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the list, so that a mock factory can await it.
 */

import { vi } from 'vitest';
import { screen } from '@testing-library/react';

// The same functions on every render.
export const gateway = {
    fetchIdPConfigs: vi.fn(),
    fetchIdPConfig: vi.fn(),
    deleteIdPConfig: vi.fn(),
    discoverIdPEndpoints: vi.fn(),
    testExistingIdP: vi.fn(),
    createIdPConfig: vi.fn(),
    updateIdPConfig: vi.fn(),
};
export const toast = { success: vi.fn(), error: vi.fn() };

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => gateway });
/** `components/common/Toast`. */
export const toastModule = () => ({ useToast: () => toast });
/** `components/idp/IdPServerForm`: says how it was opened, and has a save and a cancel button. */
export const formModule = () => ({
    default: (p: { idp: { id: string; secret_marker?: string } | null; isEdit: boolean; onSave: () => void; onCancel: () => void }) => (
        <div data-testid="form">
            <span data-testid="form-mode">{p.isEdit ? 'edit' : 'create'}</span>
            <span data-testid="form-idp">{p.idp ? (p.idp.secret_marker ?? p.idp.id) : 'none'}</span>
            <button onClick={p.onSave}>form-save</button>
            <button onClick={p.onCancel}>form-cancel</button>
        </div>
    ),
});

export const OKTA = {
    id: 'okta',
    name: 'Okta',
    issuer: 'https://okta.test/',
    authorization_endpoint: 'https://okta.test/authorize',
    token_endpoint: 'https://other.test/token',
    userinfo_endpoint: 'https://okta.test/userinfo',
    jwks_uri: 'https://okta.test/jwks',
    type: 'azure_ad',
    protocol: 'oidc',
    enabled: true,
    supports_pkce: true,
};
export const ADFS = {
    id: 'adfs',
    name: 'ADFS',
    protocol: 'saml',
    enabled: false,
};
export const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

/** How every case of the list starts: Okta (OIDC) and ADFS (SAML) stored. For a `beforeEach`. */
export function resetIdPServersTab(): void {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    for (const fn of Object.values(gateway)) fn.mockReset();
    gateway.fetchIdPConfigs.mockResolvedValue({ idps: { okta: OKTA, adfs: ADFS } });
    gateway.fetchIdPConfig.mockResolvedValue({ idp: { ...OKTA, secret_marker: 'full-record' } });
}
