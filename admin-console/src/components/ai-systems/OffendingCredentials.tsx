/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The credentials that keep the lifecycle gate's workload identity
 * requirement unmet: machine calls below the level the system needs after the
 * transition, in the last 7 days, made with a credential that can still
 * authenticate. Retiring the credential (revoking the key) clears it.
 */

import React from 'react';

import { ago } from '../../lib/relativeTime';
import { KIND_LABELS, LEVEL_LABELS, credentialKey, offendingCredentialsOf } from '../../lib/identityPolicy';
import type { GateRequirement } from '../../lib/aiSystemTypes';

const kindLabel = (kind: string): string => (KIND_LABELS as Record<string, string>)[kind] ?? kind;

/** Renders nothing unless the requirement is unmet and carries offending credentials. */
export const OffendingCredentials: React.FC<{ requirement: GateRequirement; testId: string; className?: string }> = ({ requirement, testId, className = '' }) => {
    if (requirement.satisfied) return null;
    const offending = offendingCredentialsOf(requirement);
    if (!offending || offending.items.length === 0) return null;
    const unlisted = offending.total - offending.items.length;
    return (
        <div className={`mt-1 ml-5 text-xs text-secondary space-y-1 ${className}`} data-testid={testId}>
            <p className="text-muted">
                Below the level production needs, in the last 7 days, with credentials that are still usable. Revoking a key clears it.
            </p>
            <ul className="space-y-0.5">
                {offending.items.map((c) => (
                    <li key={credentialKey(c)} className="flex flex-wrap gap-x-2">
                        <code className="font-mono break-all text-default">{c.credential_ref ?? 'no reference'}</code>
                        <span>{kindLabel(c.credential_kind)}</span>
                        <span>{c.credential_assurance ? LEVEL_LABELS[c.credential_assurance] : 'no level'}</span>
                        <span>{c.calls} calls</span>
                        {c.last_seen && (
                            <span>
                                last <time dateTime={c.last_seen}>{ago(c.last_seen)}</time>
                            </span>
                        )}
                    </li>
                ))}
            </ul>
            {unlisted > 0 && <p className="text-muted">{unlisted} more not listed.</p>}
        </div>
    );
};

export default OffendingCredentials;
