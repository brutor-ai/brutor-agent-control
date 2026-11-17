/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tenant A2A signing-key viewer.
 *
 * Shows the tenant's Ed25519 public key (from `tenant_a2a_keys`),
 * the JWKS discovery URL external verifiers should fetch, and
 * copy-to-clipboard affordances. Rotation is deferred —
 * there's no delete/rotate action here on purpose.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    Check,
    Copy,
    KeyRound,
    Link2,
    Loader2,
    RefreshCw,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import InfoBanner from '../common/InfoBanner';
import type { TenantA2APublicKeyResponse } from '../../lib/a2aTypes';
import { formatDate } from './a2aFormatters';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

const A2ATenantKeyTab: React.FC = () => {
    const { gatewayClient } = useAuth();

    const [data, setData] = useState<TenantA2APublicKeyResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setError(null);
        try {
            const resp = await gatewayClient.getA2ATenantPublicKey();
            setData(resp);
        } catch (e) {
            setError((e as Error)?.message ?? 'Failed to load tenant key');
        }
    }, [gatewayClient]);

    useEffect(() => {
        setLoading(true);
        runBackground(load().finally(() => setLoading(false)), 'load');
    }, [load]);

    async function reload() {
        setRefreshing(true);
        try {
            await load();
        } finally {
            setRefreshing(false);
        }
    }

    // JWKS URL — relative to the origin so ops can resolve it however
    // the tenant's proxy is deployed. The Rust proxy serves this at
    // /.well-known/a2a-jwks/<tenant_id>.
    const jwksUrl = data ? `/.well-known/a2a-jwks/${data.tenant_id}` : '';

    return (
        <div className="p-6 space-y-6">
            {/* ============ HEADER ============ */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div
                        className={`p-2.5 rounded-xl ${
                            data ? 'bg-primary-100' : 'bg-surface-raised'
                        }`}
                    >
                        <KeyRound
                            className={`w-7 h-7 ${
                                data ? 'text-primary-600' : 'text-muted'
                            }`}
                        />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">
                            Tenant Signing Key
                        </h1>
                        <p className="text-sm text-muted">
                            Ed25519 key that signs this tenant's A2A Agent Cards.
                        </p>
                    </div>
                </div>

                <button
                    onClick={asyncHandler(reload, 'click')}
                    disabled={refreshing}
                    className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                    title="Refresh"
                >
                    <RefreshCw
                        className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`}
                    />
                </button>
            </div>

            {/* ============ INFO ============ */}
            <InfoBanner title="Public key distribution">
                <p>
                    External verifiers fetch the public key at the JWKS URL
                    below (RFC 7517). The private key lives encrypted on the
                    gateway and never leaves it. Key rotation is not available yet. When a key is rotated, cards signed with the previous key need re-signing.
                </p>
            </InfoBanner>

            {loading ? (
                <div className="flex items-center justify-center py-16">
                    <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                </div>
            ) : error ? (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            ) : data ? (
                <div className="bg-surface border border-default rounded-lg shadow-sm divide-y divide-default">
                    <KeyRow
                        label="Public key"
                        value={data.public_key}
                        mono
                        copyable
                    />
                    <KeyRow
                        label="Algorithm"
                        value={data.key_algorithm}
                        mono
                    />
                    <KeyRow
                        label="JWKS URL"
                        value={jwksUrl}
                        icon={<Link2 className="w-4 h-4" />}
                        mono
                        copyable
                        link
                    />
                    <KeyRow
                        label="Created"
                        value={formatDate(data.created_at)}
                    />
                    {data.rotated_at && (
                        <KeyRow
                            label="Last rotated"
                            value={formatDate(data.rotated_at)}
                        />
                    )}
                </div>
            ) : null}
        </div>
    );
};

export default A2ATenantKeyTab;

// ─── Sub-components ─────────────────────────────────────────────────────────

const KeyRow: React.FC<{
    label: string;
    value: string;
    mono?: boolean;
    copyable?: boolean;
    link?: boolean;
    icon?: React.ReactNode;
}> = ({ label, value, mono, copyable, link, icon }) => {
    const [copied, setCopied] = useState(false);

    function copy() {
        navigator.clipboard?.writeText(value).then(
            () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
            },
            () => {},
        );
    }

    return (
        <div className="flex items-start justify-between gap-4 px-5 py-3">
            <dt className="flex items-center gap-2 text-sm font-medium text-secondary min-w-[120px]">
                {icon}
                {label}
            </dt>
            <dd className="flex-1 flex items-center justify-end gap-2 text-right min-h-0 min-w-0">
                {link ? (
                    <a
                        href={value}
                        target="_blank"
                        rel="noreferrer"
                        className={`text-primary-700 hover:underline ${mono ? 'font-mono text-xs' : 'text-sm'} break-all`}
                    >
                        {value}
                    </a>
                ) : (
                    <span
                        className={`text-default ${mono ? 'font-mono text-xs' : 'text-sm'} break-all`}
                    >
                        {value}
                    </span>
                )}
                {copyable && (
                    <button
                        onClick={copy}
                        className="shrink-0 p-1 text-muted hover:text-secondary rounded"
                        title="Copy to clipboard"
                    >
                        {copied ? (
                            <Check className="w-4 h-4 text-green-600" />
                        ) : (
                            <Copy className="w-4 h-4" />
                        )}
                    </button>
                )}
            </dd>
        </div>
    );
};
