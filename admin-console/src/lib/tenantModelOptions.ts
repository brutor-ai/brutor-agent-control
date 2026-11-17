/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The models the tenant settings page offers as a tenant-wide default
 * (embedding model, judge model), read from the answer of the LLM list
 * endpoint.
 */

import { getString } from './narrow';

export interface TenantModelOption {
    id: string;
    model_name: string;
    provider: string;
    enabled: boolean;
}

const property = (value: unknown, key: string): unknown =>
    value !== null && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined;

/**
 * The enabled models of an answer of `/v1/admin/llms`. The endpoint answers
 * `{ models: { <id>: {...} }, total }`, a map keyed by id; a list, under
 * `models` or `items` or bare, is read the same way.
 */
export function toModelOptions(answer: unknown): TenantModelOption[] {
    const raw = property(answer, 'models') ?? property(answer, 'items') ?? answer;
    const list: unknown[] = Array.isArray(raw) ? raw : raw !== null && typeof raw === 'object' ? Object.values(raw) : [];
    return list
        .filter((model) => property(model, 'enabled') !== false)
        .map((model) => ({
            id: getString(model, 'id') ?? '',
            model_name: getString(model, 'model_name') || getString(model, 'name') || '',
            provider: getString(model, 'provider') || '',
            enabled: true,
        }));
}
