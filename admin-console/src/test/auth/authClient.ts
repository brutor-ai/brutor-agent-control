/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The gateway API client double of the `AuthProvider` tests, and the session
 * they find in storage. Shared by `hooks/__tests__/useAuth.test.tsx` and its
 * Enterprise half under `enterprise/src/host/` (the system administrator's
 * principal is the Enterprise extension's).
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file mocks `lib/gatewayApiClient` itself and takes the module
 * from here. This module does not import the provider, so that the mock
 * factory can await it; what renders the provider is in `permissionProbe`.
 */

import { vi } from 'vitest';

class TokenExpiredError extends Error {
    constructor(message = 'token expired') {
        super(message);
        this.name = 'TokenExpiredError';
    }
}

export const mocks = {
    TokenExpiredError,
    validateToken: vi.fn(),
    setAuthToken: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
};

/** `lib/gatewayApiClient`. */
export const gatewayApiClientModule = () => ({
    TOKEN_EXPIRED_EVENT: 'gateway_token_expired',
    TokenExpiredError: mocks.TokenExpiredError,
    GatewayApiClient: class {
        setAuthToken = mocks.setAuthToken;
        validateToken = mocks.validateToken;
        login = mocks.login;
        logout = mocks.logout;
    },
});

/**
 * The session a sign-in left in storage: a tenant administrator's of tenant
 * `acme`, or one of another kind (`adminType`), which is of no tenant.
 */
export function seedStoredSession(adminType = 'tenant') {
    localStorage.setItem('gateway_auth_token', 'stored-jwt');
    localStorage.setItem('gateway_user', JSON.stringify({ username: 'real_admin' }));
    localStorage.setItem('gateway_admin_type', adminType);
    if (adminType === 'tenant') localStorage.setItem('gateway_tenant_id', 'acme');
}
