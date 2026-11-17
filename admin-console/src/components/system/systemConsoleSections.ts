/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// The System console's sub-section catalogue.
//
// Lives beside SystemConsoleTab rather than inside it so that component file
// exports only its component: a module exporting both a component and data
// opts out of Fast Refresh (react-refresh/only-export-components). Tests also
// import SUB_SECTIONS directly to pin the canonical tab order.
import { DatabaseZap, FileText, Gauge, LayoutDashboard, ListChecks, Plug, ScrollText, Users, Wrench, ShieldCheck } from 'lucide-react';

// The catalogue is the single source of truth for:
//   * the route id the sidebar dispatches on (e.g. 'system-overview')
//   * the page title and description shown in the header
//   * the icon used by both the header and the sub-tab nav (kept in
//     lockstep with AppSidebar.NAV_ITEMS to avoid drift)
//
// A sub-section without a page of its own renders a placeholder: "Not
// available yet", the one-line description, and its sample action,
// disabled (so the role gating can be seen without the action).

export type SystemSubSection =
    | 'overview'
    | 'counters'
    | 'caches'
    | 'audit'
    | 'chains'
    | 'connections'
    | 'tasks'
    | 'sessions'
    | 'logs'
    | 'maintenance';

export interface SubSectionMeta {
    id: SystemSubSection;
    routeId: string;
    label: string;
    icon: React.FC<{ className?: string }>;
    description: string;
}

export const SUB_SECTIONS: SubSectionMeta[] = [
    {
        id: 'overview',
        routeId: 'system-overview',
        label: 'Overview',
        icon: LayoutDashboard,
        description:
            'At-a-glance health for every dependency Brutor relies on — database, Redis, Qdrant, every configured LLM upstream, every MCP server. Each card links to the right deep-dive sub-section.',
    },
    {
        id: 'counters',
        routeId: 'system-counters',
        label: 'Counters',
        icon: Gauge,
        description:
            'Live per-tenant token-bucket / quota / cooldown state for every LLM and MCP rate limit. Reset a counter or release a cooldown when an operator decision warrants it (e.g. promo, false positive). Every action is audited.',
    },
    {
        id: 'caches',
        routeId: 'system-caches',
        label: 'Caches',
        icon: DatabaseZap,
        description:
            'Stats and flush controls for the 5 cache layers — group cache, semantic LLM cache, MCP tool cache, OAuth-token cache, and identity-mapping cache. Each flush is scope-restricted (tenant / group / all) and applied on every replica.',
    },
    {
        id: 'chains',
        routeId: 'system-chains',
        label: 'Audit Chains',
        icon: ShieldCheck,
        description:
            'Audit records are hash-chained per proxy process and signed in batches. Verifying recomputes every row hash, reports deleted rows as gaps, and checks signatures against the keys this deployment trusts — so a change made after the fact is detectable rather than invisible.',
    },
    {
        id: 'audit',
        routeId: 'system-audit',
        label: 'Audit',
        icon: ScrollText,
        description:
            'Every action taken from this console is recorded. Auditors get a filterable list, a before/after diff drawer, and CSV export. This is the compliance trail that backs every other tab.',
    },
    {
        id: 'connections',
        routeId: 'system-connections',
        label: 'Connections',
        icon: Plug,
        description:
            'Pool sizing + saturation gauges for every outbound dependency — Postgres, Redis, Qdrant, OpenAI/Anthropic/Bedrock/Vertex HTTP pools, plus per-MCP pool status. Useful to spot a noisy upstream before users page on-call.',
    },
    {
        id: 'tasks',
        routeId: 'system-tasks',
        label: 'Background Tasks',
        icon: ListChecks,
        description:
            'Status and manual controls for every long-running job — KB connector sync, KB ingest worker, A2A delegation sweeper, log retention vacuum, semantic-cache GC. Pause / resume / trigger-now; the state persists across replicas and restarts.',
    },
    {
        id: 'sessions',
        routeId: 'system-sessions',
        label: 'Sessions',
        icon: Users,
        description:
            'Browse and revoke live admin sessions, portal user sessions, and API keys in use right now. Three tabs match the three credential types; revoke reaches every replica in milliseconds.',
    },
    {
        id: 'logs',
        routeId: 'system-logs',
        label: 'Logs',
        icon: FileText,
        description:
            'Multi-replica live tail with per-module level sliders. Dial up debug on `mcp::vmcp` for 5 minutes without restarting the proxy; ship the tail to download for incident response. Replaces SSH-to-the-box for routine triage.',
    },
    {
        id: 'maintenance',
        routeId: 'system-maintenance',
        label: 'Maintenance',
        icon: Wrench,
        description:
            'Tenant- or platform-wide maintenance mode with drain grace period, rolling-restart trigger, and database hygiene tasks (VACUUM, REINDEX, retention sweep). The most dangerous tab — it requires the maintenance permission and a confirmation.',
    },
];

export const SUB_SECTION_BY_ROUTE: Record<string, SubSectionMeta> = Object.fromEntries(
    SUB_SECTIONS.map(s => [s.routeId, s]),
);
