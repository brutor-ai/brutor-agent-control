/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An administrator whose password was generated for it (the first
 * administrator of a tenant created without credentials) must change it
 * before anything else. The Control Plane says so in the sign-in answer
 * (`password_change_required`, at the top and in `admin`), in `/me`, and by
 * refusing every other admin route with 403
 * `{detail: {code: "password_change_required", message}}`; the core refuses
 * such a session outright (401).
 *
 * So the console goes straight to a change-password screen: on a sign-in
 * that says so, on a stored session whose `/me` says so, and on that 403
 * from any route. On that screen it asks nothing but changing the password,
 * signing out and `/me`, so nothing it does is refused and nothing signs it
 * out again in a loop. After the change it carries on, in the same session.
 */

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('reactflow/dist/style.css', () => ({}));
vi.mock('../../components/monitoring/MissionControlDashboard', () => {
    const Stub: React.FC = () => <div data-testid="screen">MissionControlDashboard</div>;
    return { default: Stub };
});

import { AppShell } from '../../App';
import { communityComposition } from '../../test/communityComposition';
import { TENANT_ADMIN, TOKEN } from '../../test/controlPlane';
import { fill, submit } from '../../test/users/tenantAdminProfile';

const REFUSAL = {
    code: 'password_change_required',
    message: "This account's password was generated for it and must be changed first: POST /v1/admin/users/me/change-password.",
};
/** The routes the Control Plane answers such an account. */
const ALLOWED = new Set(['POST /v1/admin/users/me/change-password', 'POST /v1/admin/users/logout', 'GET /v1/admin/users/me', 'POST /v1/admin/users/tenant/login']);

const plane = {
    /** Whether the account still owes the change. */
    owes: true,
    /** Whether the sign-in answer says so (a stale console might not be told). */
    loginSays: true,
    requests: [] as string[],
    changeBodies: [] as unknown[],
};

const json = (payload: unknown, status = 200) =>
    Promise.resolve(new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } }));

function stubFetch(): void {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
        const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        const url = new URL(href);
        const method = (init?.method ?? 'GET').toUpperCase();
        const route = `${method} ${url.pathname}`;
        plane.requests.push(route);
        const admin = { ...TENANT_ADMIN, password_change_required: plane.owes };
        if (route === 'POST /v1/admin/users/tenant/login') {
            const says = plane.loginSays && plane.owes;
            return json({ access_token: TOKEN, admin: { ...admin, password_change_required: says }, tenant_id: 'acme', password_change_required: says });
        }
        if (route === 'GET /v1/admin/users/me') return json(admin);
        if (route === 'POST /v1/admin/users/logout') return json({ message: 'Logged out' });
        if (route === 'POST /v1/admin/users/me/change-password') {
            plane.changeBodies.push(JSON.parse(typeof init?.body === 'string' ? init.body : 'null'));
            plane.owes = false;
            return json({ message: 'Password changed' });
        }
        if (plane.owes) return json({ detail: REFUSAL }, 403);
        if (url.pathname === '/v1/admin/tenants/current') return json({ id: 'acme', display_name: 'Acme Corp', features: {} });
        if (url.pathname === '/v1/admin/resource-groups/hierarchy-tree') return json({ tree: [] });
        return json({});
    });
}

async function settled(): Promise<void> {
    for (let i = 0; i < 8; i++) {
        await act(async () => {
            await new Promise((resolve) => setTimeout(resolve, 0));
        });
    }
}

const set = (placeholder: string, value: string) => fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });

async function signIn(): Promise<void> {
    render(<AppShell composition={communityComposition()} />);
    await screen.findByPlaceholderText('Enter your username');
    set('Enter your username', 'admin');
    set('Enter your password', 'generated-once');
    fireEvent.click(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ }));
}

const changeScreen = () => screen.findByRole('heading', { name: 'Change your password' });
const shell = () => document.querySelector('.app-shell');

beforeEach(() => {
    plane.owes = true;
    plane.loginSays = true;
    plane.requests = [];
    plane.changeBodies = [];
    localStorage.clear();
    stubFetch();
    for (const method of ['log', 'warn', 'error', 'info', 'debug'] as const) {
        vi.spyOn(console, method).mockImplementation(() => {});
    }
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    localStorage.clear();
});

describe('an administrator who must change its password', () => {
    it('is taken from the sign-in straight to the change-password screen, and nothing else is asked', async () => {
        await signIn();
        await changeScreen();
        await settled();
        expect(shell()).toBeNull();
        expect(screen.getByText(/must be changed before you can use the console/)).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
        // Only what the Control Plane answers such an account was asked.
        expect(plane.requests.filter((route) => !ALLOWED.has(route))).toEqual([]);
        // Still signed in: nothing was refused, so nothing signed it out.
        expect(localStorage.getItem('gateway_auth_token')).toBe(TOKEN);
        expect(plane.requests).not.toContain('POST /v1/admin/users/logout');
    });

    it('carries on in the same session after the change', async () => {
        await signIn();
        await changeScreen();
        fill('generated-once', 'a-new-password', 'a-new-password');
        submit();
        await waitFor(() => expect(shell()).not.toBeNull());
        expect(plane.changeBodies).toEqual([{ current_password: 'generated-once', new_password: 'a-new-password' }]);
        expect(screen.queryByRole('heading', { name: 'Change your password' })).toBeNull();
        expect(localStorage.getItem('gateway_auth_token')).toBe(TOKEN);
        expect(plane.requests).not.toContain('POST /v1/admin/users/logout');
        expect(JSON.parse(localStorage.getItem('gateway_user') ?? '{}')).toMatchObject({ password_change_required: false });
    });

    it('is taken there by the 403 of any route, when the sign-in did not say so', async () => {
        plane.loginSays = false;
        await signIn();
        await changeScreen();
        await settled();
        expect(shell()).toBeNull();
        const refusedAt = plane.requests.length;
        await settled();
        // No loop: once on the screen, nothing more is refused.
        expect(plane.requests.slice(refusedAt).filter((route) => !ALLOWED.has(route))).toEqual([]);
    });

    it('a stored session whose /me says so opens on the change-password screen', async () => {
        localStorage.setItem('gateway_auth_token', TOKEN);
        localStorage.setItem('gateway_user', JSON.stringify(TENANT_ADMIN));
        localStorage.setItem('gateway_admin_type', 'tenant');
        localStorage.setItem('gateway_tenant_id', 'acme');
        render(<AppShell composition={communityComposition()} />);
        await changeScreen();
        await settled();
        expect(plane.requests.filter((route) => !ALLOWED.has(route))).toEqual([]);
    });

    it('can sign out from the screen', async () => {
        await signIn();
        await changeScreen();
        fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
        await screen.findByPlaceholderText('Enter your username');
        expect(plane.requests).toContain('POST /v1/admin/users/logout');
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
    });

    it('a refused change says why and stays on the screen', async () => {
        await signIn();
        await changeScreen();
        fill('generated-once', 'short', 'short');
        submit();
        expect(await screen.findByText('New password must be at least 8 characters and not blank.')).toBeInTheDocument();
        expect(plane.changeBodies).toEqual([]);
        expect(shell()).toBeNull();
    });
});
