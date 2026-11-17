/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The sign-in of the Community console, wherever it is opened: the console's own form, with a user name and a password. No
 * tenant id is asked, the URL cannot ask for another form, and nothing says
 * "Multi-Tenant Edition".
 *
 * The second block holds the seam: an extension's sign-in item (slot
 * `app.sign-in`; a fake one here) adds its tenant field and footer to that
 * form, wherever the console is opened.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

vi.mock('reactflow/dist/style.css', () => ({}));
vi.mock('../../components/BrutorLogo', () => ({ BrutorLogo: () => null }));

import App from '../../App';
import { ADMIN_EXTENSION_INTERFACE_VERSION, type AdminExtension } from '../extensions';
import { FAKE_SIGN_IN_SLOTS } from '../../test/fakeSignIn';

interface HappyWindow {
    happyDOM: { setURL: (url: string) => void };
}
const open = (url: string): void => {
    (window as unknown as HappyWindow).happyDOM.setURL(url);
    expect(window.location.href).toBe(url);
};

const PLACES: readonly string[] = [
    'http://localhost:3002/',
    'http://localhost:3002/?saas',
    'http://localhost:3002/?tenant=acme',
    'http://localhost:3002/?tenant=acme&tenant_name=Acme%20Corp&user=bob%40acme.test',
    'http://localhost:3002/admin',
    'http://localhost:3002/admin?tenant=acme',
    'https://console.acme.example/',
    'https://console.acme.example/?tenant=acme',
    'https://console.acme.example/admin',
];

let original = '';

beforeEach(() => {
    original = window.location.href;
    localStorage.clear();
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify({ detail: 'Not authenticated' }), { status: 401, headers: { 'Content-Type': 'application/json' } })),
    );
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    (window as unknown as HappyWindow).happyDOM.setURL(original);
    localStorage.clear();
});

describe('the Community console signs in on its own form, wherever it is opened', () => {
    it.each(PLACES)('%s', async (url) => {
        open(url);
        render(<App />);
        // The console's own form, not the hosted tenant's ("Enter your email or username").
        await screen.findByPlaceholderText('Enter your username');
        expect([...document.querySelectorAll('input')].map((input) => input.id)).toEqual(['username', 'password']);
        expect(screen.getByRole('button', { name: 'Sign in as Tenant Admin' })).toBeInTheDocument();
        const text = document.body.textContent ?? '';
        expect(text).not.toMatch(/tenant id/i);
        expect(text).not.toMatch(/multi-tenant/i);
        expect(text).not.toMatch(/edition/i);
        expect(text).not.toMatch(/acme/i);
        expect(screen.queryByText('Administrator Type')).toBeNull();
    });
});

describe('an extension that contributes a sign-in item', () => {
    const extension: AdminExtension = { name: 'fake-sign-in', interfaceVersion: ADMIN_EXTENSION_INTERFACE_VERSION, slots: FAKE_SIGN_IN_SLOTS };
    const extensions = [extension];

    it('the console\'s own form is shown, with the item\'s tenant field and footer', async () => {
        open('http://localhost:3002/');
        render(<App extensions={extensions} />);
        expect(await screen.findByPlaceholderText('Enter tenant ID')).toHaveValue('first');
        expect(screen.getByPlaceholderText('Enter your username')).toBeInTheDocument();
        expect(screen.getByText('Fake footer line')).toBeInTheDocument();
    });

    it.each(['http://localhost:3002/admin?fake-hosted', 'https://console.acme.example/?tenant=acme'])('the form at %s is the console\'s own, with the item\'s tenant field', async (url) => {
        open(url);
        render(<App extensions={extensions} />);
        expect(await screen.findByPlaceholderText('Enter tenant ID')).toHaveValue('first');
        expect(screen.getByPlaceholderText('Enter your username')).toBeInTheDocument();
    });
});
