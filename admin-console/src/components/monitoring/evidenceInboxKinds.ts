/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance Inbox — the evidence programme's own finding kinds: an icon, a one-line meaning, and where each one is worked.
 *
 *   capture_gap          → Compliance → Evidence Ledger
 *   incident_deadline    → Compliance → Incidents
 *   judge_uncalibrated   → the check's blind review queue (Inbox → checks)
 *   calibration_stalled  → the check's blind review queue
 *   evidence_unwitnessed → Settings → Tenant → Evidence (witnesses)
 *
 * Every target is a tab id App.tsx routes; an unrouted id is a silent
 * no-op, which is why the test walks each one.
 */

import type { LucideIcon } from 'lucide-react';
import { AlarmClock, EyeOff, FileWarning, Scale, Timer } from 'lucide-react';

import type { InboxCategory } from '../../lib/assuranceInboxTypes';
import { getString } from '../../lib/narrow';

export type EvidenceInboxKind = Extract<
    InboxCategory,
    'evidence_unwitnessed' | 'capture_gap' | 'judge_uncalibrated' | 'calibration_stalled' | 'incident_deadline'
>;

export const EVIDENCE_INBOX_KINDS: Record<EvidenceInboxKind, { icon: LucideIcon; meaning: string }> = {
    evidence_unwitnessed: {
        icon: EyeOff,
        meaning: 'Tree heads have stayed standalone or self-witnessed for over an hour — no independent witness receipt yet.',
    },
    capture_gap: {
        icon: FileWarning,
        meaning: 'Something that should have been sealed was not: the change is saved, but no record exists for it.',
    },
    judge_uncalibrated: {
        icon: Scale,
        meaning: 'Blind human grades agree with this judge less often than the threshold — its verdicts need review.',
    },
    calibration_stalled: {
        icon: Timer,
        meaning: 'Sampled verdicts have waited 14 days without a blind human grade.',
    },
    incident_deadline: {
        icon: AlarmClock,
        meaning: 'A reporting deadline for a compliance incident is due within 24 hours or has passed.',
    },
};

export function isEvidenceInboxKind(category: string): category is EvidenceInboxKind {
    return category in EVIDENCE_INBOX_KINDS;
}

export interface InboxLink {
    label: string;
    tab: string;
}

/** Where an evidence finding is worked. `[]` for any other category. */
export function evidenceInboxLinks(category: string, evidence: unknown): InboxLink[] {
    switch (category) {
        case 'capture_gap':
            return [{ label: 'Open the Evidence Ledger', tab: 'compliance-evidence' }];
        case 'incident_deadline':
            return [{ label: 'Open Compliance Incidents', tab: 'compliance-incidents' }];
        case 'judge_uncalibrated':
        case 'calibration_stalled': {
            const checkId = getString(evidence, 'check_id');
            return [{ label: 'Open the blind review queue', tab: checkId ? `inbox:reviews:${checkId}` : 'inbox' }];
        }
        case 'evidence_unwitnessed':
            return [{ label: 'Configure witnesses', tab: 'config:tenant:evidence' }];
        default:
            return [];
    }
}
