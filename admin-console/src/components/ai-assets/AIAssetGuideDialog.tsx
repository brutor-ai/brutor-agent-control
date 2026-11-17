/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI Register — "Guide for operators & auditors" dialog.
 *
 * A read-only, tabbed help modal launched from the registry header. It
 * expands on the short "What the AI Register tracks" info banner
 * with three audiences:
 *   - "What is it"     — the mental model (inventory + governance overlay
 *                        + evidence), the six asset kinds, and the three
 *                        coverage planes.
 *   - "For operators"  — the day-to-day workflow: sync, curate governance,
 *                        handle observed/flagged/ungoverned assets.
 *   - "For auditors"   — the factsheet dossier, immutable snapshots,
 *                        compliance framework mapping, and export/audit.
 *
 * Pure presentational: no data fetching, no gateway calls. Matches the
 * modal chrome of AIAssetEditDialog (overlay + centered panel + header
 * with an X). Closes on the X, a backdrop click, or Escape.
 */

import React, { useEffect, useState } from 'react';
import {
    BookOpen,
    ClipboardCheck,
    Info,
    Wrench,
    X,
} from 'lucide-react';

interface Props {
    onClose: () => void;
}

type TabId = 'what' | 'operators' | 'auditors';

const TABS: { id: TabId; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { id: 'what', label: 'What is it', icon: Info },
    { id: 'operators', label: 'For operators', icon: Wrench },
    { id: 'auditors', label: 'For auditors', icon: ClipboardCheck },
];

/** Section sub-heading inside a tab panel. */
const H: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <h3 className="text-sm font-semibold text-default mt-5 first:mt-0">{children}</h3>
);

/** Lead paragraph. */
const P: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <p className="text-sm text-secondary leading-relaxed">{children}</p>
);

/** Bulleted list wrapper. */
const UL: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <ul className="mt-2 space-y-1.5 text-sm text-secondary">{children}</ul>
);

/** Bullet with an emphasised lead-in term. */
const LI: React.FC<{ term?: string; children: React.ReactNode }> = ({ term, children }) => (
    <li className="flex gap-2">
        <span className="mt-1.5 h-1.5 w-1.5 flex-shrink-0 rounded-sm bg-primary-500" />
        <span>
            {term && <span className="font-medium text-default">{term} — </span>}
            {children}
        </span>
    </li>
);

/** Callout box for caveats / honesty notes. */
const Note: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        {children}
    </div>
);

const WhatIsIt: React.FC = () => (
    <div>
        <P>
            The AI Register is this tenant&apos;s <strong>system of record for every
            AI asset</strong> — an inventory, a governance overlay, and an evidence
            generator in one. The coverage scorecard is just the landing view; underneath
            it is a per-asset record you can classify, annotate, and produce audit
            evidence from.
        </P>

        <H>The three layers</H>
        <UL>
            <LI term="Inventory">
                every AI asset auto-discovered from the platform configuration, plus any
                externally-hosted ones you add manually. Deduplicated and durable — not a
                rollup of counts.
            </LI>
            <LI term="Governance overlay">
                metadata you layer on each asset that the raw config doesn&apos;t hold:
                owner, lifecycle stage, risk tier, intended use, enforcement mode, tags,
                and review status.
            </LI>
            <LI term="Coverage scorecard">
                a derived split of the same assets across three planes (below), answering
                &ldquo;how much of our AI estate do we actually govern?&rdquo;
            </LI>
        </UL>

        <H>What it tracks (asset kinds)</H>
        <UL>
            <LI>LLM models and LLM providers</LI>
            <LI>Routing groups</LI>
            <LI>MCP servers</LI>
            <LI>Skills</LI>
            <LI>Agent cards</LI>
        </UL>

        <H>The three planes</H>
        <UL>
            <LI term="Governed">
                configured in the gateway and enforced inline — requests are proxied
                through Brutor.
            </LI>
            <LI term="Observed">
                imported from vendor traffic data for cost/usage visibility but{' '}
                <em>not</em> enforced. Enable the asset to move it into Governed.
            </LI>
            <LI term="Ungoverned">
                &ldquo;shadow AI&rdquo; the out-of-band discovery plane has seen in your
                environment but that isn&apos;t configured in — or flowing through — the
                gateway. These are your coverage gaps.
            </LI>
        </UL>

        <H>How assets get in</H>
        <UL>
            <LI term="Sync">
                rescans the platform configuration and reconciles the registry.
            </LI>
            <LI term="Manual add">
                register an externally-hosted asset the discovery scan can&apos;t see.
            </LI>
            <LI term="Discovery plane">
                Ed25519-signed collector events report shadow AI observed out-of-band
                (governance status is always computed against real config — discovery can
                never fake &ldquo;governed&rdquo;).
            </LI>
        </UL>
    </div>
);

const ForOperators: React.FC = () => (
    <div>
        <P>
            Operators keep the inventory current and curate the governance overlay on each
            asset. Your annotations are safe: <strong>Sync never overwrites operator
            fields</strong> — it only refreshes technical facts (name, icon, enabled
            state, observed flag).
        </P>

        <H>Keep the inventory current</H>
        <UL>
            <LI term="Sync now">
                run after adding or changing models, providers, MCP servers, skills, or
                agent cards. Reports created / updated / flagged counts.
            </LI>
        </UL>

        <H>Curate governance per asset (Edit)</H>
        <UL>
            <LI term="Owner">who is accountable for the asset.</LI>
            <LI term="Lifecycle stage">
                proposed → approved → active → deprecated → retired.
            </LI>
            <LI term="Risk tier">
                EU AI Act-aligned: minimal, limited, high, or unacceptable.
            </LI>
            <LI term="Intended use">a short statement of what the asset is for.</LI>
            <LI term="Tags & status">
                free-form tags; status ok / review due / flagged.
            </LI>
        </UL>

        <H>Handle the special cases</H>
        <UL>
            <LI term="Flagged (orphan)">
                the backing configuration disappeared. The registry keeps the row for
                review rather than silently dropping it — clean up or investigate.
            </LI>
            <LI term="Observed">
                imported for visibility only. Enable the underlying model to govern it
                inline; it then flips from Observed to Governed.
            </LI>
            <LI term="Ungoverned / shadow AI">
                review the Discovered — Ungoverned table, then onboard the asset from the
                Discovery plane to bring it under governance.
            </LI>
            <LI term="Manual asset">
                use for externally-hosted resources you govern by policy but don&apos;t
                proxy through the gateway.
            </LI>
        </UL>

        <Note>
            <strong>Enforcement mode is stored intent</strong> (off / warn / enforce) that
            records your governance decision on the asset. Live request enforcement today
            happens through resource-group policies and guardrails — set those to actually
            block or redact traffic.
        </Note>
    </div>
);

const ForAuditors: React.FC = () => (
    <div>
        <P>
            For audit, the registry is your <strong>AI system-of-record / AI risk
            register</strong>. The evidence artifact isn&apos;t the list — it&apos;s the
            per-asset <strong>Factsheet</strong> and its immutable <strong>snapshots</strong>.
        </P>

        <H>The Factsheet (per-asset dossier)</H>
        <P>Open the chart icon on any asset. Over a chosen window (e.g. 30 / 90 days / all-time) it assembles:</P>
        <UL>
            <LI term="Asset & config">
                what the system is, its owner, risk tier, intended use, lifecycle state.
            </LI>
            <LI term="Usage">
                requests, tokens, cost, latency, and which models actually ran — evidence
                of activity.
            </LI>
            <LI term="Guardrails">
                counts of guardrail blocks, PII detections, and argument-policy blocks —
                evidence controls fired.
            </LI>
            <LI term="Compliance">
                per-framework implication counts: SOC 2, GDPR Article 30, HIPAA, EU AI
                Act (by risk tier), and ISO 42001.
            </LI>
            <LI term="Residency">
                which regions / residency classes the asset routed to.
            </LI>
            <LI term="Access">
                which resource groups (RBAC) can reach the asset — least-privilege
                evidence.
            </LI>
        </UL>

        <H>Immutable evidence</H>
        <UL>
            <LI term="Snapshots">
                freeze the dossier at a point in time with its window and trigger
                (manual / scheduled / export) — self-describing evidence that won&apos;t
                shift under review.
            </LI>
            <LI term="Export">
                download any factsheet as CSV or JSON.
            </LI>
            <LI term="Audit trail">
                every create / update / delete / sync / snapshot / export is written to
                the audit log with the acting admin.
            </LI>
        </UL>

        <H>Questions it answers</H>
        <UL>
            <LI>
                &ldquo;Show every <strong>high-risk</strong> AI system, its owner, and who
                can access it.&rdquo; → filter by risk tier, open the factsheet.
            </LI>
            <LI>
                &ldquo;Prove PII / injection controls were active on this model last
                quarter.&rdquo; → factsheet Guardrails + a quarter-end snapshot.
            </LI>
            <LI>
                &ldquo;Show your <strong>EU AI Act</strong> risk register / <strong>GDPR
                Article 30</strong> record for AI.&rdquo; → the registry is the register;
                risk tier + compliance section per asset.
            </LI>
            <LI>
                &ldquo;What AI is running that you don&apos;t govern?&rdquo; → the
                Ungoverned plane, with the guarantee that discovery can never fake
                &ldquo;governed.&rdquo;
            </LI>
        </UL>
    </div>
);

const AIAssetGuideDialog: React.FC<Props> = ({ onClose }) => {
    const [tab, setTab] = useState<TabId>('what');

    // Escape closes the dialog (parity with a light-dismiss modal).
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    return (
        <div
            className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
            onClick={onClose}
        >
            <div
                className="bg-surface rounded-xl shadow-2xl w-full max-w-3xl max-h-[90vh] flex flex-col"
                onClick={(e) => e.stopPropagation()}
                role="dialog"
                aria-modal="true"
                aria-label="AI Register guide"
            >
                {/* Header */}
                <div className="flex items-center justify-between border-b border-default px-5 py-4">
                    <div className="flex items-center gap-2.5">
                        <div className="p-1.5 rounded-lg bg-primary-100">
                            <BookOpen className="w-5 h-5 text-primary-600" />
                        </div>
                        <h2 className="text-lg font-semibold text-default">
                            AI Register — guide for operators &amp; auditors
                        </h2>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1.5 hover:bg-surface-alt rounded-md transition-colors"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5 text-muted" />
                    </button>
                </div>

                {/* Tabs */}
                <div className="flex gap-1 border-b border-default px-5 pt-3">
                    {TABS.map(({ id, label, icon: Icon }) => {
                        const active = tab === id;
                        return (
                            <button
                                key={id}
                                onClick={() => setTab(id)}
                                className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium rounded-t-lg border-b-2 -mb-px transition-colors ${
                                    active
                                        ? 'border-primary-500 text-primary-600'
                                        : 'border-transparent text-muted hover:text-secondary hover:bg-surface-alt'
                                }`}
                                aria-current={active ? 'page' : undefined}
                            >
                                <Icon className="w-4 h-4" />
                                {label}
                            </button>
                        );
                    })}
                </div>

                {/* Body */}
                <div className="scroll-region p-5">
                    {tab === 'what' && <WhatIsIt />}
                    {tab === 'operators' && <ForOperators />}
                    {tab === 'auditors' && <ForAuditors />}
                </div>

                {/* Footer */}
                <div className="flex justify-end border-t border-default px-5 py-3">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AIAssetGuideDialog;
