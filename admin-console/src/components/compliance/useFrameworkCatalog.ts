/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The catalog summary of one framework — dated versions, role and
 * classification vocabularies, deadline rules and retention floors by
 * classification.
 *
 * The registry list does not carry the vocabularies, so this reads the
 * `framework` block of `GET /compliance/overview` (the CP caches the
 * evaluation for a minute) and memoises it per framework for the session:
 * a vocabulary does not change while the console is open.
 */

import { useCallback } from 'react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import type { ComplianceFrameworkSummary } from '../../lib/complianceEvidenceTypes';

const cache = new Map<string, Promise<ComplianceFrameworkSummary | null>>();

/** Test hook: forget memoised summaries. */
export function clearFrameworkCatalogCache(): void {
    cache.clear();
}

export function useFrameworkCatalog(framework: string, hasCatalog: boolean) {
    const { gatewayClient } = useGateway();
    const loader = useCallback((): Promise<ComplianceFrameworkSummary | null> => {
        if (!hasCatalog) return Promise.resolve(null);
        let pending = cache.get(framework);
        if (!pending) {
            pending = gatewayClient.getComplianceOverview(framework).then((o) => o.framework);
            // A failure must not be memoised — the next mount retries.
            void pending.catch(() => cache.delete(framework));
            cache.set(framework, pending);
        }
        return pending;
    }, [gatewayClient, framework, hasCatalog]);
    return useKeyedLoad(`catalog:${framework}:${hasCatalog}`, loader, 'Failed to load the framework catalog');
}
