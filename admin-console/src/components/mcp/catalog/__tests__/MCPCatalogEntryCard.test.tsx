/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCPCatalogEntryCard is the unit of the catalog grid. Its job is to be
 * honest at a glance: the trust badge in particular is a governance signal
 * — a `revoked` entry must never read as verified — and the icon must
 * degrade to a monogram rather than a broken image when the remote icon
 * 404s (catalog entries carry third-party icon URLs we don't host).
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import MCPCatalogEntryCard from '../MCPCatalogEntryCard';
import type { MCPCatalogEntryBrief } from '../../../../lib/mcpCatalogTypes';

const entry = (overrides: Partial<MCPCatalogEntryBrief> = {}): MCPCatalogEntryBrief => ({
    id: 'e1',
    tenant_id: 't1',
    name: 'GitHub',
    slug: 'github',
    short_description: 'Issues, PRs and repos.',
    icon_url: null,
    has_icon: false,
    repo_url: null,
    runtime: 'remote',
    categories: ['dev', 'scm'],
    auth_pattern: 'oauth',
    source: 'system',
    enabled: true,
    created_at: null,
    updated_at: null,
    ...overrides,
});

describe('MCPCatalogEntryCard', () => {
    afterEach(() => cleanup());

    it('shows the name, description and runtime/auth badges', () => {
        render(<MCPCatalogEntryCard entry={entry()} onClick={vi.fn()} />);
        expect(screen.getByText('GitHub')).toBeTruthy();
        expect(screen.getByText('Issues, PRs and repos.')).toBeTruthy();
        expect(screen.getByText('Remote')).toBeTruthy();
        expect(screen.getByText('OAuth')).toBeTruthy();
    });

    it('labels a containerized API-key entry with its own badges', () => {
        render(
            <MCPCatalogEntryCard
                entry={entry({ runtime: 'containerized', auth_pattern: 'api_key' })}
                onClick={vi.fn()}
            />
        );
        expect(screen.getByText('Containerized')).toBeTruthy();
        expect(screen.getByText('API Key')).toBeTruthy();
    });

    it('says so plainly when an entry carries no description', () => {
        render(<MCPCatalogEntryCard entry={entry({ short_description: null })} onClick={vi.fn()} />);
        expect(screen.getByText('No description available')).toBeTruthy();
    });

    it('truncates a long description to 100 characters plus an ellipsis', () => {
        const long = 'x'.repeat(180);
        render(<MCPCatalogEntryCard entry={entry({ short_description: long })} onClick={vi.fn()} />);
        expect(screen.getByText(`${'x'.repeat(100)}...`)).toBeTruthy();
    });

    it('shows the Verified badge only for a tenant-signed entry', () => {
        const { rerender } = render(
            <MCPCatalogEntryCard entry={entry({ trust_tier: 'tenant_signed' })} onClick={vi.fn()} />
        );
        expect(screen.getByText(/Verified/)).toBeTruthy();

        rerender(<MCPCatalogEntryCard entry={entry({ trust_tier: 'unsigned' })} onClick={vi.fn()} />);
        expect(screen.queryByText(/Verified/)).toBeNull();
    });

    it('flags a revoked entry as revoked, never as verified', () => {
        render(<MCPCatalogEntryCard entry={entry({ trust_tier: 'revoked' })} onClick={vi.fn()} />);
        expect(screen.getByText('Revoked')).toBeTruthy();
        expect(screen.queryByText(/Verified/)).toBeNull();
    });

    it('shows at most three category tags and counts the rest', () => {
        render(
            <MCPCatalogEntryCard
                entry={entry({ categories: ['a', 'b', 'c', 'd', 'e'] })}
                onClick={vi.fn()}
            />
        );
        expect(screen.getByText('a')).toBeTruthy();
        expect(screen.getByText('c')).toBeTruthy();
        expect(screen.queryByText('d')).toBeNull();
        expect(screen.getByText('+2')).toBeTruthy();
    });

    it('falls back to a monogram when the remote icon fails to load', () => {
        render(
            <MCPCatalogEntryCard
                entry={entry({ icon_url: 'https://cdn.example/gh.png' })}
                onClick={vi.fn()}
            />
        );
        const img = screen.getByRole('img', { name: 'GitHub' });
        fireEvent.error(img);
        expect(screen.queryByRole('img', { name: 'GitHub' })).toBeNull();
        expect(screen.getByText('G')).toBeTruthy();
    });

    it('hands the whole entry back on click', () => {
        const onClick = vi.fn();
        const e = entry();
        render(<MCPCatalogEntryCard entry={e} onClick={onClick} />);
        fireEvent.click(screen.getByText('GitHub'));
        expect(onClick).toHaveBeenCalledWith(e);
    });
});
