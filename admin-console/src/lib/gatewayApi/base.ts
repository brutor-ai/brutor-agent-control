/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { licenceRefusalOfBody, licenceRefusalText, quotaRefusalText, reportLicenceRefusal } from '../licenceRefusal';
import { isTenantKeyUnavailableBody, TENANT_KEY_UNAVAILABLE_TEXT } from '../gatewayRefusals';
import { errorKind, getRecord, getString } from '../narrow';

/**
 * The base of the Admin Console's API client: the session
 * token, the request method with its retries and interceptors, and the
 * errors. The domain modules in this directory extend it; the facade
 * `GatewayApiClient` (../gatewayApiClient.ts) composes them.
 */



// Token expiration event for global handling
export const TOKEN_EXPIRED_EVENT = 'gateway_token_expired';

/** localStorage key holding the console session token. One canonical name. */
export const GATEWAY_AUTH_TOKEN_KEY = 'gateway_auth_token';

/**
 * Client-side expiry check on a JWT. Exported as a free function so callers
 * that cannot reach a GatewayApiClient instance — the MCP registry subtree
 * builds its transport outside React — enforce the same contract instead of
 * re-deriving it. A malformed token counts as expired.
 */
export function isJwtExpired(token: string): boolean {
    try {
        const payload = JSON.parse(atob(token.split('.')[1]));
        const now = Math.floor(Date.now() / 1000);
        return payload.exp < now;
    } catch {
        return true;
    }
}

/**
 * Tear down the session the way the whole console expects: drop the stored
 * token and announce it, so the auth provider routes back to the login form.
 * Any transport that skips this leaves the operator staring at an in-panel
 * "HTTP 401" while the console still believes they are signed in.
 */
export function notifyTokenExpired(): void {
    if (typeof window === 'undefined') return;
    localStorage.removeItem(GATEWAY_AUTH_TOKEN_KEY);
    window.dispatchEvent(new CustomEvent(TOKEN_EXPIRED_EVENT));
}

// --- An account that must change its password first ----------------------
//
// The first administrator of a tenant created without credentials gets a
// password generated for it, and must change it before anything else: every
// admin route but changing the password, signing out and `/me` answers 403
// `{detail: {code: "password_change_required", message}}` until then. The
// refusal is announced, so the console goes to its change-password screen
// from whatever was refused. The session is kept: it is signed in.

/** Announced on every refusal of a route until the password is changed. */
export const PASSWORD_CHANGE_REQUIRED_EVENT = 'gateway_password_change_required';

/** The code of that refusal. */
export const PASSWORD_CHANGE_REQUIRED = 'password_change_required';

/** True when `error` is the Control Plane's refusal of a route until the password is changed. */
export function isPasswordChangeRequired(error: unknown): boolean {
    return error instanceof GatewayAPIError && error.status === 403 && error.code === PASSWORD_CHANGE_REQUIRED;
}

// --- The tenant an administrator of no tenant acts on -----------------------
//
// A tenant administrator's requests are answered for its own tenant. An
// administrator of no tenant (a contributed principal: the system
// administrator) has to name one in `?tenant_id=` on every tenant-scoped
// route, or the control plane answers 400. A page that shows one tenant's
// data asks such an administrator for the tenant (`TenantScoped`) and sets
// it here for as long as the page is shown. It is one value for every
// client instance, as the session token's storage key is.
let actingTenantId: string | null = null;

/** The tenant named on admin requests that name none; null for none. */
export function actingTenant(): string | null {
    return actingTenantId;
}

export function setActingTenant(tenantId: string | null): void {
    actingTenantId = tenantId || null;
}

/** `endpoint` naming `tenantId`, when it is an admin route that names no tenant itself. */
export function withTenantId(endpoint: string, tenantId: string | null): string {
    if (!tenantId || !endpoint.startsWith('/v1/admin/')) return endpoint;
    if (/[?&]tenant_id=/.test(endpoint)) return endpoint;
    return `${endpoint}${endpoint.includes('?') ? '&' : '?'}tenant_id=${encodeURIComponent(tenantId)}`;
}

/**
 * Per-mode fetch timeout budget for upstream health-check probes.
 * The server-side per-model `timeout` field is the authoritative
 * limit; these values just ensure the browser fetch outlives the
 * slowest probe each mode is allowed to take so we don't abort
 * mid-flight and report a phantom failure. Numbers chosen to be
 * a small headroom over the typical upstream max:
 *
 *   chat        — ~30 s (single completions round-trip)
 *   embedding   — ~30 s (single embeddings round-trip)
 *   audio       — ~120 s (Whisper on long inputs, TTS chunks)
 *   image       — ~300 s (gpt-image-1 + Imagen + Replicate workflows)
 *   video       — ~900 s (Sora / Veo / fal long jobs)
 *
 * Anything else falls back to the chat budget.
 */
export function healthCheckTimeoutMs(mode?: string | null): number {
    switch ((mode || '').toLowerCase()) {
        case 'image':
        case 'image_generation':
            return 300_000;
        case 'video':
        case 'video_generation':
            return 900_000;
        case 'audio':
        case 'tts':
        case 'stt':
        case 'speech':
        case 'transcription':
            return 120_000;
        case 'embedding':
        case 'embeddings':
            return 60_000;
        case 'chat':
        case 'completions':
        default:
            return 60_000;
    }
}

/**
 * Bulk health check budget — server fans out probes across every
 * keyed model and the slowest one (typically a video gen model)
 * dominates total wall-clock. 15 minutes matches the seeded
 * upper bound for video models in llm_model.py.
 */
export const BULK_HEALTH_CHECK_TIMEOUT_MS = 900_000;

export class TokenExpiredError extends Error {
    constructor(message: string = 'Authentication token has expired') {
        super(message);
        this.name = 'TokenExpiredError';
    }
}

export interface ErrorContext {
    operation: string;
    resource?: string;
    serverId?: string;
    serverName?: string;
    userId?: string;
    [key: string]: any;
}

export type ErrorReporter = (error: any, context?: ErrorContext) => void;

/**
 * Custom error class for Gateway API operations
 */
export class GatewayAPIError extends Error {
    public status: number;
    public code?: string;
    public details?: any;

    constructor(message: string, status: number, code?: string, details?: any) {
        super(message);
        this.name = 'GatewayAPIError';
        this.status = status;
        this.code = code;
        this.details = details;
    }
}

/**
 * The client reports every 401 as "Authentication failed" (base.ts). The
 * sign-in and enrolment calls are refused with 401 for reasons the admin has
 * to read ("Invalid credentials", "Invalid 2FA code", "The time to set up
 * two-factor authentication ran out. Please sign in again."), so their 401
 * carries the control plane's own `detail` instead. A refused sign-in's
 * `detail` is the same for a wrong user name and a wrong password, so
 * showing it tells nothing about which was wrong.
 */
export function withServerDetail(error: unknown): unknown {
    if (error instanceof GatewayAPIError && error.status === 401) {
        const detail = getString(error.details, 'detail');
        if (detail) {
            return new GatewayAPIError(detail, 401, error.code, error.details);
        }
    }
    return error;
}

/** What the control plane answers a password sign-in with. */
export interface PasswordSignInAnswer {
    access_token?: string;
    admin: any;
    admin_type?: string;
    tenant_id?: string;
    tenant_display_name?: string;
    is_trial?: boolean;
    trial_ends_at?: string;
    suspended?: boolean;
    suspension_reason?: string;
    /** The account's password was generated for it and must be changed first (also in `admin`). */
    password_change_required?: boolean;
    requires_2fa?: boolean;
    requires_2fa_enrollment?: boolean;
    temp_token?: string;
}

/** What a password sign-in gives its caller, for any kind of administrator. */
export interface PasswordSignIn {
    user: any;
    /** The kind of administrator that was asked to sign in. */
    admin_type: string;
    tenant_id?: string;
    tenant_display_name?: string;
    is_trial?: boolean;
    trial_ends_at?: string;
    suspended?: boolean;
    suspension_reason?: string;
    /**
     * The account must change its password before anything else: the
     * sign-in answer says so at its top or in its `admin`. The session is
     * issued; every other route refuses it until the change.
     */
    password_change_required?: boolean;
    requires_2fa?: boolean;
    /**
     * The tenant's two-factor policy requires 2FA and this admin has
     * none: no session was issued, and `temp_token` opens the enrolment
     * (`startTwoFactorEnrollment`, `completeTwoFactorEnrollment`).
     */
    requires_2fa_enrollment?: boolean;
    temp_token?: string;
}

/**
 * Unified Gateway API Client
 *
 * Provides a complete interface for all AI Gateway operations including:
 * - Authentication and session management
 * - MCP Server CRUD operations
 * - Virtual Server management
 * - Configuration management
 * - Health monitoring and transport layer operations
 * - Proxy logs and admin operations
 * - MCP Registry config and search
 * - End User Management (Admin)
 * - Gateway OAuth Client Management (Admin)
 *
 * Features:
 * - Automatic token management and injection
 * - Centralized error handling and reporting
 * - Request/response interceptors
 * - Retry logic with exponential backoff
 * - Type-safe method signatures
 */
export class GatewayApiBase {
    protected baseUrl: string;
    protected authToken: string | null = null;
    private defaultTimeout = 30000; // 30 seconds
    private requestInterceptors: Array<(config: RequestInit) => RequestInit> = [];
    private responseInterceptors: Array<(response: Response) => Response | Promise<Response>> = [];
    protected errorReporter: ErrorReporter;
    private onTokenExpired?: () => void;

    constructor(
        baseUrl: string = 'http://localhost:5050',
        errorReporter?: ErrorReporter,
        onTokenExpired?: () => void
    ) {
        this.baseUrl = baseUrl.replace(/\/$/, ''); // Remove trailing slash

        // Default error reporter
        this.errorReporter = errorReporter || ((error, context) => {
            console.error(`Gateway API error in ${context?.operation ?? 'a request'}:`, errorKind(error));
        });

        this.onTokenExpired = onTokenExpired;

        // Get auth token from localStorage if available (browser only)
        if (typeof window !== 'undefined') {
            this.authToken = localStorage.getItem('gateway_auth_token');
        }
    }

    // =============================================================================
    // Token Expired Handling
    // =============================================================================

    /**
     * Check if token is expired client-side (basic check)
     */
    private isTokenExpired(token: string): boolean {
        return isJwtExpired(token);
    }

    /**
     * Handle token expiration
     */
    private handleTokenExpired(): void {
        console.warn('JWT token has expired, clearing auth state');

        // Clear token from memory and storage, then announce it.
        this.authToken = null;
        notifyTokenExpired();

        // Call callback if provided
        if (this.onTokenExpired) {
            this.onTokenExpired();
        }
    }

    // =============================================================================
    // Authentication Management
    // =============================================================================

    /**
     * Set authentication token for API requests
     */
    public setAuthToken(token: string): void {
        this.authToken = token;
        if (typeof window !== 'undefined') {
            localStorage.setItem('gateway_auth_token', token);
        }
    }

    /**
     * Clear authentication token
     */
    public clearAuthToken(): void {
        this.authToken = null;
        if (typeof window !== 'undefined') {
            localStorage.removeItem('gateway_auth_token');
        }
    }

    // =============================================================================
    // Password sign-in
    // =============================================================================

    /**
     * What the answer to a password sign-in means: a challenge for a second
     * factor, an enrolment owed, or a session, whose token is stored. The one
     * implementation, for the tenant administrator's sign-in (auth.ts) and
     * for a sign-in another API module adds for another kind of
     * administrator. The caller clears any stored token and sends the
     * request itself, so that the path is written where its module is.
     */
    protected acceptPasswordSignIn(response: PasswordSignInAnswer, adminType: string): PasswordSignIn {
        // 2FA challenge: no access_token yet — the client must call
        // /v1/admin/users/login/2fa with the temp_token and TOTP code.
        // Propagate the flags up so useAuth can redirect to the 2FA form.
        if (response.requires_2fa) {
            return {
                user: response.admin,
                admin_type: adminType,
                tenant_id: response.tenant_id,
                tenant_display_name: response.tenant_display_name,
                requires_2fa: true,
                temp_token: response.temp_token,
            };
        }

        // Enrolment owed: like the challenge above, no access_token.
        // The temp_token opens the two enrolment calls (auth.ts) and
        // nothing else; the session comes back from the second one.
        if (response.requires_2fa_enrollment) {
            return {
                user: response.admin as unknown,
                admin_type: adminType,
                tenant_id: response.tenant_id,
                tenant_display_name: response.tenant_display_name,
                requires_2fa_enrollment: true,
                temp_token: response.temp_token,
            };
        }

        // Store the access token automatically
        if (response.access_token) {
            this.setAuthToken(response.access_token);
        }

        return {
            user: response.admin,
            admin_type: adminType,
            tenant_id: response.tenant_id,
            tenant_display_name: response.tenant_display_name,
            is_trial: response.is_trial,
            trial_ends_at: response.trial_ends_at,
            suspended: response.suspended,
            suspension_reason: response.suspension_reason,
            password_change_required:
                response.password_change_required === true || getRecord(response, 'admin')?.password_change_required === true,
        };
    }

    // =============================================================================
    // Core HTTP Request Method
    // =============================================================================

    /**
     * Core request method with enhanced error handling, retries, and interceptors
     */
    public async request<T = any>(
        endpoint: string,
        options: RequestInit & {
            timeout?: number;
            retries?: number;
            retryDelay?: number;
            idempotent?: boolean;
            /**
             * How to decode a 2xx body. Default 'auto' sniffs the response
             * Content-Type. The explicit forms exist so downloads and
             * YAML/CSV endpoints need no `fetch` of their own to control
             * decoding — such an escape hatch silently skips the expiry
             * pre-check, the 401 → session-clear path and the timeout, so a
             * hung CSV export never aborts and an expired session surfaces
             * as the raw string "Export failed: 401" while the console still
             * believes it is logged in.
             * 'raw' hands back the Response itself for callers that need a
             * response header (e.g. Content-Disposition).
             */
            responseType?: 'auto' | 'json' | 'text' | 'blob' | 'raw';
            /**
             * The request is about the installation, not about one tenant
             * (the list of tenants): the tenant an administrator of no
             * tenant is acting on is not named on it.
             */
            ofNoTenant?: boolean;
        } = {}
    ): Promise<T> {
        const {
            timeout = this.defaultTimeout,
            retries = 2,
            retryDelay = 1000,
            idempotent,
            responseType = 'auto',
            ofNoTenant = false,
            ...fetchOptions
        } = options;

        // Whether a failed attempt may be replayed.
        //
        // A 5xx/429 tells us the response failed, never that the write did.
        // A POST that the control plane committed and then died on — or that a
        // proxy answered 502/504 for after the row landed — is indistinguishable
        // here from one that never ran, so replaying it duplicates governance
        // writes: two resource groups, two API keys, a second revoke. Reads have
        // no such hazard, so retry is on by default only for GET/HEAD (and the
        // no-method case, which fetch treats as GET). A caller that knows its
        // write is safe to replay opts in with `idempotent: true`.
        const requestMethod = (fetchOptions.method ?? 'GET').toUpperCase();
        const retriable = idempotent ?? (requestMethod === 'GET' || requestMethod === 'HEAD');

        // Check if token is expired before making request
        if (this.authToken && this.isTokenExpired(this.authToken)) {
            this.handleTokenExpired();
            throw new TokenExpiredError();
        }

        const url = `${this.baseUrl}${withTenantId(endpoint, ofNoTenant ? null : actingTenantId)}`;

        // Build headers
        const headers: Record<string, string> = {
            'Accept': 'application/json',
            ...options.headers as Record<string, string>,
        };

        if (this.authToken) {
            headers['Authorization'] = `Bearer ${this.authToken}`;
        }

        // Apply request interceptors
        let requestConfig: RequestInit = {
            ...fetchOptions,
            headers,
        };

        for (const interceptor of this.requestInterceptors) {
            requestConfig = interceptor(requestConfig);
        }

        // FormData must set its own Content-Type so the browser can add the
        // multipart boundary. A caller-supplied Content-Type is also honoured:
        // overwriting it here turned the YAML policy import into a JSON POST
        // the control plane rejected, which is why that method forked off into
        // its own `fetch` in the first place.
        if (!(requestConfig.body instanceof FormData) && !headers['Content-Type']) {
            headers['Content-Type'] = 'application/json';
        }

        // The caller's own AbortSignal, if they passed one.
        // `requestConfig` spreads into the fetch init below, so a plain
        // `signal:` there would overwrite it: a caller could pass a signal,
        // see no error, and find that aborting does nothing. Compose instead
        // of replace, so both the timeout and the caller can cancel the request.
        const callerSignal = (options as RequestInit).signal ?? null;

        const attemptRequest = async (attempt: number): Promise<T> => {
            const controller = new AbortController();
            // `clearTimeout` on the success path only would leave the timer
            // pending after every throw before it (a 401, a non-2xx, a network
            // failure). With retries that is one orphaned timer per attempt,
            // each firing an abort at a controller nobody is listening to any
            // more. The `finally` below makes it unconditional.
            const timeoutId = setTimeout(() => controller.abort(), timeout);
            const abortFromCaller = () => controller.abort();
            callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
            if (callerSignal?.aborted) {
                controller.abort();
            }
            try {
                const response = await fetch(url, {
                    ...requestConfig,
                    signal: controller.signal,
                });


                // Handle token expiration responses
                if (response.status === 401) {
                    const errorBody = await response.text();
                    let errorData: any = {};

                    try {
                        errorData = JSON.parse(errorBody);
                    } catch {
                        // Fallback if response isn't JSON
                    }

                    // Check for specific token expiration error codes
                    if (
                        errorData.code === 'TOKEN_EXPIRED' ||
                        errorData.error === 'token_expired' ||
                        (typeof errorData.detail === 'object' && errorData.detail?.code === 'TOKEN_EXPIRED')
                    ) {
                        this.handleTokenExpired();
                        throw new TokenExpiredError(errorData.message || 'JWT token has expired');
                    }

                    // Other 401 errors (invalid token, etc.)
                    throw new GatewayAPIError(
                        errorData.message || 'Authentication failed',
                        401,
                        errorData.code || 'AUTH_FAILED',
                        errorData
                    );
                }

                // Apply response interceptors
                let processedResponse = response;
                for (const interceptor of this.responseInterceptors) {
                    processedResponse = await interceptor(processedResponse);
                }

                if (!processedResponse.ok) {
                    let errorMessage = `HTTP ${processedResponse.status}: ${processedResponse.statusText}`;
                    let errorDetails: any = null;

                    try {
                        const errorBody = await processedResponse.text();
                        if (errorBody) {
                            try {
                                errorDetails = JSON.parse(errorBody);
                                // Extract a string error message — detail may be a string, array, or object
                                const detail = errorDetails.detail;
                                if (Array.isArray(detail)) {
                                    // FastAPI 422 validation errors — format as readable message
                                    errorMessage = detail.map((e: any) => {
                                        const field = e.loc?.slice(1).join('.') || 'unknown';
                                        return `${field}: ${e.msg}`;
                                    }).join('; ');
                                } else {
                                    // An object detail carries its sentence as
                                    // `message`, or as `detail` where the control
                                    // plane hands on the gateway's answer.
                                    const detailMessage = typeof detail === 'string'
                                        ? detail
                                        : (getString(detail, 'message') || getString(detail, 'detail') || null);
                                    errorMessage = errorDetails.message || errorDetails.error || detailMessage || errorMessage;
                                }
                                // A refusal that comes from the license or from
                                // the tenant's quota: the console's own sentence
                                // (licenceRefusal.ts), and whoever listens is told.
                                const refusedByLicence = licenceRefusalOfBody(errorDetails);
                                if (refusedByLicence) {
                                    errorMessage = licenceRefusalText(refusedByLicence);
                                    reportLicenceRefusal(refusedByLicence);
                                } else if (isTenantKeyUnavailableBody(errorDetails)) {
                                    errorMessage = TENANT_KEY_UNAVAILABLE_TEXT;
                                } else {
                                    errorMessage = quotaRefusalText(errorDetails) ?? errorMessage;
                                }
                            } catch {
                                errorMessage = errorBody || errorMessage;
                            }
                        }
                    } catch {
                        // Ignore JSON parsing errors for error body
                    }

                    // Refused until the password is changed: announced, so
                    // the console goes to its change-password screen.
                    const mustChangePassword =
                        processedResponse.status === 403 &&
                        getString(getRecord(errorDetails, 'detail'), 'code') === PASSWORD_CHANGE_REQUIRED;
                    if (mustChangePassword && typeof window !== 'undefined') {
                        window.dispatchEvent(new CustomEvent(PASSWORD_CHANGE_REQUIRED_EVENT));
                    }
                    const error = new GatewayAPIError(
                        errorMessage,
                        processedResponse.status,
                        mustChangePassword ? PASSWORD_CHANGE_REQUIRED : errorDetails?.code,
                        errorDetails
                    );
                    // Attach validation errors array for form handling
                    if (Array.isArray(errorDetails?.detail)) {
                        (error as any).validationErrors = errorDetails.detail;
                    }
                    throw error;
                }

                // Caller pinned the decoding — honour it before any sniffing.
                // 'raw' comes first so a 204 export (or any empty-bodied
                // response) still yields the Response and its headers.
                if (responseType === 'raw') {
                    return processedResponse as unknown as T;
                }

                // Handle 204 No Content (empty response body)
                if (processedResponse.status === 204) {
                    return undefined as unknown as T;
                }

                if (responseType === 'blob') {
                    return (await processedResponse.blob()) as unknown as T;
                }
                if (responseType === 'text') {
                    return (await processedResponse.text()) as unknown as T;
                }
                if (responseType === 'json') {
                    const text = await processedResponse.text();
                    if (!text || text.trim() === '') {
                        return undefined as unknown as T;
                    }
                    return JSON.parse(text) as T;
                }

                // Parse response based on content type
                const contentType = processedResponse.headers.get('content-type');

                // Handle binary/blob responses (for file downloads)
                if (contentType && (
                    contentType.includes('application/octet-stream') ||
                    contentType.includes('text/csv') ||
                    contentType.includes('application/csv')
                )) {
                    return (await processedResponse.blob()) as unknown as T;
                }

                // Handle JSON responses
                if (contentType && contentType.includes('application/json')) {
                    // Check if response has content before parsing
                    const text = await processedResponse.text();
                    if (!text || text.trim() === '') {
                        return undefined as unknown as T;
                    }
                    try {
                        const data = JSON.parse(text);
                        return data;
                    } catch (e) {
                        console.error('Failed to parse JSON response:', errorKind(e));
                        throw new GatewayAPIError(
                            'Invalid JSON response from server',
                            processedResponse.status,
                            'INVALID_JSON'
                        );
                    }
                }

                // Default to text
                return processedResponse.text() as unknown as T;

            } catch (error: any) {
                // Don't retry token expiration errors
                if (error instanceof TokenExpiredError) {
                    throw error;
                }

                // Normalise, then let the retry check see the
                // result. A `throw` right here, above the retry test, would
                // make `shouldRetry`'s `status === 0` and `status === 408`
                // arms unreachable: transient network drops and timeouts —
                // the two failures where a retry genuinely helps — would
                // never be retried.
                let normalized: GatewayAPIError | null = null;

                if (error.name === 'AbortError') {
                    // A caller-initiated abort is not a failure to retry: the
                    // caller asked us to stop. Only the timeout's own abort
                    // becomes a retriable 408. (The caller's signal is
                    // composed into the controller, so both
                    // arrive here as the same AbortError.)
                    if (callerSignal?.aborted) {
                        throw new GatewayAPIError(
                            'Request aborted by caller',
                            0,
                            'ABORTED'
                        );
                    }
                    normalized = new GatewayAPIError(
                        `Request timeout after ${timeout}ms`,
                        408,
                        'TIMEOUT'
                    );
                } else if (error instanceof TypeError && error.message.includes('fetch')) {
                    normalized = new GatewayAPIError(
                        'Network error: Unable to connect to gateway server',
                        0,
                        'NETWORK_ERROR'
                    );
                }

                const decided = normalized ?? error;

                // Retry logic for certain errors (but not auth errors)
                if (retriable && attempt < retries && this.shouldRetry(decided) && !(decided instanceof GatewayAPIError && decided.status === 401)) {
                    await this.delay(retryDelay * Math.pow(2, attempt));
                    return attemptRequest(attempt + 1);
                }

                // Re-throw GatewayAPIError instances
                if (decided instanceof GatewayAPIError) {
                    throw decided;
                }

                // Wrap other errors
                throw new GatewayAPIError(
                    error.message || 'Unknown error occurred',
                    0,
                    'UNKNOWN_ERROR',
                    error
                );
            } finally {
                // Runs on every exit: success, throw, and retry.
                clearTimeout(timeoutId);
                callerSignal?.removeEventListener('abort', abortFromCaller);
            }
        };

        return attemptRequest(0);
    }

    /**
     * Determine if an error should trigger a retry
     */
    private shouldRetry(error: any): boolean {
        // Retry on network errors, timeouts, and certain HTTP status codes
        if (error instanceof GatewayAPIError) {
            return error.status === 0 || // Network error
                error.status === 408 || // Timeout
                error.status === 429 || // Rate limited
                error.status >= 500;    // Server errors
        }
        return false;
    }

    /**
     * Utility delay function for retries
     */
    private delay(ms: number): Promise<void> {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    /**
     * Stream a binary export from the gateway (CSV / JSONL).
     * Returns the response Blob plus the suggested filename pulled from
     * the Content-Disposition header (if the server provided one).
     */
    protected async streamExport(
        endpoint: string,
        operation: string,
        body?: Record<string, unknown>,
    ): Promise<{ blob: Blob; filename?: string }> {
        // 'raw' rather than 'blob' because the suggested filename lives in a
        // response header. Going through request() is what gives the export
        // the timeout and the 401 → session-clear path: without them a
        // hung export hangs forever, and an expired session shows
        // up as an in-panel error string while the console stays "logged in".
        let response: Response;
        try {
            // An export with a body (the filters of a list) is a POST; one
            // whose filters are in its query string is a GET.
            response = await this.request<Response>(endpoint, {
                method: body === undefined ? 'GET' : 'POST',
                headers: { Accept: '*/*' },
                responseType: 'raw',
                ...(body === undefined ? {} : { body: JSON.stringify(body) }),
            });
        } catch (error) {
            if (!(error instanceof TokenExpiredError)) {
                this.errorReporter(error, { operation });
            }
            throw error;
        }

        const blob = await response.blob();
        const disposition = response.headers.get('Content-Disposition') || '';
        const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
        return { blob, filename: match ? decodeURIComponent(match[1]) : undefined };
    }
}
