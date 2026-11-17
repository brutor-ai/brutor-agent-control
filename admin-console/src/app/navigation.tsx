/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The navigation of the Admin Console: the sidebar's sections and entries.
 *
 * Each entry's `id` is a tab id. Which screen answers a tab id is defined in
 * `screens.tsx`; an entry without children must have a screen there
 * (`screens.test.tsx` checks every one). An entry with children is not a
 * destination: the sidebar expands it, and no screen answers its id.
 *
 * It lives beside the screens, outside the sidebar component, so that
 * another distribution can add entries without editing the sidebar.
 *
 * This is the navigation of the Community Edition. The entries of the
 * Enterprise features are not listed here: the Enterprise extension places
 * each of them among these (`enterprise/src/navigation.tsx`; `NavPlacement`
 * in `extensions.ts`), by the id of its parent and of the sibling it follows.
 * An id below that an extension anchors to is part of the extension
 * interface: renaming or removing it is a change of that interface.
 */

import React from 'react';
import {
    Boxes,
    Brain,
    Shield,
    Sparkles,
    ShieldCheck,
    UserCheck,
    Braces,
    Bot,
    Package,
    Users,
    Settings,
    Network,
    Share2,
    FileText,
    Globe,
    LayoutList,
    Film,
    ScrollText,
    Download,
    DatabaseZap,
    Layers,
    Plug,
    Scale,
    CalendarX,
    Workflow,
    IdCard,
    GitBranch,
    KeyRound,
    ListChecks,
    ClipboardList,
    Files,
    ServerCog,
    LayoutDashboard,
    Gauge,
    Wrench,
    Library,
    Inbox,
} from 'lucide-react';

import MCPIcon from '../components/common/MCPIcon';
import { COMPLIANCE_SECTIONS } from '../components/compliance/complianceSections';

// ─── Types ───────────────────────────────────────────────────────────────────

export type IconProps = { className?: string; style?: React.CSSProperties };

export interface NavLeaf {
    id: string;
    label: string;
    icon: React.ComponentType<IconProps>;
    /** Optional permission gate, same semantics as NavChild's. */
    requiresPermission?: string;
    /**
     * Optional 4th-level nesting. A leaf may itself be a collapsible
     * sub-group (Section → Group → Sub-group → Leaf). Used by
     * Resources → AI Models → Cache → Response/Prompt Cache. Kept rare
     * by design — deep nav is a smell; only use when items genuinely
     * form a named cluster under an existing group.
     */
    children?: NavLeaf[];
}

export interface NavChild {
    id: string;
    label: string;
    icon: React.ComponentType<IconProps>;
    children?: NavLeaf[];
    /**
     * Optional permission-string gate (same semantics as NavItem). When
     * set, the child leaf is hidden unless `hasPermission(permission)`
     * is true. Used for Governance → AI Assets (`ai_asset:read`).
     */
    requiresPermission?: string;
    /** Deployment gate — same semantics as NavItem.requiresGate. */
    requiresGate?: string;
}

export interface NavItem {
    id: string;
    label: string;
    icon: React.ComponentType<IconProps>;
    requiresTenantAdmin?: boolean;
    /**
     * Only for a contributed principal (slot `app.principals`): the id of
     * the one that sees the entry. An entry of a principal nobody
     * contributes is never shown.
     */
    requiresPrincipal?: string;
    /**
     * Optional permission-string gate. When set, the nav item is
     * hidden unless `hasPermission(permission)` returns true. Used
     * for the System Operations Console (SYSTEM_READ)
     * — sub-section items inherit the gate so an auditor with read-
     * only access still sees the section but not destructive actions
     * (the per-action buttons inside each page check SYSTEM_OPERATE
     * separately).
     */
    requiresPermission?: string;
    /**
     * Deployment gate: only show while the named gate is open. A gate is
     * contributed to the sidebar (slot `app.nav-gates`) and reports whether
     * this deployment has the thing: `registry-instance` is open when it
     * runs the optional Brutor MCP Registry and the registry answers.
     * Unlike feature flags — which follow the show-page-as-
     * FeatureDisabled convention — a component that is not deployed at
     * all is simply absent from the nav. So is one whose gate nobody
     * contributes.
     */
    requiresGate?: string;
    children?: NavChild[];
    /** If true, children are rendered dynamically (not from static config) */
    dynamicChildren?: boolean;
}

// ─── Navigation structure ────────────────────────────────────────────────────

/**
 * Sectioned IA. Section labels are places to scan,
 * not destinations — every tab id below is unchanged from the flat nav,
 * so deep links, the persisted selection and App.tsx routing all keep
 * working. Governance is a pure rules container; the four inventory
 * concepts (AI Systems, Resource Groups, Resources, Asset Register) sit
 * adjacent under AI Estate.
 */
export interface NavSection {
    label: string | null;
    items: NavItem[];
}

export const NAV_SECTIONS: NavSection[] = [
    {
        label: 'Mission Control',
        items: [
            // The human loop first: findings awaiting a disposition, the checks
            // that raise them, the weekly summary that reports them.
            { id: 'inbox', label: 'Inbox', icon: Inbox, requiresTenantAdmin: true },
            // Agent calls parked for a person. Gated on the
            // permission, not the role: an operator who may read the queue
            // sees it; deciding needs `tool_approval:manage` (checked on the page).
            { id: 'approvals', label: 'Approvals', icon: UserCheck, requiresPermission: 'tool_approval:read' },
            // The RG-card estate dashboard — the landing page.
            { id: 'mission-control', label: 'Overview', icon: Network, requiresTenantAdmin: true },
            // The tabbed drill-down view — the card dashboard above is the
            // landing page and deep-links here per group ("Metrics" on a card).
            { id: 'mission-control-metrics', label: 'Analytics', icon: Gauge, requiresTenantAdmin: true },
        ],
    },
    {
        label: 'AI Estate',
        items: [
            // The AI System is the product-level noun,
            // so it leads the estate. It is the SUBJECT of governance, which
            // is why it is not inside the Governance section.
            { id: 'ai-systems', label: 'AI Systems', icon: Layers, requiresTenantAdmin: true, requiresPermission: 'resourcegroup:read' },
            {
                id: 'groups',
                label: 'Resource Groups',
                icon: Boxes,
                requiresTenantAdmin: true,
                // Children are rendered dynamically from the live group tree
                children: [],
                dynamicChildren: true,
            },
            {
                id: 'resources',
                label: 'Resources',
                icon: Package,
                requiresTenantAdmin: true,
                children: [
                    {
                        id: 'ai-models',
                        label: 'AI Models',
                        icon: Brain,
                        children: [
                            { id: 'llm-providers', label: 'Providers', icon: Globe },
                            { id: 'llm-models', label: 'Configuration', icon: Settings },
                            { id: 'llm-routing', label: 'Routing', icon: Share2 },
                        ],
                    },
                    { id: 'server-management', label: 'MCP Servers', icon: MCPIcon,
                        children: [
                            { id: 'mcp-config', label: 'Configuration', icon: Settings },
                        ],
                    },
                    { id: 'idp', label: 'Identity Providers', icon: Shield },
                    { id: 'agent-skills-section', label: 'Skills', icon: Sparkles,
                        children: [
                            { id: 'agent-skills', label: 'Configuration', icon: Settings },
                            { id: 'agent-skill-sources', label: 'Sources', icon: GitBranch },
                        ],
                    },
                    { id: 'a2a', label: 'Agents', icon: Workflow,
                        children: [
                            { id: 'a2a-agent-cards', label: 'Cards', icon: IdCard },
                            { id: 'a2a-identities', label: 'Identities', icon: Bot },
                            { id: 'a2a-tasks', label: 'Tasks', icon: ListChecks },
                            { id: 'a2a-delegation-graphs', label: 'Delegation Graphs', icon: GitBranch },
                            { id: 'a2a-tenant-key', label: 'Signing Key', icon: KeyRound },
                        ],
                    },
                ],
            },
            // The compliance-facing RECORD of what exists (incl. discovered /
            // shadow assets) — distinct from Resources, the operational
            // catalog of usable things. EU-AI-Act-leaning name carries that.
            { id: 'ai-assets', label: 'Asset Register', icon: ClipboardList, requiresTenantAdmin: true, requiresPermission: 'ai_asset:read' },
        ],
    },
    {
        label: 'Governance',
        items: [
            {
                id: 'governance',
                label: 'Governance',
                icon: Scale,
                requiresTenantAdmin: true,
                children: [
                    { id: 'guardrails', label: 'Guardrails', icon: ShieldCheck },
                    // UI-only grouping: the policy surfaces under one node.
                    // Child ids and routes unchanged.
                    { id: 'policy-section', label: 'Policies', icon: Files, children: [
                        { id: 'semantic-policies', label: 'Semantic Policies', icon: ScrollText, requiresPermission: 'semantic_policy:read' },
                        { id: 'argument-policies', label: 'Argument Policies', icon: Braces },
                        { id: 'agent-policies', label: 'Agent Policies', icon: Bot, requiresPermission: 'agent_identity:read' },
                    ] },
                    // Declared periods excluded from judgement
                    // (kept as evidence). A rule about what is graded, so it
                    // sits with the rules; each AI System lists its own too.
                    { id: 'incident-windows', label: 'Incident Windows', icon: CalendarX },
                    // Compliance is its own top-level section — see the Compliance section below.
                    // A2A outbound policy lives on each Resource Group's A2A
                    // governance tab — child groups inherit restrictively from
                    // the root, so the root group's settings are the tenant-wide
                    // ceiling. It has no entry here, which keeps a single source
                    // of truth with the other per-group knobs.
                ],
            },
        ],
    },
    {
        // Evidence first, frameworks second. One item whose
        // leaves are the console's pages; the shell (ComplianceConsoleTab)
        // dispatches on the leaf id. Leaf icons come from the page
        // catalogue so the sidebar and the tab strip cannot drift.
        label: 'Compliance',
        items: [
            {
                id: 'compliance',
                label: 'Compliance',
                icon: Scale,
                requiresTenantAdmin: true,
                requiresPermission: 'compliance:read',
                children: COMPLIANCE_SECTIONS.map((s) => ({ id: s.routeId, label: s.label, icon: s.icon })),
            },
        ],
    },
    {
        label: 'Operations',
        items: [
            { id: 'async-jobs', label: 'Async Jobs', icon: LayoutList, requiresTenantAdmin: true,
                children: [
                    { id: 'video-gen-jobs', label: 'Video Gen Jobs', icon: Film },
                ],
            },
            // Labelled "Logs & Audit", not "System Logs", so it cannot be
            // confused with System; the children say what they are.
            { id: 'monitoring', label: 'Logs & Audit', icon: ScrollText, requiresTenantAdmin: true,
                children: [
                    { id: 'monitoring-audit-logs', label: 'Admin Audit Logs', icon: ScrollText },
                    { id: 'monitoring-proxy-logs', label: 'Proxy Logs', icon: ScrollText },
                    { id: 'monitoring-audit-export', label: 'Audit Export', icon: Download },
                ],
            },
        ],
    },
    {
        label: 'Administration',
        items: [
            { id: 'users', label: 'Users', icon: Users, requiresTenantAdmin: true },
            // Tenant gateway settings. Labelled "Settings" so that, next to the
            // System runtime console, the two cannot be confused.
            { id: 'config', label: 'Settings', icon: Settings, requiresTenantAdmin: true },
            // System Operations Console. Gated on `system:read`.
            // The sub-section ids match the routes SystemConsoleTab
            // dispatches on. Nav gates visibility; pages gate destructive
            // actions.
            {
                id: 'system',
                label: 'System',
                icon: ServerCog,
                requiresPermission: 'system:read',
                children: [
                    { id: 'system-overview',    label: 'Overview',         icon: LayoutDashboard },
                    { id: 'system-counters',    label: 'Counters',         icon: Gauge },
                    { id: 'system-caches',      label: 'Caches',           icon: DatabaseZap },
                    { id: 'system-chains',      label: 'Audit Chains',     icon: ShieldCheck },
                    { id: 'system-audit',       label: 'Audit',            icon: ScrollText },
                    { id: 'system-connections', label: 'Connections',      icon: Plug },
                    { id: 'system-tasks',       label: 'Background Tasks', icon: ListChecks },
                    { id: 'system-sessions',    label: 'Sessions',         icon: Users },
                    { id: 'system-logs',        label: 'Logs',             icon: FileText },
                    { id: 'system-maintenance', label: 'Maintenance',      icon: Wrench },
                ],
            },
        ],
    },
    {
        label: 'Platform',
        items: [
            // The catalogs the installation shares: for whoever holds
            // `platform:catalog:manage`. Where there is one tenant that is
            // its administrator; where there is a system administrator the
            // tenant administrators do not hold it, and the system
            // administrator holds every permission.
            { id: 'llm-catalog-admin', label: 'LLM Catalog', icon: Library, requiresPermission: 'platform:catalog:manage' },
            { id: 'mcp-catalog-admin', label: 'MCP Catalog', icon: Package, requiresPermission: 'platform:catalog:manage' },
        ],
    },
];

/** Flat view — the ancestor map and any id-based lookups build on this. */
export const NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap((s) => s.items);
