/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The logged-in admin's username, or null when no auth provider is mounted.
 *
 * For panels that only *adapt* to who is looking — default the approver to
 * them, disable a re-attest button for the person already on the version —
 * and must still render standalone in tests. `useAuth` throws without a
 * provider by design; this is the tolerant reading of the same context.
 */

import { useAuth } from './useAuth';

export function useAdminUsername(): string | null {
    try {
        return useAuth().user?.username ?? null;
    } catch {
        return null;
    }
}
