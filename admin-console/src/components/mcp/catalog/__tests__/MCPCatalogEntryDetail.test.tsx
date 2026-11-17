/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCPCatalogEntryDetail is the last screen before an operator imports a
 * third-party server definition into their tenant. It has to disclose what
 * the import will actually pull in: which env vars are required, which of
 * them are secrets, and whether the entry is a remote URL or a container
 * image. Rendering the *wrong* runtime block (or none) is the failure mode
 * these tests guard, along with the import callback carrying the full entry.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';

import MCPCatalogEntryDetail from '../MCPCatalogEntryDetail';
import type { MCPCatalogEntry } from '../../../../lib/mcpCatalogTypes';

const entry = (overrides: Partial<MCPCatalogEntry> = {}): MCPCatalogEntry => ({
    id: 'e1',
    tenant_id: 't1',
    name: 'GitHub',
    slug: 'github',
    short_description: 'Issues, PRs and repos.',
    description: 'A long form description.',
    icon_url: null,
    has_icon: false,
    repo_url: null,
    runtime: 'remote',
    categories: ['dev'],
    auth_pattern: 'oauth',
    source: 'system',
    enabled: true,
    created_at: null,
    updated_at: null,
    remote_config: { fixedURL: 'https://mcp.github.com', headers: [] },
    containerized_config: null,
    env_vars: [],
    tool_preview: [],
    metadata_extra: null,
    ...overrides,
});

describe('MCPCatalogEntryDetail', () => {
    afterEach(() => cleanup());

    it('shows the entry identity, badges and long description', () => {
        render(<MCPCatalogEntryDetail entry={entry()} onImport={vi.fn()} />);
        expect(screen.getByText('GitHub')).toBeTruthy();
        expect(screen.getByText('A long form description.')).toBeTruthy();
        expect(screen.getByText('Remote')).toBeTruthy();
        expect(screen.getByText('OAuth')).toBeTruthy();
        expect(screen.getByText('System')).toBeTruthy();
    });

    it('renders the remote config for a remote entry and no container block', () => {
        render(<MCPCatalogEntryDetail entry={entry()} onImport={vi.fn()} />);
        expect(screen.getByText('https://mcp.github.com')).toBeTruthy();
        expect(screen.queryByText('Container Configuration')).toBeNull();
    });

    it('renders the container config for a containerized entry instead', () => {
        render(
            <MCPCatalogEntryDetail
                entry={entry({
                    runtime: 'containerized',
                    remote_config: null,
                    containerized_config: {
                        image: 'ghcr.io/acme/mcp:1.2',
                        port: 8080,
                        path: '/mcp',
                        args: ['--verbose', '--json'],
                    },
                })}
                onImport={vi.fn()}
            />
        );
        expect(screen.getByText('Container Configuration')).toBeTruthy();
        expect(screen.getByText('ghcr.io/acme/mcp:1.2')).toBeTruthy();
        expect(screen.getByText('--verbose --json')).toBeTruthy();
        expect(screen.queryByText('Remote Configuration')).toBeNull();
    });

    it('discloses which environment variables are required and which are secrets', () => {
        render(
            <MCPCatalogEntryDetail
                entry={entry({
                    env_vars: [
                        {
                            key: 'GITHUB_TOKEN',
                            name: 'Token',
                            required: true,
                            sensitive: true,
                            description: 'PAT with repo scope',
                        },
                        {
                            key: 'GITHUB_ORG',
                            name: 'Org',
                            required: false,
                            sensitive: false,
                            description: 'Default org',
                        },
                    ],
                })}
                onImport={vi.fn()}
            />
        );
        expect(screen.getByText('Environment Variables (2)')).toBeTruthy();

        const tokenRow = screen.getByText('GITHUB_TOKEN').closest('tr');
        expect(within(tokenRow as HTMLElement).getByText('Yes')).toBeTruthy();

        const orgRow = screen.getByText('GITHUB_ORG').closest('tr');
        // Not required, not sensitive — both columns read "No".
        expect(within(orgRow as HTMLElement).getAllByText('No')).toHaveLength(2);
    });

    it('previews the tools the import would bring in', () => {
        render(
            <MCPCatalogEntryDetail
                entry={entry({
                    tool_preview: [
                        { name: 'search_issues', description: 'Search issues', params: {} },
                        { name: 'create_pr', description: 'Open a PR', params: {} },
                    ],
                })}
                onImport={vi.fn()}
            />
        );
        expect(screen.getByText('Tools (2)')).toBeTruthy();
        expect(screen.getByText('search_issues')).toBeTruthy();
        expect(screen.getByText('Open a PR')).toBeTruthy();
    });

    it('omits the optional sections an entry does not populate', () => {
        render(
            <MCPCatalogEntryDetail
                entry={entry({ description: null, categories: [], repo_url: null })}
                onImport={vi.fn()}
            />
        );
        expect(screen.queryByText('Description')).toBeNull();
        expect(screen.queryByText('Categories')).toBeNull();
        expect(screen.queryByText('Repository')).toBeNull();
        expect(screen.queryByText(/^Tools \(/)).toBeNull();
    });

    it('links the repository out safely in a new tab', () => {
        render(
            <MCPCatalogEntryDetail
                entry={entry({ repo_url: 'https://github.com/acme/mcp' })}
                onImport={vi.fn()}
            />
        );
        const link = screen.getByRole('link', { name: /github.com\/acme\/mcp/ });
        expect(link.getAttribute('target')).toBe('_blank');
        expect(link.getAttribute('rel')).toContain('noopener');
    });

    it('hands the full entry to the import handler', () => {
        const onImport = vi.fn();
        const e = entry();
        render(<MCPCatalogEntryDetail entry={e} onImport={onImport} />);
        fireEvent.click(screen.getByRole('button', { name: /import to servers/i }));
        expect(onImport).toHaveBeenCalledWith(e);
    });

    it('offers Back only when the caller can go back', () => {
        const onBack = vi.fn();
        const { rerender } = render(
            <MCPCatalogEntryDetail entry={entry()} onImport={vi.fn()} onBack={onBack} />
        );
        fireEvent.click(screen.getByRole('button', { name: /back to list/i }));
        expect(onBack).toHaveBeenCalledTimes(1);

        rerender(<MCPCatalogEntryDetail entry={entry()} onImport={vi.fn()} />);
        expect(screen.queryByRole('button', { name: /back to list/i })).toBeNull();
    });
});
