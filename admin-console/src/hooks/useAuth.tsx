/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { createContext, useContext, useState, useEffect, ReactNode, useMemo, useCallback } from 'react';
import { GatewayApiClient, TOKEN_EXPIRED_EVENT, TokenExpiredError } from '../lib/gatewayApiClient';
import { PASSWORD_CHANGE_REQUIRED_EVENT } from '../lib/gatewayApi/base';
import { runBackground } from '../lib/asyncSafe';
import { debugLog } from '../lib/debugLog';
import { errorKind, isRecord, parseJson } from '../lib/narrow';
import { useSlot, type PrincipalSlotItem } from '../app/slots';
import type { components } from '../lib/generated/control-plane-api';

/** The `admin_type` of the one principal this provider knows by itself. */
const TENANT_ADMIN_TYPE = 'tenant';

/**
 * The kind of administrator a session is of, from everything that states it
 * (what was stored, what was asked for, what the control plane answered).
 * Statements that are absent are skipped. The answer is the stated type when
 * there is at least one statement, every one is a known type and they all
 * agree; anything else is `null`, which is no session. There is no default:
 * a session of a kind nobody here knows is not signed in.
 */
function statedAdminType(isKnown: (type: string) => boolean, statements: readonly unknown[]): string | null {
    const stated = statements.filter((statement) => statement !== undefined && statement !== null);
    const [first] = stated;
    if (typeof first !== 'string' || !isKnown(first)) return null;
    return stated.every((statement) => statement === first) ? first : null;
}

/** What `value` states under `key`, of whatever type; undefined when it states nothing. */
function statement(value: unknown, key: string): unknown {
    return isRecord(value) ? value[key] : undefined;
}

/** The message of a sign-in that answered for another kind of administrator. */
const UNKNOWN_ADMIN_TYPE = 'Sign-in refused: unknown administrator type';

/** The administrator as the control plane answers it (`/v1/admin/users/me`). */
type AnsweredAdmin = components['schemas']['CurrentAdminResponse'];

interface User {
    id: string;
    username: string;
    email: string;
    full_name?: string;
    /** A tenant administrator's role; a system administrator has none and answers none. */
    role_id?: AnsweredAdmin['role_id'];
    role_name?: AnsweredAdmin['role_name'];
    permissions: AnsweredAdmin['permissions'];
    last_login?: string;
    avatar_url?: string;
    totp_enabled?: boolean;
    admin_type?: string;
    tenant_id?: string;
    tenant_display_name?: string;
    is_trial?: boolean;
    trial_ends_at?: string;
    suspended?: boolean;
    suspension_reason?: string;
    /** Whether the administrator operates what the whole installation shares (the schedulers, say). */
    is_installation_operator?: boolean;
    /** Whether the installation has a system administrator above its tenants. */
    installation_has_system_admin?: boolean;
    /**
     * The password was generated for this account and must be changed
     * before anything else (`AdminResponse.password_change_required`).
     */
    password_change_required?: AnsweredAdmin['password_change_required'];
}

interface AuthContextType {
    user: User | null;
    isAuthenticated: boolean;
    isLoading: boolean;
    login: (username: string, password: string, adminType?: string, tenantId?: string) => Promise<void>;
    /**
     * Second step of the password + TOTP flow. The 2FA login endpoint
     * returns `{token, admin, tenant_id, tenant_display_name, is_trial, ...}` —
     * mirror the same localStorage/state setup that the password-only
     * `login()` performs so the session is usable without a page reload.
     * Also ends an enrolment at sign-in, whose response carries the session
     * as `access_token` (see TwoFactorEnrollmentStep).
     */
    complete2FALogin: (resp: any, adminType: string, tenantIdFallback?: string) => void;
    logout: () => Promise<void>;
    hasRole: (role: string) => boolean;
    hasPermission: (permission: string) => boolean;
    gatewayClient: GatewayApiClient;
    isTokenExpired: boolean;
    refreshToken: () => Promise<boolean>;
    isTenantAdmin: () => boolean;
    /**
     * The contributed principal (slot `app.principals`) the administrator is
     * signed in as; null for a tenant administrator and when signed out. A
     * contributed principal is of no tenant.
     */
    principal: PrincipalSlotItem | null;
    getCurrentTenantId: () => string | null;
    /**
     * The signed-in account must change its password before anything else:
     * its sign-in or `/me` said so, or a route was refused with 403
     * `password_change_required`. Only changing it, signing out and `/me`
     * are answered until then.
     */
    passwordChangeRequired: boolean;
    /** The password was changed: the session carries on as it is. */
    passwordChanged: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

interface AuthProviderProps {
    children: ReactNode;
    gatewayUrl?: string;
}

export const AuthProvider: React.FC<AuthProviderProps> = ({
                                                              children,
                                                              gatewayUrl = 'http://localhost:5050'
                                                          }) => {
    const [user, setUser] = useState<User | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isTokenExpired, setIsTokenExpired] = useState(false);

    // The kinds of administrator there are: the tenant administrator, and
    // whatever is contributed. Without a provider of slots, only the first.
    const principals = useSlot('app.principals');
    const isKnownAdminType = useCallback(
        (type: string) => type === TENANT_ADMIN_TYPE || principals.some((p) => p.id === type),
        [principals],
    );

    // Handle token expiration callback - Clear user immediately
    const handleTokenExpired = useCallback(() => {
        console.warn('Token expired - clearing all auth state immediately');

        // Set expired flag first
        setIsTokenExpired(true);

        // Then clear user (this makes isAuthenticated false)
        setUser(null);

        // Clear stored data
        if (typeof window !== 'undefined') {
            localStorage.removeItem('gateway_auth_token');
            localStorage.removeItem('gateway_user');
            localStorage.removeItem('gateway_admin_type');
            localStorage.removeItem('gateway_tenant_id');
        }

        debugLog('Session cleared: the token expired');
    }, []);

    // Create gateway client instance with stable reference and token expiration handler
    const gatewayClient = useMemo(() => {
        const client = new GatewayApiClient(
            gatewayUrl,
            (error, context) => {
                console.error(`Auth service error in ${context?.operation ?? 'a request'}:`, errorKind(error));
            },
            handleTokenExpired
        );

        // Load existing token on creation
        const existingToken = typeof window !== 'undefined' ? localStorage.getItem('gateway_auth_token') : null;
        if (existingToken) {
            client.setAuthToken(existingToken);
        }

        return client;
    }, [gatewayUrl, handleTokenExpired]);

    // Refresh token by re-validating with server
    const refreshToken = useCallback(async (): Promise<boolean> => {
        const token = localStorage.getItem('gateway_auth_token');
        if (!token) {
            return false;
        }

        try {
            const validationResult = await gatewayClient.validateToken();
            const adminType = statedAdminType(isKnownAdminType, [
                localStorage.getItem('gateway_admin_type'),
                statement(validationResult.user, 'admin_type'),
            ]);
            if (validationResult.valid && validationResult.user && adminType) {
                setUser(validationResult.user);
                setIsTokenExpired(false);
                debugLog('Session token validated again');
                return true;
            } else {
                console.warn('Token validation failed, clearing auth');
                handleTokenExpired();
                return false;
            }
        } catch (error) {
            if (error instanceof TokenExpiredError) {
                console.warn('Token expired during refresh attempt');
                handleTokenExpired();
                return false;
            }
            console.error('Token refresh failed:', errorKind(error));
            handleTokenExpired();
            return false;
        }
    }, [gatewayClient, handleTokenExpired, isKnownAdminType]);

    // Listen for global token expiration events
    useEffect(() => {
        const handleGlobalTokenExpired = () => {
            handleTokenExpired();
        };

        if (typeof window !== 'undefined') {
            window.addEventListener(TOKEN_EXPIRED_EVENT, handleGlobalTokenExpired);
            return () => {
                window.removeEventListener(TOKEN_EXPIRED_EVENT, handleGlobalTokenExpired);
            };
        }
    }, [handleTokenExpired]);

    // Sets whether the signed-in account owes a password change, in the
    // session and in what is stored of it. Nothing when signed out.
    const setPasswordChangeRequired = useCallback((owed: boolean) => {
        setUser((current) => (!current || !!current.password_change_required === owed ? current : { ...current, password_change_required: owed }));
        // What is stored of the session says the same, so a reload starts where this left off.
        const stored = parseJson(localStorage.getItem('gateway_user') ?? '');
        if (isRecord(stored)) localStorage.setItem('gateway_user', JSON.stringify({ ...stored, password_change_required: owed }));
    }, []);

    // A route refused until the password is changed, wherever it was asked:
    // the console goes to its change-password screen.
    useEffect(() => {
        const owed = () => setPasswordChangeRequired(true);
        window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, owed);
        return () => window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, owed);
    }, [setPasswordChangeRequired]);

    // Check authentication status on app start
    useEffect(() => {
        const checkAuthStatus = async () => {
            try {
                // Check for existing auth
                const token = localStorage.getItem('gateway_auth_token');
                const storedUser = localStorage.getItem('gateway_user');
                const storedAdminType = localStorage.getItem('gateway_admin_type');
                const storedTenantId = localStorage.getItem('gateway_tenant_id');

                if (!token || !storedUser) {
                    debugLog('No stored session');
                    setIsLoading(false);
                    return;
                }

                // Token was already set during client creation, now validate it
                const validationResult = await gatewayClient.validateToken();
                // What kind of administrator the session is of: as stored at
                // sign-in and as the control plane says now. A kind this
                // console does not know, or two that disagree, is no session.
                const adminType = statedAdminType(isKnownAdminType, [
                    storedAdminType,
                    statement(validationResult.user, 'admin_type'),
                ]);
                if (validationResult.valid && validationResult.user && !adminType) {
                    console.warn('Stored session is of an unknown administrator type — clearing it');
                    gatewayClient.clearAuthToken();
                    handleTokenExpired();
                } else if (validationResult.valid && validationResult.user && adminType) {
                    debugLog('Stored session is valid');

                    // Restore admin type and tenant ID from storage
                    const userData = {
                        ...validationResult.user,
                        admin_type: adminType,
                        tenant_id: storedTenantId || undefined
                    };

                    setUser(userData);
                    setIsTokenExpired(false);
                } else {
                    // Token invalid, clear storage
                    console.warn('Token validation failed');
                    handleTokenExpired();
                }
            } catch (error) {
                if (error instanceof TokenExpiredError) {
                    debugLog('Stored session expired');
                    handleTokenExpired();
                } else {
                    // Any other failure — the control plane is unreachable, it
                    // returned a 5xx, CORS rejected the call — means we could
                    // NOT establish who this is. The only safe outcome is no
                    // session: clear the stored token and land on the login
                    // screen.
                    //
                    // No fallback user is ever set here: anyone can make this
                    // request fail — by blocking it, or by waiting for a
                    // control-plane blip — and a client that renders any
                    // privileged surface on an auth *failure* is exactly
                    // backwards. Fail closed.
                    console.error('Auth check failed — clearing session:', errorKind(error));
                    handleTokenExpired();
                }
            } finally {
                setIsLoading(false);
            }
        };

        runBackground(checkAuthStatus(), 'check auth status');
    }, [gatewayClient, handleTokenExpired, isKnownAdminType]);

    // Periodic session validity check — detects expired JWT without waiting
    // for a failed request. Checks every 60 seconds.
    useEffect(() => {
        if (!user) return;

        const checkSession = () => {
            const token = localStorage.getItem('gateway_auth_token');
            if (!token) {
                handleTokenExpired();
                return;
            }
            try {
                const payload = JSON.parse(atob(token.split('.')[1]));
                const now = Math.floor(Date.now() / 1000);
                if (payload.exp < now) {
                    console.warn('Session expired (detected by periodic check)');
                    handleTokenExpired();
                }
            } catch {
                handleTokenExpired();
            }
        };

        const interval = setInterval(checkSession, 60_000); // every 60 seconds
        return () => clearInterval(interval);
    }, [user, handleTokenExpired]);

    const login = async (
        username: string,
        password: string,
        adminType: string = TENANT_ADMIN_TYPE,
        tenantId?: string
    ): Promise<void> => {
        try {
            // The tenant administrator's sign-in is this console's own; any
            // other kind of administrator signs in the way its contributed
            // principal says. A kind nobody contributed cannot sign in.
            const contributed = principals.find((p) => p.id === adminType);
            if (adminType !== TENANT_ADMIN_TYPE && !contributed) {
                throw new Error(UNKNOWN_ADMIN_TYPE);
            }
            const result = contributed
                ? await contributed.signIn(gatewayClient, { username, password })
                : await gatewayClient.login({ username, password, tenant_id: tenantId });

            // Check if 2FA is required
            if (result.requires_2fa) {
                const err = new Error('2FA_REQUIRED');
                (err as any).temp_token = result.temp_token;
                (err as any).tenant_id = result.tenant_id;
                (err as any).tenant_display_name = result.tenant_display_name;
                throw err;
            }

            // The tenant's policy requires 2FA and this admin has none: no
            // session was issued. Reported like the challenge above, under
            // its own name, so the form shows the enrolment step.
            if (result.requires_2fa_enrollment) {
                throw Object.assign(new Error('2FA_ENROLLMENT_REQUIRED'), {
                    temp_token: result.temp_token,
                    tenant_id: result.tenant_id,
                    tenant_display_name: result.tenant_display_name,
                });
            }

            // The answer may say what kind of administrator it is for. It has
            // to be the kind that was asked for: an answer for another kind
            // is not a session of this one, and its token is dropped.
            if (statedAdminType(isKnownAdminType, [adminType, statement(result.user, 'admin_type')]) !== adminType) {
                gatewayClient.clearAuthToken();
                throw new Error(UNKNOWN_ADMIN_TYPE);
            }

            // Extract user data and add admin type info
            // Use tenant_id from the response (backend resolves it), fallback to request param
            const resolvedTenantId = result.tenant_id || result.user?.tenant_id || tenantId;
            const tenantDisplayName = result.tenant_display_name || resolvedTenantId;
            const userData: User = {
                ...result.user,
                admin_type: adminType,
                tenant_id: adminType === TENANT_ADMIN_TYPE ? resolvedTenantId : undefined,
                tenant_display_name: adminType === TENANT_ADMIN_TYPE ? tenantDisplayName : undefined,
                is_trial: result.is_trial,
                trial_ends_at: result.trial_ends_at,
                suspended: result.suspended,
                suspension_reason: result.suspension_reason,
                password_change_required: result.password_change_required === true,
            };

            // Store user data and metadata in localStorage for persistence
            localStorage.setItem('gateway_user', JSON.stringify(userData));
            localStorage.setItem('gateway_admin_type', adminType);
            if (resolvedTenantId) {
                localStorage.setItem('gateway_tenant_id', resolvedTenantId);
            }

            // Set user state and clear expiration flag
            setUser(userData);
            setIsTokenExpired(false);

            debugLog('Signed in', adminType);
        } catch (error) {
            // The kind alone: an answer that owes a code or an enrolment
            // is thrown with its temp token on it.
            console.error('Login error:', errorKind(error));
            throw error;
        }
    };

    const complete2FALogin = (
        resp: any,
        adminType: string,
        tenantIdFallback?: string,
    ): void => {
        // As in `login`: the kind of administrator asked for must be one this
        // console knows, and the answer must not be for another. Checked
        // before the token is installed.
        const answered: unknown = resp;
        const stated = [adminType, statement(answered, 'admin_type'), statement(statement(answered, 'admin'), 'admin_type')];
        if (statedAdminType(isKnownAdminType, stated) !== adminType) {
            throw new Error(UNKNOWN_ADMIN_TYPE);
        }

        // The 2FA endpoint returns `token` (the LoginResponse uses
        // `access_token`). Normalize so the rest of the app doesn't
        // care which endpoint minted it.
        const token: string = resp?.token || resp?.access_token;
        if (!token) {
            throw new Error('2FA response missing token');
        }
        gatewayClient.setAuthToken(token);

        const rawAdmin = resp?.admin || {};
        const resolvedTenantId = resp?.tenant_id || rawAdmin?.tenant_id || tenantIdFallback;
        const tenantDisplayName = resp?.tenant_display_name || resolvedTenantId;

        const userData: User = {
            ...rawAdmin,
            admin_type: adminType,
            tenant_id: adminType === TENANT_ADMIN_TYPE ? resolvedTenantId : undefined,
            tenant_display_name: adminType === TENANT_ADMIN_TYPE ? tenantDisplayName : undefined,
            is_trial: resp?.is_trial,
            trial_ends_at: resp?.trial_ends_at,
            suspended: resp?.suspended,
            suspension_reason: resp?.suspension_reason,
            password_change_required: statement(answered, 'password_change_required') === true || statement(statement(answered, 'admin'), 'password_change_required') === true,
        };

        localStorage.setItem('gateway_user', JSON.stringify(userData));
        localStorage.setItem('gateway_admin_type', adminType);
        if (resolvedTenantId) {
            localStorage.setItem('gateway_tenant_id', resolvedTenantId);
        }

        setUser(userData);
        setIsTokenExpired(false);
    };

    const logout = async (): Promise<void> => {
        try {
            // Use gateway client for logout (handles server-side logout)
            await gatewayClient.logout();
        } catch (error) {
            console.error('Logout error:', errorKind(error));
            // Continue with local cleanup even if server logout fails
        } finally {
            // Always clean up local storage and state
            localStorage.removeItem('gateway_auth_token');
            localStorage.removeItem('gateway_user');
            localStorage.removeItem('gateway_admin_type');
            localStorage.removeItem('gateway_tenant_id');
            setUser(null);
            setIsTokenExpired(false);
            debugLog('Signed out');
        }
    };

    const hasRole = (roleName: string): boolean => {
        if (!user || !user.role_name) {
            return false;
        }
        // Check if user has the specific role or is a Tenant Admin (which has all permissions)
        return user.role_name === roleName || user.role_name === 'Tenant Admin';
    };

    // The contributed principal the user is signed in as, if any
    const principal = principals.find((p) => p.id === user?.admin_type) ?? null;

    const hasPermission = (permission: string): boolean => {
        if (!user || !user.permissions || !Array.isArray(user.permissions)) {
            return false;
        }
        // The principal the user is signed in as may hold every permission.
        // No other principal is asked: what one says of a role id is about
        // its own kind of session (a tenant administrator whose role id is
        // stated as another principal's is not that principal).
        if (principal?.holdsEveryPermission(user)) {
            return true;
        }
        // Check if user has the specific permission or wildcard
        return user.permissions.includes(permission) || user.permissions.includes('*');
    };

    // Check if current user is tenant admin
    const isTenantAdmin = (): boolean => {
        return user?.admin_type === TENANT_ADMIN_TYPE;
    };

    // Get current tenant ID (only for tenant admins)
    const getCurrentTenantId = (): string | null => {
        if (!user || user.admin_type !== TENANT_ADMIN_TYPE) {
            return null;
        }
        return user.tenant_id || null;
    };

    // User is authenticated ONLY if user exists AND token is not expired
    const isAuthenticated = !!user && !isTokenExpired;

    const passwordChangeRequired = isAuthenticated && user?.password_change_required === true;
    const passwordChanged = useCallback(() => setPasswordChangeRequired(false), [setPasswordChangeRequired]);

    const contextValue: AuthContextType = {
        user,
        isAuthenticated,
        isLoading,
        login,
        complete2FALogin,
        logout,
        hasRole,
        hasPermission,
        gatewayClient,
        isTokenExpired,
        refreshToken,
        isTenantAdmin,
        principal,
        getCurrentTenantId,
        passwordChangeRequired,
        passwordChanged,
    };

    return React.createElement(
        AuthContext.Provider,
        { value: contextValue },
        children
    );
};

export const useAuth = (): AuthContextType => {
    const context = useContext(AuthContext);
    if (!context) {
        throw new Error('useAuth must be used within an AuthProvider');
    }
    return context;
};