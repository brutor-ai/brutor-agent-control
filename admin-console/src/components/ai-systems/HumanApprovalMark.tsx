/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { HUMAN_APPROVAL_GLYPH, HUMAN_APPROVAL_META, approvalTitle, type NodeApproval } from '../../lib/humanApproval';
import type { HumanApprovalScope } from '../../lib/aiSystemTypes';

const SCOPE_BORDER: Record<HumanApprovalScope, string> = {
    always: 'border-solid',
    conditional: 'border-dashed',
};

/** Declared human approval on a resource. The text and the border both say
 *  which kind it is, so colour is never the only signal. Neutral on purpose: a
 *  gate is a control, not a fault. */
export function HumanApprovalMark({
    approval,
    compact = false,
    testId,
}: {
    approval: NodeApproval;
    /** The glyph alone, for the tight lane box; the tooltip carries the rest. */
    compact?: boolean;
    testId: string;
}) {
    const { label } = HUMAN_APPROVAL_META[approval.scope];
    return (
        <span
            className={`mt-0.5 inline-flex items-center gap-1 whitespace-nowrap rounded-full border ${SCOPE_BORDER[approval.scope]} border-strong px-1.5 text-[10px] font-medium text-secondary`}
            title={approvalTitle(approval)}
            aria-label={compact ? label : undefined}
            data-testid={testId}
            data-scope={approval.scope}
        >
            <span aria-hidden="true">{HUMAN_APPROVAL_GLYPH}</span>
            {!compact && label}
        </span>
    );
}

/** The key for the marks; drawn only when the system has a gate. */
export function HumanApprovalLegend() {
    return (
        <div className="rounded-lg border border-default bg-surface px-3 py-2" data-testid="human-approval-legend">
            <p className="mb-1 text-xs font-medium text-secondary">Human approval (declared in the contract)</p>
            <ul className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                {(['always', 'conditional'] as const).map((scope) => (
                    <li key={scope} className="flex items-start gap-2 text-xs text-secondary">
                        <HumanApprovalMark
                            approval={{ scope, details: [], windowSeconds: null }}
                            testId={`human-approval-legend-${scope}`}
                        />
                        <span>{HUMAN_APPROVAL_META[scope].meaning}</span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
