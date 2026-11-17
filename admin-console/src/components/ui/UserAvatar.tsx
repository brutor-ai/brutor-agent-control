/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { apiUrl } from '../../env';

interface UserAvatarProps {
    avatarUrl?: string | null;
    name?: string;
    size?: 'sm' | 'md' | 'lg';
    className?: string;
}

const sizeClasses = {
    sm: 'w-7 h-7 text-xs',
    md: 'w-9 h-9 text-sm',
    lg: 'w-12 h-12 text-base',
};

/**
 * Reusable avatar component. Shows the user's photo if available,
 * or falls back to an initial-letter circle.
 */
export function UserAvatar({ avatarUrl, name, size = 'md', className = '' }: UserAvatarProps) {
    const sizeClass = sizeClasses[size];
    const initial = name?.[0]?.toUpperCase() || '?';

    if (avatarUrl) {
        // Relative URLs (internal photos served by gateway-control,
        // e.g. /v1/assets/users/end-user/123/photo?h=abc) need to
        // resolve against the gateway origin, not the admin UI's
        // own origin, where they are not found. `apiUrl()` makes the
        // address from the Control Plane's address the console is
        // configured with, the one the API client uses, so it is right
        // however the console was deployed.
        const src = avatarUrl.startsWith('/') ? apiUrl(avatarUrl) : avatarUrl;
        return (
            <img
                src={src}
                alt={name || 'User'}
                className={`${sizeClass} rounded-full object-cover flex-shrink-0 ${className}`}
                onError={(e) => {
                    // Fallback to initial on broken image
                    const target = e.currentTarget;
                    target.style.display = 'none';
                    target.nextElementSibling?.classList.remove('hidden');
                }}
            />
        );
    }

    return (
        <div
            className={`${sizeClass} rounded-full bg-gradient-to-br from-blue-100 to-purple-100 flex items-center justify-center flex-shrink-0 ${className}`}
            title={name}
        >
            <span className="font-medium text-blue-700">{initial}</span>
        </div>
    );
}
