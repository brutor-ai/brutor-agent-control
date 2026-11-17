/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { useTenantTheme } from '../context/TenantThemeContext';
import { useTheme } from '../hooks/useTheme';
import { PRODUCT_NAME } from '../app/productName';

// Bundled (hashed) assets: a redesigned logo ships under a new URL
// automatically, so browsers cannot pin a stale cached copy the way
// they could with a fixed public path.
import logoLight from '../assets/brutor-logo.png';
import logoDark from '../assets/brutor-logo-dark.png';
import mascotLight from '../assets/brutor-mascot.png';
import mascotDark from '../assets/brutor-mascot-dark.png';

interface BrutorLogoProps {
    size?: 'small' | 'medium' | 'large';
    showText?: boolean;
    className?: string;
    /** 'dark' = cream artwork for gradient/dark surfaces (default,
     *  matching existing placements); 'light' = navy artwork for white
     *  cards like the login form; 'auto' = follow the console's
     *  light/dark mode, for surfaces that themselves flip with it
     *  (`bg-surface-raised` cards such as the login form, where a fixed
     *  navy lockup disappeared into the dark card). */
    variant?: 'light' | 'dark' | 'auto';
    /** Square mascot mark only (no wordmark) — for narrow spots like
     *  the collapsed sidebar rail where the wide lockup cannot fit. */
    mark?: boolean;
}

// The lockup is ~3.8:1 wide; rendering by height keeps every placement
// proportionate, where a fixed square scaled up would overflow its box.
const LOCKUP_HEIGHTS = { small: 32, medium: 40, large: 48 } as const;
const MARK_SIZES = { small: 36, medium: 44, large: 56 } as const;

export const BrutorLogo: React.FC<BrutorLogoProps> = ({
    size = 'medium',
    showText = false,
    className = '',
    variant = 'dark',
    mark = false
}) => {
    const { theme } = useTenantTheme();
    const { theme: mode } = useTheme();
    const [imageError, setImageError] = useState(false);

    // 'auto' resolves against the mode: a dark console needs the cream
    // artwork, a light one the navy artwork.
    const resolved = variant === 'auto' ? (mode === 'dark' ? 'dark' : 'light') : variant;

    const textSizeClasses = {
        small: 'text-sm',
        medium: 'text-base',
        large: 'text-xl'
    };

    const defaultLogo = mark
        ? (resolved === 'light' ? mascotLight : mascotDark)
        : (resolved === 'light' ? logoLight : logoDark);
    // A tenant-supplied logo_url wins until it fails to load.
    const logoUrl = theme?.logo_url && !imageError ? theme.logo_url : defaultLogo;
    const companyName = theme?.company_name || PRODUCT_NAME;

    return (
        <div className={`flex items-center space-x-2 ${className}`}>
            <img
                src={logoUrl}
                alt={`${companyName} Logo`}
                style={mark
                    ? { height: `${MARK_SIZES[size]}px`, width: `${MARK_SIZES[size]}px`, objectFit: 'contain', display: 'block' }
                    : { height: `${LOCKUP_HEIGHTS[size]}px`, width: 'auto', display: 'block' }}
                onError={() => setImageError(true)}
            />
            {showText && (
                <span className={`font-bold text-default ${textSizeClasses[size]} tracking-tight`}>
                    {companyName}
                </span>
            )}
        </div>
    );
};
