/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control → Inbox: the human loop, on its own page.
 *
 * Three things an operator works rather than watches — the assurance
 * inbox (findings awaiting a disposition), the continuous checks that
 * raise them, and the weekly summary that projects them — live here, not on
 * top of the estate dashboard, where they would make the landing page a queue
 * and the queue a preamble. The dashboard is the *overview*; this is where
 * the work is.
 */

import { Inbox } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import AssuranceInbox from './AssuranceInbox';
import AssuranceChecks from './AssuranceChecks';
import AssuranceDigestPanel from './AssuranceDigestPanel';

interface AssuranceInboxPageProps {
    setActiveTab?: (tab: string) => void;
    /** `inbox:reviews:<id>` — open that judged check's blind review queue. */
    reviewCheckId?: string;
}

export default function AssuranceInboxPage({ setActiveTab, reviewCheckId }: AssuranceInboxPageProps) {
    return (
        <div className="p-6 space-y-4" data-testid="assurance-inbox-page">
            <div className="flex items-start gap-2">
                <Inbox className="w-5 h-5 text-primary-600 mt-0.5" />
                <div>
                    <div className="flex items-center gap-2">
                        <h1 className="text-xl font-semibold text-default">Inbox</h1>
                        <HelpTip text="Findings that need a person: guardrail blocks, drift with its cause, liveness misses, budget stops and check breaches converge here, deduplicated on a fingerprint. Every item is closed with a disposition that becomes evidence in the Assurance Report; unreviewed critical items count as review debt against system health." />
                    </div>
                    <p className="text-sm text-secondary">
                        Findings awaiting a decision, the continuous checks that raise them, and the
                        weekly summary that reports them.
                    </p>
                </div>
            </div>

            {/* The sink, a source, and the mirror. */}
            <AssuranceInbox setActiveTab={setActiveTab} />
            <AssuranceChecks focusReviewCheckId={reviewCheckId} />
            <AssuranceDigestPanel />
        </div>
    );
}
