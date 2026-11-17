/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * InfoBanner — canonical "page-top notice" component.
 *
 * The reference styling is the banner on the
 * **MCP / VMCP Server Access** page (see ServerConfigTab.tsx).
 * All informational banners across the admin UI should use this
 * component so that:
 *
 *   1. Light + dark themes render consistently — the colour
 *      tokens (`bg-blue-50` etc.) are remapped in
 *      `src/styles/globals.css` under `[data-theme="dark"]`.
 *   2. The shape is uniform (icon position, padding, radius).
 *   3. Future visual changes happen in one place.
 *
 * Variants:
 *
 *   - **info** (default) — neutral explanatory text. Blue.
 *   - **warning** — operator-actionable caution. Amber.
 *   - **error** — destructive/blocking notice. Red.
 *   - **success** — confirmation. Green.
 *
 * Callers compose the body as children, so the banner can carry
 * inline `<code>`, links, or nested paragraphs.
 */

import React from 'react';
import { AlertCircle, AlertTriangle, CheckCircle, Info } from 'lucide-react';

export type InfoBannerVariant = 'info' | 'warning' | 'error' | 'success';

interface InfoBannerProps {
    title?: string;
    children?: React.ReactNode;
    variant?: InfoBannerVariant;
    /** Extra Tailwind classes on the outer container. Use sparingly
     * (the `mb-6` default is what most pages want); reach for this
     * for tightly-packed dialog layouts. */
    className?: string;
    /** Optional icon override. If omitted, the variant's default
     * icon is rendered. Useful when a page wants a domain-specific
     * glyph (e.g. `Layers` for VMCPs) while keeping the canonical
     * shape + tokens. */
    icon?: React.ReactNode;
}

interface VariantTokens {
    container: string;
    iconText: string;
    bodyText: string;
    defaultIcon: React.ComponentType<{ className?: string }>;
}

const VARIANTS: Record<InfoBannerVariant, VariantTokens> = {
    info: {
        container: 'bg-blue-50 border-blue-200',
        iconText: 'text-blue-600',
        bodyText: 'text-blue-700',
        defaultIcon: Info,
    },
    warning: {
        container: 'bg-amber-50 border-amber-200',
        iconText: 'text-amber-600',
        bodyText: 'text-amber-700',
        defaultIcon: AlertTriangle,
    },
    error: {
        container: 'bg-red-50 border-red-200',
        iconText: 'text-red-600',
        bodyText: 'text-red-700',
        defaultIcon: AlertCircle,
    },
    success: {
        container: 'bg-green-50 border-green-200',
        iconText: 'text-green-600',
        bodyText: 'text-green-700',
        defaultIcon: CheckCircle,
    },
};

const InfoBanner: React.FC<InfoBannerProps> = ({
    title,
    children,
    variant = 'info',
    className,
    icon,
}) => {
    const tokens = VARIANTS[variant];
    const IconComponent = tokens.defaultIcon;

    return (
        <div
            className={[
                'mb-6 p-4 border rounded-md',
                tokens.container,
                className ?? '',
            ].filter(Boolean).join(' ')}
        >
            <div className="flex items-start space-x-2">
                {icon ?? (
                    <IconComponent
                        className={`w-5 h-5 ${tokens.iconText} flex-shrink-0 mt-0.5`}
                    />
                )}
                <div className={`text-sm ${tokens.bodyText} flex-1 min-w-0 min-h-0`}>
                    {title && (
                        <p className="font-medium mb-1">{title}</p>
                    )}
                    {children}
                </div>
            </div>
        </div>
    );
};

export default InfoBanner;
