/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of `TenantSettingsTab.defaultModels.test` share: the
 * tenant, the models and the knowledge-base figures the gateway answers
 * with, what was asked of it, and the fields of the sub-tab. The Community
 * half is the page with nothing contributed; the default models sub-tab is
 * the Enterprise extension's, and its cases are under `enterprise/src/host/`.
 */

import { vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

import { request, resetTenantSettingsDoubles } from './tenantSettingsTab';

export const EMBEDDING_URL = '/v1/admin/llms?mode=embedding&enabled=true';
export const JUDGE_URL = '/v1/admin/llms?mode=chat&enabled=true';

export const TENANT = {
    id: 't1',
    name: 't1',
    display_name: 'Tenant One',
    organization_name: 'Org',
    primary_contact_email: 'a@b.c',
    mfa_policy: 'optional',
    data_residency: null,
    data_retention: null,
    default_embedding_model_id: 'e1',
    default_judge_model_id: 'j1',
};

// The list endpoint answers with a map keyed by id; one model has no
// `model_name`, one is disabled.
export const EMBEDDING_MODELS = {
    models: {
        e1: { id: 'e1', model_name: 'text-embedding-3-small', provider: 'openai', enabled: true },
        e2: { id: 'e2', name: 'nomic-embed-text', provider: 'ollama' },
        e3: { id: 'e3', model_name: 'retired-embedder', provider: 'openai', enabled: false },
    },
    total: 3,
};
export const JUDGE_MODELS = {
    items: [
        { id: 'j1', model_name: 'judge-large', provider: 'anthropic', enabled: true },
        { id: 'j2', model_name: 'judge-small', provider: 'openai', enabled: true },
    ],
};
/** The volume of the tenant's knowledge base, as its statistics answer it: documents by whatever statuses they are in. */
export interface KbStats {
    document_count: number;
    chunk_count: number;
    documents_by_status: Record<string, number>;
    estimate: { hosted_seconds: number; local_seconds: number; note: string };
}
export const KB_STATS: KbStats = {
    document_count: 1200,
    chunk_count: 5400,
    documents_by_status: { indexed: 1100, pending: 100 },
    estimate: { hosted_seconds: 120, local_seconds: 5400, note: '' },
};

export interface World {
    tenant?: Record<string, unknown>;
    stats?: KbStats | null;
    embedding?: unknown;
    patch?: (body: Record<string, unknown>) => unknown;
    reembed?: () => unknown;
}

export function answer({ tenant = TENANT, stats = KB_STATS, embedding = EMBEDDING_MODELS, patch, reembed }: World = {}) {
    request.mockImplementation((path: string, options?: { method?: string; body?: string }) => {
        // The save is a PATCH of the same URL the page loads from.
        if (path === '/v1/admin/tenants/current' && options?.method !== 'PATCH') return Promise.resolve(tenant);
        if (path === EMBEDDING_URL) return embedding instanceof Error ? Promise.reject(embedding) : Promise.resolve(embedding);
        if (path === JUDGE_URL) return Promise.resolve(JUDGE_MODELS);
        if (path.endsWith('/kb-stats')) return stats ? Promise.resolve(stats) : Promise.reject(new Error('no kb'));
        if (path.endsWith('/re-embed-kb')) return Promise.resolve().then(() => (reembed ? reembed() : { documents_queued: 7 }));
        if (options?.method === 'PATCH') {
            const body = JSON.parse(options.body ?? '{}') as Record<string, unknown>;
            return Promise.resolve().then(() => (patch ? patch(body) : { ...tenant, ...body }));
        }
        return Promise.resolve({});
    });
}

export const calls = (match: (path: string, options?: { method?: string; body?: string }) => boolean) =>
    (request.mock.calls as Array<[string, { method?: string; body?: string }?]>).filter(([path, options]) => match(path, options));
export const patches = () =>
    calls((_, options) => options?.method === 'PATCH').map(([path, options]) => ({
        path,
        body: JSON.parse(options?.body ?? '{}') as Record<string, unknown>,
    }));
export const reembeds = () => calls((path) => path.endsWith('/re-embed-kb'));
export const statsRequests = () => calls((path) => path.endsWith('/kb-stats')).length;

/** The select under a label of the sub-tab (the labels are not tied to their fields). */
export function selectUnder(label: string): HTMLSelectElement {
    const select = screen.getByText(label).parentElement?.querySelector('select');
    if (!select) throw new Error(`no select under ${label}`);
    return select;
}
export const optionsOf = (select: HTMLSelectElement) => Array.from(select.options).map((o) => [o.value, o.textContent]);
export const embedding = () => selectUnder('Default embedding model');
export const judge = () => selectUnder('Default judge model');
export const saveButton = () => screen.getByRole('button', { name: /Save changes|Review changes…/ });
export const reembedButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Re-embed KB content' });
export const goTo = (label: string) => fireEvent.click(screen.getByRole('button', { name: label }));

/** How every case starts. For a `beforeEach`. */
export function resetDefaultModels(): void {
    resetTenantSettingsDoubles();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
}
