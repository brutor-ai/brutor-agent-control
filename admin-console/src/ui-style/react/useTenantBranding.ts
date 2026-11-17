/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useContext } from 'react';

import { TenantBrandingContext, type TenantBrandingContextValue } from './TenantBrandingProvider';

export function useTenantBranding(): TenantBrandingContextValue {
    const ctx = useContext(TenantBrandingContext);
    if (!ctx) throw new Error('useTenantBranding must be used within TenantBrandingProvider');
    return ctx;
}
