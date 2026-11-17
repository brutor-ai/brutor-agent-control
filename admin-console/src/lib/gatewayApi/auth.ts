/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Sign-in, sign-out and token validation of a console session.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { GatewayApiBase, GatewayAPIError, withServerDetail, type PasswordSignIn, type PasswordSignInAnswer } from './base';
import type { ApiBodyExactly, ApiRequestBody } from '../api/controlPlaneTypes';

/**
 * What starts an enrolment in 2FA: the secret for the authenticator app, as
 * text, as an `otpauth://` URI and as a QR code (a data URI).
 */
export interface TwoFactorEnrollmentSetup {
    secret: string;
    provisioning_uri: string;
    qr_code: string;
}

/**
 * The answers of the second-factor routes, as far as the console reads them.
 * Written by hand: the Control Plane serves these routes without a response
 * model, so the contract types each answer `unknown`.
 */
export interface TwoFactorLoginSession {
    token?: string;
    [key: string]: unknown;
}
export interface TwoFactorStatus {
    enabled: boolean;
    verified_at?: string;
}
export interface TwoFactorSetupSecret {
    qr_code: string;
    secret: string;
}
export interface TwoFactorBackupCodes {
    backup_codes?: string[];
}

/**
 * What ends an enrolment at sign-in: the response of a tenant admin's
 * password sign-in, plus the backup codes, which are returned this once.
 */
export interface TwoFactorEnrollmentSession {
    access_token: string;
    token_type?: string;
    expires_in?: number;
    admin_type?: string;
    admin: Record<string, unknown>;
    tenant_id?: string;
    tenant_display_name?: string;
    is_trial?: boolean;
    trial_ends_at?: string | null;
    suspended?: boolean;
    suspension_reason?: string | null;
    backup_codes: string[];
}

export class AuthApi extends GatewayApiBase {

    // =============================================================================
    // Authentication Operations
    // =============================================================================

    /**
     * Sign a tenant administrator in.
     *
     * @param credentials Login credentials
     * @param credentials.username Username
     * @param credentials.password Password
     * @param credentials.tenant_id Tenant ID (optional: the Control Plane resolves it from the user name when omitted)
     */
    async login(credentials: {
        username: string;
        password: string;
        tenant_id?: string;
    }): Promise<PasswordSignIn> {
        try {
            const requestBody: Record<string, string> = {
                username: credentials.username,
                password: credentials.password,
            };
            if (credentials.tenant_id) {
                requestBody.tenant_id = credentials.tenant_id;
            }
            // Clear any expired token before login — the login endpoint
            // doesn't require auth, and an expired token would cause the
            // pre-request check to throw TokenExpiredError.
            this.clearAuthToken();

            const response = await this.request<PasswordSignInAnswer>('/v1/admin/users/tenant/login', {
                method: 'POST',
                body: JSON.stringify(requestBody),
            });
            return this.acceptPasswordSignIn(response, 'tenant');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'tenant_admin_login',
                resource: 'auth',
                username: credentials.username,
                tenant_id: credentials.tenant_id
            });
            throw withServerDetail(error);
        }
    }

    /**
     * Enrolment at sign-in, first step: have the control plane generate a
     * TOTP secret for the admin the temp token names. There is no session
     * yet, so any stored token is cleared first, as `login` does.
     */
    async startTwoFactorEnrollment(tempToken: string): Promise<TwoFactorEnrollmentSetup> {
        this.clearAuthToken();
        try {
            return await this.request<TwoFactorEnrollmentSetup>('/v1/admin/users/login/2fa/enroll/setup', {
                method: 'POST',
                body: JSON.stringify({ temp_token: tempToken }),
            });
        } catch (error) {
            throw withServerDetail(error);
        }
    }

    /**
     * Enrolment at sign-in, second step: verify a code from the
     * authenticator app. Returns the session and the backup codes. The
     * session is NOT installed here: the caller shows the backup codes
     * first (they are returned once) and then hands the response to
     * `complete2FALogin`.
     */
    async completeTwoFactorEnrollment(tempToken: string, code: string): Promise<TwoFactorEnrollmentSession> {
        this.clearAuthToken();
        try {
            return await this.request<TwoFactorEnrollmentSession>('/v1/admin/users/login/2fa/enroll/verify', {
                method: 'POST',
                body: JSON.stringify({ temp_token: tempToken, code }),
            });
        } catch (error) {
            throw withServerDetail(error);
        }
    }

    /**
     * The second step of a sign-in with a second factor: the temporary token
     * of the password step and a code. Answers with the session, which the
     * caller hands to `complete2FALogin`. A refusal is thrown as it came:
     * the form reads an account lock from it.
     * POST /v1/admin/users/login/2fa
     */
    async completeTwoFactorLogin(tempToken: string, code: string): Promise<TwoFactorLoginSession> {
        return this.request<TwoFactorLoginSession>('/v1/admin/users/login/2fa', {
            method: 'POST',
            body: JSON.stringify({ temp_token: tempToken, code } satisfies ApiRequestBody<'/v1/admin/users/login/2fa', 'post'>),
        });
    }

    /**
     * Whether the signed-in administrator has a second factor.
     * GET /v1/admin/users/me/2fa/status
     */
    async getTwoFactorStatus(): Promise<TwoFactorStatus> {
        return this.request<TwoFactorStatus>('/v1/admin/users/me/2fa/status');
    }

    /**
     * Have the Control Plane generate a TOTP secret for the signed-in administrator.
     * POST /v1/admin/users/me/2fa/setup
     */
    async startTwoFactorSetup(): Promise<TwoFactorSetupSecret> {
        return this.request<TwoFactorSetupSecret>('/v1/admin/users/me/2fa/setup', { method: 'POST' });
    }

    /**
     * Verify a code from the authenticator app and switch the second factor
     * on. Answers with the backup codes, which are returned this once.
     * POST /v1/admin/users/me/2fa/verify
     */
    async verifyTwoFactorSetup(code: string): Promise<TwoFactorBackupCodes> {
        return this.request<TwoFactorBackupCodes>('/v1/admin/users/me/2fa/verify', {
            method: 'POST',
            body: JSON.stringify({ code } satisfies ApiRequestBody<'/v1/admin/users/me/2fa/verify', 'post'>),
        });
    }

    /**
     * Switch the second factor of the signed-in administrator off.
     * POST /v1/admin/users/me/2fa/disable
     */
    async disableTwoFactor(code: string): Promise<unknown> {
        return this.request<unknown>('/v1/admin/users/me/2fa/disable', {
            method: 'POST',
            body: JSON.stringify({ code } satisfies ApiRequestBody<'/v1/admin/users/me/2fa/disable', 'post'>),
        });
    }

    /**
     * Change the signed-in administrator's own password. The current
     * password is part of the request: a session alone does not change it.
     * POST /v1/admin/users/me/change-password
     */
    async changeOwnPassword<Sent>(
        passwords: ApiBodyExactly<'/v1/admin/users/me/change-password', 'post', Sent>,
    ): Promise<unknown> {
        return this.request<unknown>('/v1/admin/users/me/change-password', {
            method: 'POST',
            body: JSON.stringify(passwords),
        });
    }

    /**
     * Logout from the gateway
     */
    async logout(): Promise<void> {
        try {
            // The unified logout endpoint.
            await this.request('/v1/admin/users/logout', { method: 'POST' });
        } catch (error) {
            this.errorReporter(error, { operation: 'logout', resource: 'auth' });
            // Continue with cleanup even if server logout fails
        } finally {
            this.clearAuthToken();
        }
    }

    /**
     * Validate current authentication token
     */
    async validateToken(): Promise<{ valid: boolean; user?: any }> {
        try {
            // The session's own user: /v1/admin/users/me.
            const user = await this.request('/v1/admin/users/me', {
                method: 'GET'
            });

            return {
                valid: true,
                user: user
            };
        } catch (error) {
            // If we get a 401, token is invalid
            if (error instanceof GatewayAPIError && error.status === 401) {
                return {
                    valid: false
                };
            }

            this.errorReporter(error, { operation: 'validate_token', resource: 'auth' });
            throw error;
        }
    }
}
