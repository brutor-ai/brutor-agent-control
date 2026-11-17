/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { getProviderInfo } from './providerInfo';

import { useGateway } from '../../../hooks/useGateway';
import { useLlmProviders } from '../../../hooks/useLlmProviders';

/*
 * Provider logo rendering.
 *
 * Source of truth for provider icons is the database, seeded from
 * `brutor-gateway-control/app/database/provider_icons/*.svg` on
 * tenant provisioning. The admin and portal UIs ship no
 * bundled icon files — `<ProviderLogo>` always fetches via
 * `getProviderIconUrl(...)` and falls back to a coloured-letter
 * box when the catalogue row has no icon yet (operator-uploaded
 * provider, or seeding hasn't run).
 *
 * `PROVIDER_INFO` is kept as a small static registry of slug →
 * `displayName` + `fallbackColor` so the search/filter dropdowns
 * have a stable list of well-known providers without hitting the
 * API on cold load. The list mirrors `PROVIDER_SEED_CATALOG` in
 * `init_llm_providers.py`.
 */


/**
 * Provider info registry — stable list of well-known slugs used
 * by the search/filter dropdowns. Keys match the seed catalogue
 * in `init_llm_providers.py`. Adding a new provider here is a
 * UI nicety only: the runtime dropdown in `LLModelForm` reads
 * the live `useLlmProviders()` list, so a brand-new operator-
 * created slug shows up there automatically.
 */


interface ProviderLogoProps {
    provider: string;
    size?: 'xs' | 'sm' | 'md' | 'lg';
    className?: string;
}

const sizeMap = {
    xs: 'w-3 h-3',
    sm: 'w-4 h-4',
    md: 'w-5 h-5',
    lg: 'w-6 h-6',
};

/**
 * ProviderLogo component
 *
 * Resolution order:
 *   1. Operator-managed icon served by the gateway
 *      (`GET /v1/admin/providers/{id}/icon`) — picked up via the
 *      `useLlmProviders` cache, keyed on the provider slug. Single
 *      source of truth: the seed catalogue
 *      (`app/database/provider_icons/*.svg`) is loaded into the
 *      DB on tenant provisioning.
 *   2. Coloured-letter fallback when the row has no icon yet —
 *      e.g. operator created a custom slug and hasn't uploaded a
 *      mark, or the seed hasn't run on this tenant.
 *
 * Usage:
 * <ProviderLogo provider="openai" size="md" />
 */
export const ProviderLogo: React.FC<ProviderLogoProps> = ({
    provider,
    size = 'sm',
    className = '',
}) => {
    const [imageError, setImageError] = React.useState(false);
    const { gatewayClient } = useGateway();
    // The hook is WeakMap-cached per client and has a 60s SWR
    // window, so calling it from every model row in a long list
    // is effectively free after the first fetch.
    const { providers } = useLlmProviders(gatewayClient);
    const liveRow = React.useMemo(
        () => providers.find(
            p => p.key.toLowerCase() === (provider ?? '').toLowerCase(),
        ) ?? null,
        [providers, provider],
    );

    const providerInfo = getProviderInfo(provider);
    const sizeClass = sizeMap[size];

    // Reset the error flag if the source URL changes — otherwise a
    // transient blip would suppress a fresh upload for the rest of
    // the session.
    const iconKey = liveRow?.has_icon
        ? `gw:${liveRow.id}:${liveRow.icon_uploaded_at ?? ''}`
        : 'none';
    React.useEffect(() => {
        setImageError(false);
    }, [iconKey]);

    if (liveRow?.has_icon && gatewayClient && !imageError) {
        const url = gatewayClient.getProviderIconUrl(
            liveRow.id,
            liveRow.icon_uploaded_at ?? undefined,
        );
        return (
            <img
                src={url}
                alt={`${liveRow.display_name} logo`}
                className={`${sizeClass} ${className} object-contain`}
                onError={() => setImageError(true)}
            />
        );
    }

    // Fallback: coloured square with the provider's first letter.
    const label = liveRow?.display_name ?? providerInfo.displayName;
    return (
        <div
            className={`${sizeClass} ${className} rounded flex items-center justify-center text-white font-bold text-xs`}
            style={{ backgroundColor: providerInfo.fallbackColor }}
        >
            {label.charAt(0)}
        </div>
    );
};

export default ProviderLogo;
