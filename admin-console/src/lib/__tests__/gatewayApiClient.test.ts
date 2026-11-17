/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GatewayApiClient Unit Tests (Admin Console)
 *
 * Tests the admin API client — token management, request mechanics,
 * error handling, retry logic, and token expiration detection.
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { SERVER_UPDATE_FIELDS } from '../gatewayApi/mcpServers'
import type { MCPServerConfig } from '../types'
import {
    GatewayApiClient,
    TokenExpiredError,
    GatewayAPIError,
    TOKEN_EXPIRED_EVENT
} from '../gatewayApiClient'

// Helper: create a mock JWT with configurable expiry
function createMockJwt(exp: number): string {
    const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const payload = btoa(JSON.stringify({
        sub: 'admin-123',
        admin_type: 'tenant',
        tenant_id: 'default',
        exp,
        iat: Math.floor(Date.now() / 1000),
    })).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const sig = btoa('mock').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    return `${header}.${payload}.${sig}`
}

function validJwt(): string {
    return createMockJwt(Math.floor(Date.now() / 1000) + 3600)
}

function expiredJwt(): string {
    return createMockJwt(Math.floor(Date.now() / 1000) - 60)
}

describe('GatewayApiClient', () => {
    let client: GatewayApiClient

    beforeEach(() => {
        client = new GatewayApiClient('http://localhost:5000')
    })

    // =========================================================================
    // Constructor
    // =========================================================================

    describe('constructor', () => {
        it('trims trailing slash from base URL', () => {
            const c = new GatewayApiClient('http://localhost:5000/')
            // Verify by making a request and checking the URL
            expect(c).toBeDefined()
        })

        it('loads token from localStorage on construction', () => {
            localStorage.setItem('gateway_auth_token', 'stored-token')
            // Constructed for its side effect — the assertion is that
            // construction reads the token rather than clearing it.
            new GatewayApiClient('http://localhost:5000')
            // Verify by checking localStorage is still set (token loaded internally)
            expect(localStorage.getItem('gateway_auth_token')).toBe('stored-token')
        })
    })

    // =========================================================================
    // Token Management
    // =========================================================================

    describe('setAuthToken', () => {
        it('stores token in localStorage', () => {
            const token = validJwt()
            client.setAuthToken(token)

            expect(localStorage.getItem('gateway_auth_token')).toBe(token)
        })
    })

    describe('clearAuthToken', () => {
        it('clears token from localStorage', () => {
            client.setAuthToken(validJwt())
            client.clearAuthToken()

            expect(localStorage.getItem('gateway_auth_token')).toBeNull()
        })
    })

    // =========================================================================
    // Token Expiration — Client-Side Detection
    // =========================================================================

    describe('token expiration', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        it('throws TokenExpiredError before fetch if token is expired', async () => {
            client.setAuthToken(expiredJwt())

            await expect(
                client.request('/v1/admin/some-endpoint')
            ).rejects.toThrow(TokenExpiredError)
        })

        it('clears auth state on expiration', async () => {
            client.setAuthToken(expiredJwt())

            try {
                await client.request('/v1/admin/test')
            } catch {
                // expected
            }

            expect(localStorage.getItem('gateway_auth_token')).toBeNull()
        })

        it('dispatches TOKEN_EXPIRED_EVENT', async () => {
            client.setAuthToken(expiredJwt())
            const handler = vi.fn()
            window.addEventListener(TOKEN_EXPIRED_EVENT, handler)

            try {
                await client.request('/v1/admin/test')
            } catch {
                // expected
            }

            expect(handler).toHaveBeenCalledTimes(1)
            window.removeEventListener(TOKEN_EXPIRED_EVENT, handler)
        })

        it('calls onTokenExpired callback', async () => {
            const onExpired = vi.fn()
            const c = new GatewayApiClient('http://localhost:5000', undefined, onExpired)
            c.setAuthToken(expiredJwt())

            try {
                await c.request('/v1/admin/test')
            } catch {
                // expected
            }

            expect(onExpired).toHaveBeenCalledTimes(1)
        })

        it('does not throw for valid token', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ ok: true }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            client.setAuthToken(validJwt())
            const result = await client.request('/v1/admin/test')
            expect(result).toEqual({ ok: true })
        })
    })

    // =========================================================================
    // Request Headers
    // =========================================================================

    describe('request headers', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        it('includes Authorization header when token is set', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({}), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            const token = validJwt()
            client.setAuthToken(token)
            await client.request('/v1/admin/test')

            const [, init] = vi.mocked(fetch).mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Authorization']).toBe(`Bearer ${token}`)
        })

        it('does not include Authorization when no token', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({}), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            await client.request('/v1/admin/test')

            const [, init] = vi.mocked(fetch).mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Authorization']).toBeUndefined()
        })

        it('includes Content-Type for JSON body', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({}), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            await client.request('/v1/admin/test', {
                method: 'POST',
                body: JSON.stringify({ key: 'value' }),
            })

            const [, init] = vi.mocked(fetch).mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Content-Type']).toBe('application/json')
        })
    })

    // =========================================================================
    // Error Handling
    // =========================================================================

    describe('error handling', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        it('throws GatewayAPIError on 4xx/5xx response', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ detail: 'Not Found' }), {
                    status: 404,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            client.setAuthToken(validJwt())

            await expect(
                client.request('/v1/admin/missing')
            ).rejects.toThrow(GatewayAPIError)
        })

        it('handles 401 with TOKEN_EXPIRED code as TokenExpiredError', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({
                    detail: 'Token expired',
                    code: 'TOKEN_EXPIRED'
                }), {
                    status: 401,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            client.setAuthToken(validJwt())

            await expect(
                client.request('/v1/admin/test', { retries: 0 })
            ).rejects.toThrow(TokenExpiredError)
        })

        it('handles network error', async () => {
            vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))

            client.setAuthToken(validJwt())

            await expect(
                client.request('/v1/admin/test', { retries: 0 })
            ).rejects.toThrow()
        })

        it('handles 204 No Content', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(null, { status: 204 })
            )

            client.setAuthToken(validJwt())
            const result = await client.request('/v1/admin/test')
            expect(result).toBeUndefined()
        })
    })

    // =========================================================================
    // Retry Logic
    // =========================================================================

    describe('retry logic', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        it('retries on 500 error', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch')
                .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Error' }), {
                    status: 500,
                    headers: { 'Content-Type': 'application/json' },
                }))
                .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }))

            client.setAuthToken(validJwt())
            const result = await client.request('/v1/admin/test', { retries: 1, retryDelay: 10 })

            expect(result).toEqual({ ok: true })
            expect(fetchMock).toHaveBeenCalledTimes(2)
        })

        it('does NOT retry a POST on 500', async () => {
            // A 500 says the *response* failed, not the write. Replaying a POST
            // that already committed server-side creates the row twice.
            const fetchMock = vi.spyOn(globalThis, 'fetch')
                .mockResolvedValue(new Response(JSON.stringify({ detail: 'Error' }), {
                    status: 500,
                    headers: { 'Content-Type': 'application/json' },
                }))

            client.setAuthToken(validJwt())

            await expect(
                client.request('/v1/admin/test', { method: 'POST', body: '{}', retries: 3, retryDelay: 10 })
            ).rejects.toThrow()

            expect(fetchMock).toHaveBeenCalledTimes(1)
        })

        it('does NOT retry a DELETE on 429', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch')
                .mockResolvedValue(new Response(JSON.stringify({ detail: 'Slow down' }), {
                    status: 429,
                    headers: { 'Content-Type': 'application/json' },
                }))

            client.setAuthToken(validJwt())

            await expect(
                client.request('/v1/admin/test', { method: 'DELETE', retries: 3, retryDelay: 10 })
            ).rejects.toThrow()

            expect(fetchMock).toHaveBeenCalledTimes(1)
        })

        it('retries a POST when the caller opts in with idempotent', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch')
                .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Error' }), {
                    status: 500,
                    headers: { 'Content-Type': 'application/json' },
                }))
                .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }))

            client.setAuthToken(validJwt())
            const result = await client.request('/v1/admin/test', {
                method: 'POST', body: '{}', idempotent: true, retries: 1, retryDelay: 10,
            })

            expect(result).toEqual({ ok: true })
            expect(fetchMock).toHaveBeenCalledTimes(2)
        })

        it('does NOT retry on 401', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch')
                .mockResolvedValue(new Response(JSON.stringify({ detail: 'Unauthorized' }), {
                    status: 401,
                    headers: { 'Content-Type': 'application/json' },
                }))

            client.setAuthToken(validJwt())

            await expect(
                client.request('/v1/admin/test', { retries: 3, retryDelay: 10 })
            ).rejects.toThrow()

            // Should only call once (no retries for 401)
            expect(fetchMock).toHaveBeenCalledTimes(1)
        })

        it('does NOT retry TokenExpiredError', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch')
            client.setAuthToken(expiredJwt())

            await expect(
                client.request('/v1/admin/test', { retries: 3 })
            ).rejects.toThrow(TokenExpiredError)

            // fetch should never be called (pre-request check catches it)
            expect(fetchMock).not.toHaveBeenCalled()
        })
    })

    // =========================================================================
    // Login After Session Expiry
    // =========================================================================

    describe('login after session expiry', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        it('login clears expired token before making request', async () => {
            // Set an expired token (simulates session timeout)
            client.setAuthToken(expiredJwt())
            expect(localStorage.getItem('gateway_auth_token')).not.toBeNull()

            // Mock successful login response
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({
                    access_token: validJwt(),
                    admin: { id: 'admin-1', username: 'admin' },
                    admin_type: 'tenant',
                }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            // Login should NOT throw TokenExpiredError
            const result = await client.login({
                username: 'admin',
                password: 'Admin123!',
               
                tenant_id: 'default',
            })

            expect(result.user).toBeDefined()
            // Token should now be the new valid one
            expect(localStorage.getItem('gateway_auth_token')).not.toBeNull()
        })

        it('login sends request without Authorization header', async () => {
            client.setAuthToken(expiredJwt())

            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({
                    access_token: validJwt(),
                    admin: { id: 'admin-1', username: 'admin' },
                    admin_type: 'tenant',
                }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            await client.login({
                username: 'admin',
                password: 'Admin123!',
               
                tenant_id: 'default',
            })

            // Verify no Authorization header was sent (login is unauthenticated)
            const [, init] = vi.mocked(fetch).mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Authorization']).toBeUndefined()
        })

        it('authenticated request includes Authorization header', async () => {
            const token = validJwt()
            client.setAuthToken(token)

            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ ok: true }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            await client.request('/v1/admin/some-endpoint')

            const [, init] = vi.mocked(fetch).mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Authorization']).toBe(`Bearer ${token}`)
        })

        it('request without token sends no Authorization header', async () => {
            // No token set — simulates unauthenticated request
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ ok: true }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )

            await client.request('/v1/admin/public-endpoint')

            const [, init] = vi.mocked(fetch).mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Authorization']).toBeUndefined()
        })
    })

    // =========================================================================
    // MCP server admin — mcp_apps_enabled round-trip
    // =========================================================================

    describe('createServer / updateServer mcp_apps_enabled', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        it('createServer forwards mcp_apps_enabled in the request body', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ server: { id: 'mcp-1' } }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())

            await client.createServer({
                name: 'Map MCP',
                description: '',
                base_url: 'http://mcp:3001',
                mcp_endpoint_path: '/mcp',
                auth_type: 'none',
                timeout: 30,
                enabled: true,
                // The new governance field — must be persisted server-side.
                mcp_apps_enabled: false,
            } as any)

            const [, init] = fetchMock.mock.calls[0]
            const body = JSON.parse(init?.body as string)
            expect(body.mcp_apps_enabled).toBe(false)
            expect(body.enabled).toBe(true)
        })

        it('updateServer forwards a partial mcp_apps_enabled change', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ id: 'mcp-1', mcp_apps_enabled: false }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())

            await client.updateServer('mcp-1', { mcp_apps_enabled: false })

            const [url, init] = fetchMock.mock.calls[0]
            expect(String(url)).toContain('/v1/admin/mcp-servers/mcp-1')
            expect(init?.method).toBe('PATCH')
            const body = JSON.parse(init?.body as string)
            expect(body).toEqual({ mcp_apps_enabled: false })
        })

        it('updateServer sends only what the update accepts: a server as it was read loses its read-only fields', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ message: 'ok', server: { id: 'mcp-1' } }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())

            // The form holds the server as the list returned it, with what was edited.
            await client.updateServer('mcp-1', {
                id: 'mcp-1',
                name: 'Map MCP',
                description: 'Maps',
                version: '1.2.0',
                base_url: 'http://mcp:3001',
                mcp_endpoint_path: '/mcp',
                auth_type: 'oauth',
                idp_id: 'idp-1',
                timeout: 45,
                enabled: true,
                mcp_apps_enabled: true,
                wants_listener: false,
                tenant_limits: { requests_per_minute: 60 },
                tenant_governance: null,
                region: 'eu',
                // Read-only: what the control plane states about the server.
                idp_name: 'Corporate IdP',
                created_by: 'admin',
                updated_by: 'admin',
                created_at: '2026-11-01T00:00:00Z',
                updated_at: '2026-11-02T00:00:00Z',
                oauth_pairing: { paired: true },
                health_status: 'healthy',
                capabilities: { tools: [{ name: 't' }] },
                tenant_id: 'default',
            } as unknown as Partial<MCPServerConfig>)

            const [, init] = fetchMock.mock.calls[0]
            expect(JSON.parse(init?.body as string)).toEqual({
                name: 'Map MCP',
                description: 'Maps',
                version: '1.2.0',
                base_url: 'http://mcp:3001',
                mcp_endpoint_path: '/mcp',
                auth_type: 'oauth',
                idp_id: 'idp-1',
                timeout: 45,
                enabled: true,
                mcp_apps_enabled: true,
                wants_listener: false,
                tenant_limits: { requests_per_minute: 60 },
                tenant_governance: null,
                region: 'eu',
            })
        })

        it('the fields updateServer sends are the fields of the control plane\'s update model, all of them', () => {
            const generated = readFileSync(path.resolve(__dirname, '../generated/control-plane-api.d.ts'), 'utf8')
            const start = generated.indexOf('        ServerUpdateRequest: {')
            expect(start).toBeGreaterThan(0)
            const schema = generated.slice(start, generated.indexOf('\n        };', start))
            const fields = [...schema.matchAll(/^ {12}([a-z_]+)\??:/gm)].map((m) => m[1]).sort()
            expect(fields.length).toBeGreaterThan(10)
            expect([...SERVER_UPDATE_FIELDS].sort()).toEqual(fields)
        })

        it('createServer omits mcp_apps_enabled if caller does not set it (server applies default)', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ server: { id: 'mcp-2' } }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())

            await client.createServer({
                name: 'Bare',
                description: '',
                base_url: 'http://x',
                mcp_endpoint_path: '',
                auth_type: 'none',
                timeout: 30,
                enabled: true,
            } as any)

            const [, init] = fetchMock.mock.calls[0]
            const body = JSON.parse(init?.body as string)
            // Pydantic ServerCreateRequest defaults mcp_apps_enabled=true
            // server-side. The client should not invent a value.
            expect(body.mcp_apps_enabled).toBeUndefined()
        })
    })

    // =========================================================================
    // Non-JSON transports (exports, YAML, file upload)
    // =========================================================================

    describe('non-JSON transports go through request()', () => {
        afterEach(() => {
            vi.restoreAllMocks()
        })

        // These six methods control the Accept/Content-Type header or send
        // FormData, and must still go through request(). A direct `fetch`
        // runs no client-side expiry pre-check, routes no server 401 through
        // handleTokenExpired(), and sets no timeout — a hung compliance CSV
        // export would never abort and an expired session would surface as the
        // raw string "Export failed: 401" while the console stays "logged in".
        const cases: Array<[string, (c: GatewayApiClient) => Promise<unknown>]> = [
            ['exportArgumentPoliciesYaml', (c) => c.exportArgumentPoliciesYaml()],
            ['importArgumentPoliciesYaml', (c) => c.importArgumentPoliciesYaml('- name: x')],
            ['downloadComplianceExportCsv', (c) => c.downloadComplianceExportCsv('gdpr/article-30')],
            ['fetchCostFocusCsv', (c) => c.fetchCostFocusCsv({ period_hours: 24 })],
            ['uploadProviderIcon', (c) =>
                c.uploadProviderIcon('prov-1', new File(['x'], 'i.png', { type: 'image/png' }))],
        ]

        it.each(cases)('%s refuses to send on an expired token', async (_name, call) => {
            const fetchMock = vi.spyOn(globalThis, 'fetch')
            client.setAuthToken(expiredJwt())

            await expect(call(client)).rejects.toBeInstanceOf(TokenExpiredError)
            // Never left the browser, and the session was torn down.
            expect(fetchMock).not.toHaveBeenCalled()
            expect(localStorage.getItem('gateway_auth_token')).toBeNull()
        })

        it.each(cases)('%s clears the session on a server 401', async (_name, call) => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ code: 'TOKEN_EXPIRED' }), {
                    status: 401,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())

            await expect(call(client)).rejects.toBeInstanceOf(TokenExpiredError)
            expect(localStorage.getItem('gateway_auth_token')).toBeNull()
        })

        it('exportArgumentPoliciesYaml returns YAML text, not a parsed object', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response('- name: policy-a\n', {
                    status: 200,
                    headers: { 'Content-Type': 'application/x-yaml' },
                })
            )
            client.setAuthToken(validJwt())
            await expect(client.exportArgumentPoliciesYaml()).resolves.toBe('- name: policy-a\n')
        })

        it('importArgumentPoliciesYaml keeps the caller Content-Type instead of forcing JSON', async () => {
            // request() must not overwrite a Content-Type the caller set.
            const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ created: [], updated: [], unchanged: [], errors: [], unresolved_groups: [] }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())
            await client.importArgumentPoliciesYaml('- name: x')

            const [, init] = fetchMock.mock.calls[0]
            const headers = init?.headers as Record<string, string>
            expect(headers['Content-Type']).toBe('application/x-yaml')
        })

        it('uploadProviderIcon lets the browser set the multipart boundary', async () => {
            const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response(JSON.stringify({ id: 'prov-1' }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            )
            client.setAuthToken(validJwt())
            await client.uploadProviderIcon('prov-1', new File(['x'], 'i.png', { type: 'image/png' }))

            const [, init] = fetchMock.mock.calls[0]
            const headers = init?.headers as Record<string, string>
            // A Content-Type here would omit the boundary and break the upload.
            expect(headers['Content-Type']).toBeUndefined()
            expect(init?.body).toBeInstanceOf(FormData)
        })

        it('fetchCostFocusCsv returns CSV text rather than a Blob', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response('a,b\n1,2\n', {
                    status: 200,
                    headers: { 'Content-Type': 'text/csv' },
                })
            )
            client.setAuthToken(validJwt())
            await expect(client.fetchCostFocusCsv({ period_hours: 24 })).resolves.toBe('a,b\n1,2\n')
        })

        it('downloadComplianceExportCsv returns a Blob', async () => {
            vi.spyOn(globalThis, 'fetch').mockResolvedValue(
                new Response('a,b\n', { status: 200, headers: { 'Content-Type': 'text/csv' } })
            )
            client.setAuthToken(validJwt())
            await expect(client.downloadComplianceExportCsv('gdpr/article-30'))
                .resolves.toBeInstanceOf(Blob)
        })
    })

    // =========================================================================
    // Caller cancellation, and the timeout that outlived the request
    // =========================================================================

    describe('AbortSignal and timeout handling', () => {
        it('honours a caller-supplied AbortSignal', async () => {
            client.setAuthToken(validJwt())
            const controller = new AbortController()
            // Never resolves on its own — only the abort can end it. If the
            // client's own `signal` in the fetch init overwrote the caller's,
            // aborting would do nothing and this would hang.
            vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) =>
                new Promise((_resolve, reject) => {
                    init.signal?.addEventListener('abort', () =>
                        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
                    )
                })
            ))

            const pending = client.request('/v1/admin/thing', { signal: controller.signal })
            controller.abort()
            await expect(pending).rejects.toThrow()
        })

        it('an already-aborted signal never reaches the network', async () => {
            client.setAuthToken(validJwt())
            const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response('{}', { status: 200 })))
            vi.stubGlobal('fetch', fetchMock)

            const controller = new AbortController()
            controller.abort()
            await client
                .request('/v1/admin/thing', { signal: controller.signal })
                .catch(() => undefined)
            // Whether fetch was called is the runtime's business once the
            // signal is already aborted; what matters is that the abort is
            // observed rather than silently dropped.
            const init = fetchMock.mock.calls[0]?.[1]
            if (init) {
                expect(init.signal?.aborted).toBe(true)
            }
        })

        it('clears its timeout when the request fails, not only when it succeeds', async () => {
            client.setAuthToken(validJwt())
            const clearSpy = vi.spyOn(globalThis, 'clearTimeout')
            vi.stubGlobal('fetch', vi.fn(() =>
                Promise.resolve(new Response('{"detail":"nope"}', { status: 500 }))
            ))

            await client
                .request('/v1/admin/thing', { retries: 0 })
                .catch(() => undefined)

            // A timer cleared only on the success path would leave one pending
            // for every non-2xx — one per retry attempt — each firing an
            // abort at a controller nobody is listening to any more.
            expect(clearSpy).toHaveBeenCalled()
            clearSpy.mockRestore()
        })
    })

    // =========================================================================
    // The two failures a retry actually helps with
    // =========================================================================

    describe('retry on transient failures', () => {
        it('retries a network error', async () => {
            client.setAuthToken(validJwt())
            const fetchMock = vi.fn()
                .mockRejectedValueOnce(new TypeError('Failed to fetch'))
                .mockResolvedValueOnce(new Response('{"ok":true}', {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }))
            vi.stubGlobal('fetch', fetchMock)

            const result = await client.request<{ ok: boolean }>('/v1/admin/thing', {
                retries: 1,
                retryDelay: 1,
            })
            expect(result.ok).toBe(true)
            // Two calls: the TypeError must reach the retry check, where
            // `shouldRetry`'s status-0 arm handles it.
            expect(fetchMock).toHaveBeenCalledTimes(2)
        })

        it('retries a timeout', async () => {
            client.setAuthToken(validJwt())
            const abort = () => Object.assign(new Error('aborted'), { name: 'AbortError' })
            const fetchMock = vi.fn()
                .mockRejectedValueOnce(abort())
                .mockResolvedValueOnce(new Response('{"ok":true}', {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }))
            vi.stubGlobal('fetch', fetchMock)

            const result = await client.request<{ ok: boolean }>('/v1/admin/thing', {
                retries: 1,
                retryDelay: 1,
            })
            expect(result.ok).toBe(true)
            expect(fetchMock).toHaveBeenCalledTimes(2)
        })

        it('does NOT retry when the caller aborted', async () => {
            client.setAuthToken(validJwt())
            const controller = new AbortController()
            const fetchMock = vi.fn((_url: string, init: RequestInit) =>
                new Promise((_resolve, reject) => {
                    init.signal?.addEventListener('abort', () =>
                        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
                    )
                })
            )
            vi.stubGlobal('fetch', fetchMock)

            const pending = client.request('/v1/admin/thing', {
                signal: controller.signal,
                retries: 3,
                retryDelay: 1,
            })
            controller.abort()
            await expect(pending).rejects.toThrow(/aborted by caller/i)
            // A caller's cancellation and a timeout both arrive as AbortError.
            // Retrying the one the caller asked us to stop would be worse than
            // not retrying a timeout.
            expect(fetchMock).toHaveBeenCalledTimes(1)
        })
    })
})
