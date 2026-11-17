/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Renders a provider's icon — either the bytes served by
 * `GET /v1/admin/providers/{id}/icon`, or an initials-based
 * fallback when the row has no icon yet.
 *
 * Why a dedicated component
 * -------------------------
 * Multiple surfaces show provider chips: the LLModelForm header,
 * the (forthcoming) Providers admin page, and the model picker
 * dropdown in the portal. Centralising the icon-fetch + fallback
 * logic here means a single place to fix when we add a `srcSet`
 * for retina, swap to `<picture>`, or switch the fallback shape.
 */
import React from 'react';

import type { GatewayApiClient } from '../../../lib/gatewayApiClient';
import type { LlmProviderResponse } from '../../../lib/llmProviderTypes';

export interface ProviderIconProps {
    provider: Pick<
        LlmProviderResponse,
        'id' | 'key' | 'display_name' | 'has_icon' | 'icon_uploaded_at'
    >;
    /** API client — used to construct the icon URL. */
    api: GatewayApiClient | null;
    /** Pixel size of the rendered square. Defaults to 24. */
    size?: number;
    className?: string;
}

/** Lazy palette for the initials fallback. Keeps the colour stable
 *  per provider key so the same fallback always looks the same. */
const FALLBACK_PALETTE = [
    '#6366f1', // indigo
    '#0ea5e9', // sky
    '#10b981', // emerald
    '#f59e0b', // amber
    '#ef4444', // red
    '#8b5cf6', // violet
    '#ec4899', // pink
    '#14b8a6', // teal
];

function paletteIndex(key: string): number {
    let hash = 0;
    for (let i = 0; i < key.length; i++) {
        // Cheap, stable hash — we only need 0..7.
        hash = (hash * 31 + key.charCodeAt(i)) | 0;
    }
    return Math.abs(hash) % FALLBACK_PALETTE.length;
}

function initialsFromKey(key: string, displayName: string): string {
    // Prefer initials of the display name if it splits nicely.
    const parts = displayName
        .replace(/\(.+?\)/g, '')
        .trim()
        .split(/\s+/)
        .filter(Boolean);
    if (parts.length >= 2) {
        return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    if (key.length >= 2) {
        return key.slice(0, 2).toUpperCase();
    }
    return key.toUpperCase();
}

export const ProviderIcon: React.FC<ProviderIconProps> = ({
    provider,
    api,
    size = 24,
    className,
}) => {
    const dimension = `${size}px`;
    const radius = `${Math.round(size * 0.2)}px`;
    const baseStyle: React.CSSProperties = {
        width: dimension,
        height: dimension,
        borderRadius: radius,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
        overflow: 'hidden',
    };

    if (provider.has_icon && api) {
        const url = api.getProviderIconUrl(
            provider.id,
            provider.icon_uploaded_at ?? undefined,
        );
        return (
            <img
                src={url}
                alt={provider.display_name}
                width={size}
                height={size}
                style={{ ...baseStyle, objectFit: 'contain', background: 'var(--color-surface)' }}
                className={className}
                loading="lazy"
            />
        );
    }

    // Fallback: solid colour square with the provider's initials.
    // Stable per `key` so re-renders don't flip the colour.
    const bg = FALLBACK_PALETTE[paletteIndex(provider.key)];
    return (
        <span
            aria-label={provider.display_name}
            title={provider.display_name}
            className={className}
            style={{
                ...baseStyle,
                background: bg,
                color: 'var(--color-on-accent)',
                fontSize: `${Math.round(size * 0.42)}px`,
                fontWeight: 600,
                letterSpacing: '-0.02em',
            }}
        >
            {initialsFromKey(provider.key, provider.display_name)}
        </span>
    );
};

export default ProviderIcon;
