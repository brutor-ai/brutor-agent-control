/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The name the console goes by before the application is on screen: the page
 * title of the Community page, and the loading screen and the error screen
 * of the entry point (`mount.tsx`, shared by both editions), and the title
 * the console gives the tab once it runs. The shared code takes the name
 * from the page it is mounted in (`<meta name="title">`, which each
 * edition's `index.html` sets and nothing changes afterwards).
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TenantThemeProvider } from '../../context/TenantThemeContext';
import { ErrorBoundary, LoadingScreen } from '../MountParts';
import { consoleName, PRODUCT_NAME, productName } from '../productName';

const session = { isAuthenticated: false, tenantId: null as string | null, companyName: null as string | null };

vi.mock('../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchCurrentTenantThemeVariants: () => Promise.resolve({ company_name: session.companyName, variants: {} }),
    }),
}));
vi.mock('../../hooks/useAuth', () => ({
    useAuth: () => ({
        user: session.isAuthenticated ? { id: 'u1', username: 'admin', admin_type: 'tenant_admin', tenant_id: session.tenantId } : null,
        isAuthenticated: session.isAuthenticated,
        getCurrentTenantId: () => session.tenantId,
    }),
}));
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }));

/** Mount in a page of the given name, as that edition's `index.html` declares it. */
function pageNamed(name: string) {
    const meta = document.createElement('meta');
    meta.setAttribute('name', 'title');
    meta.setAttribute('content', name);
    document.head.appendChild(meta);
    document.title = name;
}

const COMMUNITY = 'Brutor Agent Control Admin Console';
const ENTERPRISE = 'Brutor Agent Control Enterprise Admin Console';
const OLD_NAMES = /AI Gateway Admin|AI Control Plane Admin|Brutor AI Gateway/;

const Broken = (): React.ReactElement => {
    throw new Error('boom');
};

beforeEach(() => {
    Object.assign(session, { isAuthenticated: false, tenantId: null, companyName: null });
    localStorage.clear();
    vi.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
    document.head.querySelectorAll('meta[name="title"]').forEach((meta) => meta.remove());
    document.title = '';
    vi.restoreAllMocks();
});

describe('the name of the console', () => {
    it('is the title of the Community page, and no earlier name is on it', () => {
        const page = readFileSync(path.resolve(__dirname, '../../../index.html'), 'utf8');
        expect(page).toContain(`<title>${COMMUNITY}</title>`);
        expect(page).toContain(`<h2 class="loading-title">${COMMUNITY}</h2>`);
        expect(page).not.toMatch(OLD_NAMES);
    });

    it('is the product\'s name in the web manifest, which both editions serve', () => {
        const manifest = JSON.parse(readFileSync(path.resolve(__dirname, '../../../public/site.webmanifest'), 'utf8')) as { name: string };
        expect(manifest.name).toBe(PRODUCT_NAME);
        expect(PRODUCT_NAME).toBe('Brutor Agent Control');
    });

    it('names the product by the edition of the page: the console name without "Admin Console"', () => {
        expect(productName()).toBe('Brutor Agent Control');
        pageNamed(ENTERPRISE);
        expect(productName()).toBe('Brutor Agent Control Enterprise');
    });

    it('is the default theme\'s company name, and the logo\'s when a theme names none', () => {
        const theme = readFileSync(path.resolve(__dirname, '../../context/TenantThemeContext.tsx'), 'utf8');
        const logo = readFileSync(path.resolve(__dirname, '../../components/BrutorLogo.tsx'), 'utf8');
        expect(theme).toContain('company_name: PRODUCT_NAME');
        expect(logo).toContain('theme?.company_name || PRODUCT_NAME');
    });

    it.each([COMMUNITY, ENTERPRISE])('the loading screen shows the title of its page (%s)', (title) => {
        pageNamed(title);
        render(<LoadingScreen />);
        expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(new RegExp(`^${title}$`));
    });

    it('the loading screen of a page with no title shows the Community name', () => {
        render(<LoadingScreen />);
        expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(new RegExp(`^${COMMUNITY}$`));
    });

    it.each([COMMUNITY, ENTERPRISE])('the error screen names the console by the title of its page (%s)', (title) => {
        pageNamed(title);
        // The tab's title may by then be a tenant's: the name is still the console's.
        document.title = 'Acme - Admin';
        const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
        render(
            <ErrorBoundary>
                <Broken />
            </ErrorBoundary>,
        );
        expect(screen.getByText(`The ${title} encountered an unexpected error. Please try refreshing the page.`)).toBeInTheDocument();
        expect(logged.mock.calls.some((call) => call[0] === `${title} Error:`)).toBe(true);
        expect(document.body.textContent).not.toMatch(OLD_NAMES);
    });

    it('is read from the page, and is the Community name where the page says nothing', () => {
        expect(consoleName()).toBe(COMMUNITY);
        pageNamed(ENTERPRISE);
        expect(consoleName()).toBe(ENTERPRISE);
    });
});

describe('the title of the tab while the console runs', () => {
    const mount = () =>
        render(
            <TenantThemeProvider>
                <span>console</span>
            </TenantThemeProvider>,
        );

    it.each([COMMUNITY, ENTERPRISE])('before sign-in it stays the name of the console (%s)', async (name) => {
        pageNamed(name);
        mount();
        await screen.findByText('console');
        await waitFor(() => expect(document.title).toBe(name));
        expect(document.title).not.toMatch(OLD_NAMES);
    });

    it('for a tenant that has set no company name it is the name of the console', async () => {
        pageNamed(ENTERPRISE);
        document.title = 'something else';
        Object.assign(session, { isAuthenticated: true, tenantId: 't1' });
        mount();
        await waitFor(() => expect(document.title).toBe(ENTERPRISE));
    });

    it('for a tenant with a company name it is that name', async () => {
        pageNamed(COMMUNITY);
        Object.assign(session, { isAuthenticated: true, tenantId: 't1', companyName: 'Acme' });
        mount();
        await waitFor(() => expect(document.title).toBe('Acme - Admin'));
    });
});
