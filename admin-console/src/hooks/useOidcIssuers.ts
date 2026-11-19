/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant's OIDC identity providers, the ones an identity policy may
 * name in `allowed_issuers`. Read only when `enabled` (an administrator who
 * may edit a policy); a failed read is said, not taken for an empty list.
 */

import { useEffect, useState } from 'react';

import { useAuth } from './useAuth';
import { failureSentence } from '../lib/narrow';
import { oidcIssuers, type Issuer } from '../components/identity/policyDraft';

export function useOidcIssuers(enabled: boolean): { issuers: Issuer[]; error: string | null } {
    const { gatewayClient } = useAuth();
    const [issuers, setIssuers] = useState<Issuer[]>([]);
    const [error, setError] = useState<string | null>(null);
    useEffect(() => {
        if (!enabled) return;
        gatewayClient.fetchIdPConfigs().then(
            (answer) => {
                setIssuers(oidcIssuers(answer));
                setError(null);
            },
            (err: unknown) => {
                setIssuers([]);
                setError(failureSentence('The identity providers of this tenant could not be read', err));
            },
        );
    }, [enabled, gatewayClient]);
    return { issuers, error };
}
