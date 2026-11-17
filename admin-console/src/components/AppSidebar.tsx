/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
    ChevronDown,
    ChevronLeft,
    ChevronRight,
    LogOut,
    Moon,
    Sun,
    AlertCircle,
    Plus,
    RefreshCw,
    Loader,
    Unlink,
} from 'lucide-react';
import { BrutorLogo } from './BrutorLogo';
import { useTenantTheme } from '../context/TenantThemeContext';
import { useTheme } from '../hooks/useTheme';
import { GroupHierarchyTree as GroupTree, formatGroupLabel } from '../lib/groupTypes';
import GovernanceTreeBadges from './groups/GovernanceTreeBadges';
import { resolveGroupIcon, resolveGroupColor } from '../lib/lucideIconMap';
import { useSlot } from '../app/slots';
import ReadFailureNotice from './common/ReadFailureNotice';
import type { ReadFailure } from '../hooks/useRead';

import { UserAvatar } from './ui/UserAvatar';
import { NAV_ITEMS, NAV_SECTIONS, type NavChild, type NavItem, type NavLeaf, type NavSection } from '../app/navigation';

// The navigation is defined in `app/navigation.tsx`. Re-exported here for the
// modules that have always read it from the sidebar.
export { NAV_ITEMS, NAV_SECTIONS };

// ─── Types ───────────────────────────────────────────────────────────────────

interface AppSidebarProps {
    /**
     * The navigation to show: the built-in sections and an extension's. Absent, the built-in sections.
     */
    sections?: readonly NavSection[];
    activeTab: string;
    onTabChange: (tab: string) => void;
    username: string;
    tenantId: string;
    tenantDisplayName?: string;
    isTenantAdmin: boolean;
    /**
     * The contributed principal the administrator is signed in as (slot
     * `app.principals`), if any: its id opens the entries that require it,
     * its label is shown under the name.
     */
    principal?: { id: string; label: string } | null;
    avatarUrl?: string;
    onLogout: () => void;
    hasErrors: boolean;
    errorCount: number;
    hasCriticalErrors: boolean;
    /** Unresolved assurance findings by severity, shown on the Inbox item. */
    inboxBadges?: { critical: number; warning: number } | null;
    /** Live group hierarchy tree for the Resource Groups section */
    groupTree: GroupTree[];
    groupTreeLoading: boolean;
    /** The read of the tree failed: the section says so where it would say there are no groups. */
    groupTreeFailure?: ReadFailure | null;
    onRefreshGroupTree: () => void;
    /**
     * Permission check from the auth hook. Used by the visibility
     * filter when a NavItem declares `requiresPermission`: the
     * System nav item is hidden from anyone without `system:read`.
     */
    hasPermission: (permission: string) => boolean;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function buildAncestorMap(items: NavItem[]): Map<string, string[]> {
    const map = new Map<string, string[]>();
    for (const item of items) {
        if (!item.children || item.dynamicChildren) continue;
        for (const child of item.children) {
            if (child.children) {
                for (const leaf of child.children) {
                    map.set(leaf.id, [item.id, child.id]);
                    // 4th level: a leaf that is itself a sub-group (e.g.
                    // AI Models → Cache → Response/Prompt) — its own leaves
                    // carry all three ancestors so they auto-expand fully.
                    if (leaf.children) {
                        for (const sub of leaf.children) {
                            map.set(sub.id, [item.id, child.id, leaf.id]);
                        }
                    }
                }
            } else {
                map.set(child.id, [item.id]);
            }
        }
    }
    return map;
}

const BUILTIN_ANCESTOR_MAP = buildAncestorMap(NAV_ITEMS);

function countChildLeaves(child: NavChild, expanded: Set<string>): number {
    if (!child.children) return 1;
    if (!expanded.has(child.id)) return 1; // collapsed group → just its header row
    // header row + each leaf; a leaf that is itself an expanded sub-group
    // (4th level, e.g. Cache) contributes its own leaves too.
    let rows = 1;
    for (const leaf of child.children) {
        rows += 1;
        if (leaf.children && expanded.has(leaf.id)) rows += leaf.children.length;
    }
    return rows;
}

const SIDEBAR_COLLAPSED_KEY = 'sidebar-collapsed';
const SIDEBAR_EXPANDED_SECTIONS_KEY = 'sidebar-expanded-sections';
const GROUP_TREE_EXPANDED_KEY = 'sidebar-group-tree-expanded';
const SIDEBAR_WIDTH_KEY = 'sidebar-width';

const SIDEBAR_MIN_WIDTH = 200;
const SIDEBAR_MAX_WIDTH = 420;
const SIDEBAR_DEFAULT_WIDTH = 256;
const SIDEBAR_COLLAPSED_WIDTH = 64;

// ─── Component ───────────────────────────────────────────────────────────────

// Sky-blue defaults from TenantThemeContext's DEFAULT_THEME — the values a
// tenant that has never branded the console still resolves to. Used only to
// detect "unbranded", never to paint.
const DEFAULT_HEADER_PRIMARY = '#1BB5E8';
const DEFAULT_HEADER_SECONDARY = '#87ceeb';
// Dark-mode header ramp: slate-900 -> slate-800, matching the console's own
// dark chrome so the logo sits on the sidebar rather than on a banner.
const DARK_HEADER_FROM = '#0f172a';
const DARK_HEADER_TO = '#1e293b';

const AppSidebar: React.FC<AppSidebarProps> = ({
    sections = NAV_SECTIONS,
    activeTab,
    onTabChange,
    username,
    tenantId,
    tenantDisplayName,
    isTenantAdmin,
    principal,
    avatarUrl,
    onLogout,
    hasErrors,
    errorCount,
    hasCriticalErrors,
    inboxBadges,
    groupTree,
    groupTreeLoading,
    groupTreeFailure,
    onRefreshGroupTree,
    hasPermission,
}) => {
    const { theme } = useTenantTheme();
    const primaryColor = theme.primary_color || '#1BB5E8';
    const ancestorMap = React.useMemo(
        () => (sections === NAV_SECTIONS ? BUILTIN_ANCESTOR_MAP : buildAncestorMap(sections.flatMap((s) => s.items))),
        [sections]
    );
    const secondaryColor = theme.secondary_color || '#87ceeb';

    // Deployment gates (slot `app.nav-gates`): an entry that requires a gate
    // is shown only while the gate's probe reports it open. Nothing reported,
    // or a gate nobody contributes, is closed.
    const navGates = useSlot('app.nav-gates');
    const [openGates, setOpenGates] = useState<Record<string, boolean>>({});
    const reportGate = useCallback(
        (gate: string, open: boolean) => setOpenGates((gates) => (gates[gate] === open ? gates : { ...gates, [gate]: open })),
        [],
    );
    const gateOpen = (gate?: string) => !gate || openGates[gate] === true;

    // ── Dark mode toggle ───────────────────────────────────────────────────
    const { theme: currentTheme, setTheme: setCurrentTheme } = useTheme();

    // ── Logo header background ─────────────────────────────────────────────
    // The tenant theme resolves `admin_dark` per field and falls back to the
    // same sky-blue defaults as `admin_light`, so an unbranded console put a
    // bright banner behind the logo while every other surface went dark. In
    // dark mode an unbranded header uses the dark surface ramp instead. A
    // tenant that HAS set admin_dark colours still gets its own gradient —
    // this only replaces the default nobody chose.
    const headerIsUnbranded =
        primaryColor === DEFAULT_HEADER_PRIMARY && secondaryColor === DEFAULT_HEADER_SECONDARY;
    const headerBackground =
        currentTheme === 'dark' && headerIsUnbranded
            ? `linear-gradient(135deg, ${DARK_HEADER_FROM}, ${DARK_HEADER_TO})`
            : `linear-gradient(135deg, ${primaryColor}, ${secondaryColor})`;

    // ── Persisted collapsed state ────────────────────────────────────────────
    const [collapsed, setCollapsed] = useState(() => {
        try { return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true'; }
        catch { return false; }
    });

    useEffect(() => {
        try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed)); }
        catch { /* ignore */ }
    }, [collapsed]);

    // ── Persisted sidebar width (resizable) ───────────────────────────────────
    const [sidebarWidth, setSidebarWidth] = useState(() => {
        try {
            const stored = localStorage.getItem(SIDEBAR_WIDTH_KEY);
            if (stored) {
                const w = parseInt(stored, 10);
                if (w >= SIDEBAR_MIN_WIDTH && w <= SIDEBAR_MAX_WIDTH) return w;
            }
        } catch { /* ignore */ }
        return SIDEBAR_DEFAULT_WIDTH;
    });

    useEffect(() => {
        try { localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth)); }
        catch { /* ignore */ }
    }, [sidebarWidth]);

    const isResizing = useRef(false);

    const handleResizeStart = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        isResizing.current = true;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        const onMouseMove = (ev: MouseEvent) => {
            if (!isResizing.current) return;
            const newWidth = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, ev.clientX));
            setSidebarWidth(newWidth);
        };

        const onMouseUp = () => {
            isResizing.current = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };

        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
    }, []);

    const handleResizeDoubleClick = useCallback(() => {
        setSidebarWidth(SIDEBAR_DEFAULT_WIDTH);
    }, []);

    // ── Persisted expanded sections ──────────────────────────────────────────
    const [expandedSections, setExpandedSections] = useState<Set<string>>(() => {
        try {
            const raw = localStorage.getItem(SIDEBAR_EXPANDED_SECTIONS_KEY);
            return raw ? new Set(JSON.parse(raw)) : new Set(['resources']);
        }
        catch { return new Set(['resources']); }
    });

    useEffect(() => {
        try { localStorage.setItem(SIDEBAR_EXPANDED_SECTIONS_KEY, JSON.stringify([...expandedSections])); }
        catch { /* ignore */ }
    }, [expandedSections]);

    // ── Persisted expanded group tree nodes ──────────────────────────────────
    const [expandedGroupNodes, setExpandedGroupNodes] = useState<Set<string>>(() => {
        try {
            const raw = localStorage.getItem(GROUP_TREE_EXPANDED_KEY);
            return raw ? new Set(JSON.parse(raw)) : new Set<string>();
        }
        catch { return new Set<string>(); }
    });

    useEffect(() => {
        try { localStorage.setItem(GROUP_TREE_EXPANDED_KEY, JSON.stringify([...expandedGroupNodes])); }
        catch { /* ignore */ }
    }, [expandedGroupNodes]);

    const toggleGroupNode = (nodeId: string) => {
        setExpandedGroupNodes(prev => {
            const next = new Set(prev);
            if (next.has(nodeId)) next.delete(nodeId); else next.add(nodeId);
            return next;
        });
    };

    // Auto-expand ancestor sections when a leaf is active
    useEffect(() => {
        // Handle static nav items
        const ancestors = ancestorMap.get(activeTab);
        if (ancestors) {
            setExpandedSections(prev => {
                const next = new Set(prev);
                let changed = false;
                for (const id of ancestors) {
                    if (!next.has(id)) { next.add(id); changed = true; }
                }
                return changed ? next : prev;
            });
        }

        // Handle dynamic groups: auto-expand the groups section
        if (activeTab.startsWith('groups')) {
            setExpandedSections(prev => {
                if (prev.has('groups')) return prev;
                const next = new Set(prev);
                next.add('groups');
                return next;
            });
        }
    }, [activeTab, ancestorMap]);

    // ── Flyout for collapsed groups ──────────────────────────────────────────
    const [flyoutId, setFlyoutId] = useState<string | null>(null);
    const flyoutTimer = useRef<ReturnType<typeof setTimeout>>();

    const openFlyout = (id: string) => { clearTimeout(flyoutTimer.current); setFlyoutId(id); };
    const closeFlyout = () => { flyoutTimer.current = setTimeout(() => setFlyoutId(null), 150); };
    const keepFlyout = () => clearTimeout(flyoutTimer.current);

    // ── Toggle helpers ───────────────────────────────────────────────────────
    const toggleSection = (id: string) => {
        setExpandedSections(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
        });
    };

    // ── Active helpers ───────────────────────────────────────────────────────
    // Exact match OR `<id>:<suffix>` deep-link match. Several tabs
    // accept a colon-suffix to deep-link a sub-target (e.g.
    // `groups:<groupId>`, `llm-models:<modelId>` from Mission
    // Control's System-Alerts links). The sidebar nav item should
    // still highlight when the user is on one of those sub-targets.
    const isActive = (id: string) => activeTab === id || activeTab.startsWith(`${id}:`);

    /** Is any descendant of this static nav node active? */
    const isDescendantActive = (node: { id: string; children?: { id: string; children?: { id: string }[] }[] }): boolean => {
        if (!node.children) return false;
        return node.children.some(c =>
            isActive(c.id) || ('children' in c && c.children ? c.children.some(gc => isActive(gc.id)) : false)
        );
    };

    /** Is the groups section active (any group selected)? */
    const isGroupsSectionActive = activeTab === 'groups' || activeTab.startsWith('groups:') || activeTab === 'groups-create';

    // ── Visibility filter ────────────────────────────────────────────────────
    const isVisible = (item: NavItem): boolean => {
        if (item.requiresTenantAdmin && !isTenantAdmin) return false;
        if (item.requiresPrincipal && item.requiresPrincipal !== principal?.id) return false;
        // Reachability, not just configuration: the trial compose always
        // sets BRUTOR_MCP_REGISTRY_INTERNAL_URL, including when the mcp-registry
        // profile is not running — only a registry that answers its
        // health probe earns the nav entry.
        if (!gateOpen(item.requiresGate)) return false;
        // Explicit permission gate. Hides the System nav from
        // anyone without `system:read` (the Tenant Auditor / Read-Only
        // roles get this; Tenant User does not). The sub-section pages
        // themselves re-check `system:operate` before enabling any
        // destructive buttons.
        if (item.requiresPermission && !hasPermission(item.requiresPermission)) return false;
        return true;
    };

    // Per-child permission gate. Children inherit their parent section's
    // admin gate; this only filters leaves that declare an explicit
    // `requiresPermission` (e.g. Governance → AI Assets).
    const isChildVisible = (child: NavChild): boolean => {
        if (!gateOpen(child.requiresGate)) return false;
        return !child.requiresPermission || hasPermission(child.requiresPermission);
    };
    // A leaf at the third level, or one of a sub-group's own leaves at the
    // fourth: both are hidden without the permission they name.
    const isLeafVisible = (leaf: NavLeaf): boolean =>
        !leaf.requiresPermission || hasPermission(leaf.requiresPermission);
    const visibleSubLeaves = (leaf: NavLeaf): NavLeaf[] => (leaf.children ?? []).filter(isLeafVisible);

    // ── Group tree renderer ─────────────────────────────────────────────────
    const renderGroupTreeNodes = (nodes: GroupTree[], level: number): React.ReactNode => {
        return nodes.map(node => {
            const Icon = resolveGroupIcon(node.icon, node.group_type);
            const color = resolveGroupColor(node.color, currentTheme);
            const nodeActive = activeTab === `groups:${node.id}`;
            const hasChildren = node.children && node.children.length > 0;
            const isExpanded = expandedGroupNodes.has(node.id);

            return (
                <div key={node.id}>
                    <button
                        onClick={() => onTabChange(`groups:${node.id}`)}
                        className={`
                            w-full flex items-center pr-3 py-[6px] rounded-lg text-[12px] transition-colors duration-150
                            ${nodeActive ? 'text-default font-medium' : 'text-muted hover:text-default hover:bg-surface-raised'}
                        `}
                        style={{
                            paddingLeft: `${36 + level * 16}px`,
                            backgroundColor: nodeActive ? `${primaryColor}10` : undefined,
                            borderLeft: nodeActive ? `3px solid ${primaryColor}` : '3px solid transparent',
                        }}
                    >
                        {/* Expand/collapse chevron */}
                        {hasChildren ? (
                            <span
                                className="w-4 h-4 flex items-center justify-center flex-shrink-0 mr-1"
                                onClick={(e) => { e.stopPropagation(); toggleGroupNode(node.id); }}
                            >
                                {isExpanded
                                    ? <ChevronDown className="w-3 h-3 text-muted" />
                                    : <ChevronRight className="w-3 h-3 text-muted" />
                                }
                            </span>
                        ) : (
                            <span className="w-4 h-4 flex-shrink-0 mr-1" />
                        )}

                        {/* Group icon */}
                        <span
                            className="w-5 h-5 rounded flex items-center justify-center flex-shrink-0 mr-1.5"
                            style={{ backgroundColor: color.bg }}
                        >
                            <Icon className="w-3 h-3" style={{ color: nodeActive ? primaryColor : color.fg }} />
                        </span>

                        {/* Name */}
                        <span className="truncate flex-1 text-left min-h-0 min-w-0">
                            {formatGroupLabel(node)}
                        </span>

                        {/* Where limits and policy live for this group */}
                        <GovernanceTreeBadges node={node} />

                        {/* No-inherit indicator */}
                        {!node.inherit_resources && node.parent_group_id && (
                            <span
                                className="ml-1 flex-shrink-0 text-amber-400"
                                title="Inheritance disabled"
                            >
                                <Unlink className="w-3 h-3" />
                            </span>
                        )}

                        {/* Inactive badge */}
                        {!node.is_active && (
                            <span className="ml-1 px-1 py-0.5 text-[9px] font-medium text-muted bg-surface-raised rounded flex-shrink-0">
                                Off
                            </span>
                        )}
                    </button>

                    {/* Children */}
                    {hasChildren && isExpanded && (
                        <div>{renderGroupTreeNodes(node.children, level + 1)}</div>
                    )}
                </div>
            );
        });
    };

    // ═════════════════════════════════════════════════════════════════════════
    // Render
    // ═════════════════════════════════════════════════════════════════════════
    const effectiveWidth = collapsed ? SIDEBAR_COLLAPSED_WIDTH : sidebarWidth;

    return (
        <div
            className="relative app-column h-full bg-surface border-r border-default select-none flex-shrink-0"
            style={{
                width: effectiveWidth,
                transition: isResizing.current ? 'none' : 'width 200ms ease-in-out',
            }}
        >
            {navGates.map((gate) => (
                <gate.Probe key={gate.id} onChange={(open) => reportGate(gate.id, open)} />
            ))}
            {/* ── Resize handle ──────────────────────────────────────── */}
            {!collapsed && (
                <div
                    className="absolute top-0 right-0 w-[5px] h-full cursor-col-resize z-50 group"
                    onMouseDown={handleResizeStart}
                    onDoubleClick={handleResizeDoubleClick}
                    title="Drag to resize, double-click to reset"
                >
                    <div className="w-[2px] h-full ml-auto bg-transparent group-hover:bg-blue-400/50 transition-colors duration-150" />
                </div>
            )}
            {/* ── Header ──────────────────────────────────────────────────── */}
            <div
                className="flex items-center justify-center px-2 py-2 flex-shrink-0"
                style={{ background: headerBackground }}
            >
                {collapsed ? (
                    <BrutorLogo size="small" mark showText={false} />
                ) : (
                    <div className="flex flex-col items-center">
                        <BrutorLogo size="small" showText={false} className="mb-0.5" />
                        <div className="text-[10px] font-bold text-white/80 uppercase tracking-[0.2em] leading-tight text-center">
                            Admin Console
                        </div>
                    </div>
                )}
            </div>

            {/* ── Navigation ──────────────────────────────────────────────── */}
            <nav className="scroll-region overflow-x-hidden py-2 px-2 space-y-0.5">
                {sections.map((section) => {
                    const visibleItems = section.items.filter(isVisible);
                    // A section whose items are all filtered out (role or
                    // permission) hides its header too — no orphan labels.
                    if (visibleItems.length === 0) return null;
                    return (
                        <React.Fragment key={section.label ?? 'root'}>
                            {section.label && (collapsed ? (
                                // Collapsed rail: headers become divider lines.
                                <div className="my-2 mx-2 border-t border-default" aria-hidden="true" />
                            ) : (
                                <div
                                    className="px-3 pt-4 pb-1 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted select-none"
                                    data-testid={`nav-section-${section.label.toLowerCase().replace(/\s+/g, '-')}`}
                                >
                                    <span>{section.label}</span>
                                    <span className="flex-1 border-t border-default min-h-0 min-w-0" aria-hidden="true" />
                                </div>
                            ))}
                            {visibleItems.map((item) => {
                    const Icon = item.icon;
                    const hasChildren = !!item.children && item.children.length > 0;
                    const isDynamicParent = !!item.dynamicChildren;
                    const showAsParent = hasChildren || isDynamicParent;

                    // Determine highlight state
                    let highlighted: boolean;
                    if (item.id === 'groups') {
                        highlighted = isGroupsSectionActive;
                    } else {
                        const groupActive = isDescendantActive(item);
                        const selfActive = isActive(item.id);
                        highlighted = selfActive || groupActive;
                    }

                    const sectionOpen = expandedSections.has(item.id) && !collapsed;

                    return (
                        <React.Fragment key={item.id}>
                            {/* ── Top-level item button ──────────────────── */}
                            <div
                                className="relative"
                                onMouseEnter={() => { if (collapsed && showAsParent) openFlyout(item.id); }}
                                onMouseLeave={() => { if (collapsed && showAsParent) closeFlyout(); }}
                            >
                                <button
                                    onClick={() => {
                                        if (showAsParent) {
                                            if (collapsed) {
                                                // Collapsed: navigate to first available item
                                                if (isDynamicParent) {
                                                    onTabChange('groups');
                                                } else {
                                                    onTabChange(item.children![0].children?.[0]?.id ?? item.children![0].id);
                                                }
                                            } else {
                                                toggleSection(item.id);
                                            }
                                        } else {
                                            onTabChange(item.id);
                                        }
                                    }}
                                    title={collapsed ? item.label : undefined}
                                    className={`
                                        w-full flex items-center rounded-lg transition-colors duration-150
                                        ${collapsed ? 'justify-center px-0 py-2.5' : 'px-3 py-2'}
                                        ${highlighted ? 'text-default font-medium' : 'text-secondary hover:text-default hover:bg-surface-raised'}
                                    `}
                                    style={{
                                        backgroundColor: highlighted ? `${primaryColor}12` : undefined,
                                        borderLeft: !collapsed && highlighted ? `3px solid ${primaryColor}` : !collapsed ? '3px solid transparent' : undefined,
                                    }}
                                >
                                    <Icon className="w-[18px] h-[18px] flex-shrink-0" style={{ color: highlighted ? primaryColor : undefined }} />
                                    {!collapsed && (
                                        <>
                                            <span className="ml-3 text-[13px] truncate flex-1 text-left min-h-0 min-w-0">{item.label}</span>
                                            {item.id === 'inbox' && inboxBadges && (inboxBadges.critical > 0 || inboxBadges.warning > 0) && (
                                                <span className="ml-1 flex items-center gap-1 flex-shrink-0" data-testid="inbox-badges">
                                                    {inboxBadges.critical > 0 && (
                                                        <span
                                                            className="px-1.5 py-0.5 rounded-full text-[10px] font-semibold leading-none bg-red-100 text-red-800"
                                                            title={`${inboxBadges.critical} critical finding${inboxBadges.critical === 1 ? '' : 's'} open`}
                                                            data-testid="inbox-badge-critical"
                                                        >
                                                            {inboxBadges.critical}
                                                        </span>
                                                    )}
                                                    {inboxBadges.warning > 0 && (
                                                        <span
                                                            className="px-1.5 py-0.5 rounded-full text-[10px] font-semibold leading-none bg-yellow-100 text-yellow-800"
                                                            title={`${inboxBadges.warning} warning finding${inboxBadges.warning === 1 ? '' : 's'} open`}
                                                            data-testid="inbox-badge-warning"
                                                        >
                                                            {inboxBadges.warning}
                                                        </span>
                                                    )}
                                                </span>
                                            )}
                                            {/* Create + Refresh group buttons inline */}
                                            {isDynamicParent && (
                                                <>
                                                    <span
                                                        className="p-0.5 rounded hover:bg-surface-inset text-muted hover:text-secondary transition-colors flex-shrink-0"
                                                        title="Refresh groups"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            onRefreshGroupTree();
                                                        }}
                                                    >
                                                        <RefreshCw className="w-3 h-3" />
                                                    </span>
                                                    <span
                                                        className="p-0.5 rounded hover:bg-surface-inset text-muted hover:text-secondary transition-colors flex-shrink-0 mr-1"
                                                        title="Create new group"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            onTabChange('groups-create');
                                                        }}
                                                    >
                                                        <Plus className="w-3.5 h-3.5" />
                                                    </span>
                                                </>
                                            )}
                                            {showAsParent && (
                                                <ChevronDown className={`w-3.5 h-3.5 text-muted transition-transform duration-200 flex-shrink-0 ${sectionOpen ? 'rotate-180' : ''}`} />
                                            )}
                                        </>
                                    )}
                                </button>

                                {/* ── Collapsed flyout ────────────────────── */}
                                {collapsed && showAsParent && flyoutId === item.id && (
                                    <div
                                        className="absolute left-full top-0 ml-1 z-50 min-w-[220px] bg-surface border border-default rounded-lg shadow-lg py-1"
                                        onMouseEnter={keepFlyout}
                                        onMouseLeave={closeFlyout}
                                    >
                                        <div className="px-3 py-2 text-xs font-semibold text-muted uppercase tracking-wider">
                                            {item.label}
                                        </div>

                                        {/* Dynamic group tree flyout */}
                                        {isDynamicParent ? (
                                            <div className="max-h-64 overflow-y-auto">
                                                {groupTreeLoading ? (
                                                    <div className="flex items-center justify-center py-4">
                                                        <Loader className="w-4 h-4 animate-spin text-muted" />
                                                    </div>
                                                ) : groupTreeFailure && groupTree.length === 0 ? (
                                                    <ReadFailureNotice compact className="px-3 py-3" failure={groupTreeFailure} onRetry={onRefreshGroupTree} />
                                                ) : groupTree.length === 0 ? (
                                                    <div className="px-3 py-3 text-xs text-muted text-center">No groups</div>
                                                ) : (
                                                    groupTree.map(node => {
                                                        const nodeActive = activeTab === `groups:${node.id}`;
                                                        return (
                                                            <button
                                                                key={node.id}
                                                                onClick={() => { onTabChange(`groups:${node.id}`); setFlyoutId(null); }}
                                                                className={`flex items-center w-full px-3 py-2 text-sm transition-colors ${
                                                                    nodeActive ? 'text-default font-medium' : 'text-secondary hover:text-default hover:bg-surface-alt'
                                                                }`}
                                                                style={{ backgroundColor: nodeActive ? `${primaryColor}10` : undefined }}
                                                            >
                                                                <span className="truncate">{formatGroupLabel(node)}</span>
                                                                {node.children?.length > 0 && (
                                                                    <span className="ml-auto text-[10px] text-muted">{node.children.length}</span>
                                                                )}
                                                            </button>
                                                        );
                                                    })
                                                )}
                                                <div className="border-t border-default mt-1 pt-1">
                                                    <button
                                                        onClick={() => { onTabChange('groups-create'); setFlyoutId(null); }}
                                                        className="flex items-center w-full px-3 py-2 text-sm text-muted hover:text-default hover:bg-surface-alt transition-colors"
                                                    >
                                                        <Plus className="w-3.5 h-3.5 mr-2" />
                                                        <span>New Group</span>
                                                    </button>
                                                </div>
                                            </div>
                                        ) : (
                                            /* Static children flyout (unchanged) */
                                            item.children!.filter(isChildVisible).map(child => {
                                                const ChildIcon = child.icon;
                                                if (child.children) {
                                                    return (
                                                        <div key={child.id}>
                                                            <div className="px-3 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-wider">
                                                                {child.label}
                                                            </div>
                                                            {child.children.filter(isLeafVisible).map(leaf => {
                                                                const LeafIcon = leaf.icon;

                                                                // 4th-level sub-group in the flyout: render its
                                                                // label as a sub-heading, then its leaves.
                                                                if (leaf.children) {
                                                                    return (
                                                                        <div key={leaf.id}>
                                                                            <div className="px-4 pt-2 pb-0.5 text-[10px] font-semibold text-muted uppercase tracking-wider">
                                                                                {leaf.label}
                                                                            </div>
                                                                            {visibleSubLeaves(leaf).map(sub => {
                                                                                const SubIcon = sub.icon;
                                                                                const subActive = isActive(sub.id);
                                                                                return (
                                                                                    <button
                                                                                        key={sub.id}
                                                                                        onClick={() => { onTabChange(sub.id); setFlyoutId(null); }}
                                                                                        className={`flex items-center w-full px-5 py-2 text-sm transition-colors ${
                                                                                            subActive ? 'text-default font-medium' : 'text-secondary hover:text-default hover:bg-surface-alt'
                                                                                        }`}
                                                                                        style={{ backgroundColor: subActive ? `${primaryColor}10` : undefined }}
                                                                                    >
                                                                                        <SubIcon className="w-3.5 h-3.5 mr-2.5 flex-shrink-0" style={{ color: subActive ? primaryColor : undefined }} />
                                                                                        <span>{sub.label}</span>
                                                                                    </button>
                                                                                );
                                                                            })}
                                                                        </div>
                                                                    );
                                                                }

                                                                const leafActive = isActive(leaf.id);
                                                                return (
                                                                    <button
                                                                        key={leaf.id}
                                                                        onClick={() => { onTabChange(leaf.id); setFlyoutId(null); }}
                                                                        className={`flex items-center w-full px-4 py-2 text-sm transition-colors ${
                                                                            leafActive ? 'text-default font-medium' : 'text-secondary hover:text-default hover:bg-surface-alt'
                                                                        }`}
                                                                        style={{ backgroundColor: leafActive ? `${primaryColor}10` : undefined }}
                                                                    >
                                                                        <LeafIcon className="w-3.5 h-3.5 mr-2.5 flex-shrink-0" style={{ color: leafActive ? primaryColor : undefined }} />
                                                                        <span>{leaf.label}</span>
                                                                    </button>
                                                                );
                                                            })}
                                                        </div>
                                                    );
                                                }
                                                const childActive = isActive(child.id);
                                                return (
                                                    <button
                                                        key={child.id}
                                                        onClick={() => { onTabChange(child.id); setFlyoutId(null); }}
                                                        className={`flex items-center w-full px-3 py-2 text-sm transition-colors ${
                                                            childActive ? 'text-default font-medium' : 'text-secondary hover:text-default hover:bg-surface-alt'
                                                        }`}
                                                        style={{ backgroundColor: childActive ? `${primaryColor}10` : undefined }}
                                                    >
                                                        <ChildIcon className="w-4 h-4 mr-2.5 flex-shrink-0" style={{ color: childActive ? primaryColor : undefined }} />
                                                        <span>{child.label}</span>
                                                    </button>
                                                );
                                            })
                                        )}
                                    </div>
                                )}
                            </div>

                            {/* ── Dynamic group tree (expanded, not collapsed) ── */}
                            {!collapsed && isDynamicParent && (
                                <div
                                    className="overflow-hidden transition-all duration-200"
                                    style={{
                                        maxHeight: sectionOpen ? '2000px' : '0px',
                                        opacity: sectionOpen ? 1 : 0,
                                    }}
                                >
                                    {groupTreeLoading ? (
                                        <div className="flex items-center justify-center py-3">
                                            <Loader className="w-4 h-4 animate-spin text-muted" />
                                        </div>
                                    ) : groupTreeFailure ? (
                                        <>
                                            <ReadFailureNotice compact className="pl-9 pr-3 py-2" failure={groupTreeFailure} onRetry={onRefreshGroupTree} />
                                            {groupTree.length > 0 && <div className="mt-0.5">{renderGroupTreeNodes(groupTree, 0)}</div>}
                                        </>
                                    ) : groupTree.length === 0 ? (
                                        <div className="pl-9 pr-3 py-2 text-[11px] text-muted italic">
                                            No groups yet
                                        </div>
                                    ) : (
                                        <div className="mt-0.5">
                                            {renderGroupTreeNodes(groupTree, 0)}
                                        </div>
                                    )}
                                </div>
                            )}

                            {/* ── Static children (level 1) ──────────────── */}
                            {!collapsed && hasChildren && !isDynamicParent && (
                                <div
                                    className="overflow-hidden transition-all duration-200"
                                    style={{
                                        maxHeight: sectionOpen
                                            ? `${item.children!.reduce((sum, c) => sum + countChildLeaves(c, expandedSections) * 34, 0)}px`
                                            : '0px',
                                        opacity: sectionOpen ? 1 : 0,
                                    }}
                                >
                                    {item.children!.filter(isChildVisible).map(child => {
                                        const ChildIcon = child.icon;
                                        const hasGrandchildren = !!child.children;
                                        const childActive = isActive(child.id) || isDescendantActive(child);
                                        const subOpen = expandedSections.has(child.id);

                                        return (
                                            <React.Fragment key={child.id}>
                                                <button
                                                    onClick={() => {
                                                        if (hasGrandchildren) {
                                                            toggleSection(child.id);
                                                        } else {
                                                            onTabChange(child.id);
                                                        }
                                                    }}
                                                    className={`
                                                        w-full flex items-center pl-9 pr-3 py-[7px] rounded-lg text-[13px] transition-colors duration-150
                                                        ${childActive ? 'text-default font-medium' : 'text-muted hover:text-default hover:bg-surface-raised'}
                                                    `}
                                                    style={{
                                                        backgroundColor: childActive ? `${primaryColor}10` : undefined,
                                                        borderLeft: childActive ? `3px solid ${primaryColor}` : '3px solid transparent',
                                                    }}
                                                >
                                                    <ChildIcon className="w-4 h-4 flex-shrink-0" style={{ color: childActive ? primaryColor : undefined }} />
                                                    <span className="ml-2.5 truncate flex-1 text-left min-h-0 min-w-0">{child.label}</span>
                                                    {hasGrandchildren && (
                                                        <ChevronDown className={`w-3 h-3 text-muted transition-transform duration-200 flex-shrink-0 ${subOpen ? 'rotate-180' : ''}`} />
                                                    )}
                                                </button>

                                                {hasGrandchildren && (
                                                    <div
                                                        className="overflow-hidden transition-all duration-200"
                                                        style={{
                                                            maxHeight: subOpen
                                                                ? `${child.children!.reduce((s, leaf) => s + 1 + (leaf.children && expandedSections.has(leaf.id) ? leaf.children.length : 0), 0) * 34}px`
                                                                : '0px',
                                                            opacity: subOpen ? 1 : 0,
                                                        }}
                                                    >
                                                        {child.children!.filter(isLeafVisible).map(leaf => {
                                                            const LeafIcon = leaf.icon;

                                                            // 4th-level sub-group (e.g. Cache → Response/Prompt):
                                                            // a clickable header that toggles, then its own
                                                            // deeper-indented leaves.
                                                            if (leaf.children) {
                                                                const subSubOpen = expandedSections.has(leaf.id);
                                                                const groupActive = isActive(leaf.id) || leaf.children.some(s => isActive(s.id));
                                                                return (
                                                                    <React.Fragment key={leaf.id}>
                                                                        <button
                                                                            onClick={() => toggleSection(leaf.id)}
                                                                            className={`
                                                                                w-full flex items-center pl-[52px] pr-3 py-[6px] rounded-lg text-[12px] transition-colors duration-150
                                                                                ${groupActive ? 'text-default font-medium' : 'text-muted hover:text-default hover:bg-surface-raised'}
                                                                            `}
                                                                            style={{
                                                                                backgroundColor: groupActive ? `${primaryColor}10` : undefined,
                                                                                borderLeft: groupActive ? `3px solid ${primaryColor}` : '3px solid transparent',
                                                                            }}
                                                                        >
                                                                            <LeafIcon className="w-3.5 h-3.5 flex-shrink-0" style={{ color: groupActive ? primaryColor : undefined }} />
                                                                            <span className="ml-2 truncate flex-1 text-left min-h-0 min-w-0">{leaf.label}</span>
                                                                            <ChevronDown className={`w-3 h-3 text-muted transition-transform duration-200 flex-shrink-0 ${subSubOpen ? 'rotate-180' : ''}`} />
                                                                        </button>
                                                                        <div
                                                                            className="overflow-hidden transition-all duration-200"
                                                                            style={{
                                                                                maxHeight: subSubOpen ? `${visibleSubLeaves(leaf).length * 34}px` : '0px',
                                                                                opacity: subSubOpen ? 1 : 0,
                                                                            }}
                                                                        >
                                                                            {visibleSubLeaves(leaf).map(sub => {
                                                                                const SubIcon = sub.icon;
                                                                                const subActive = isActive(sub.id);
                                                                                return (
                                                                                    <button
                                                                                        key={sub.id}
                                                                                        onClick={() => onTabChange(sub.id)}
                                                                                        className={`
                                                                                            w-full flex items-center pl-[64px] pr-3 py-[6px] rounded-lg text-[12px] transition-colors duration-150
                                                                                            ${subActive ? 'text-default font-medium' : 'text-muted hover:text-default hover:bg-surface-raised'}
                                                                                        `}
                                                                                        style={{
                                                                                            backgroundColor: subActive ? `${primaryColor}10` : undefined,
                                                                                            borderLeft: subActive ? `3px solid ${primaryColor}` : '3px solid transparent',
                                                                                        }}
                                                                                    >
                                                                                        <SubIcon className="w-3.5 h-3.5 flex-shrink-0" style={{ color: subActive ? primaryColor : undefined }} />
                                                                                        <span className="ml-2 truncate">{sub.label}</span>
                                                                                    </button>
                                                                                );
                                                                            })}
                                                                        </div>
                                                                    </React.Fragment>
                                                                );
                                                            }

                                                            const leafActive = isActive(leaf.id);
                                                            return (
                                                                <button
                                                                    key={leaf.id}
                                                                    onClick={() => onTabChange(leaf.id)}
                                                                    className={`
                                                                        w-full flex items-center pl-[52px] pr-3 py-[6px] rounded-lg text-[12px] transition-colors duration-150
                                                                        ${leafActive ? 'text-default font-medium' : 'text-muted hover:text-default hover:bg-surface-raised'}
                                                                    `}
                                                                    style={{
                                                                        backgroundColor: leafActive ? `${primaryColor}10` : undefined,
                                                                        borderLeft: leafActive ? `3px solid ${primaryColor}` : '3px solid transparent',
                                                                    }}
                                                                >
                                                                    <LeafIcon className="w-3.5 h-3.5 flex-shrink-0" style={{ color: leafActive ? primaryColor : undefined }} />
                                                                    <span className="ml-2 truncate">{leaf.label}</span>
                                                                </button>
                                                            );
                                                        })}
                                                    </div>
                                                )}
                                            </React.Fragment>
                                        );
                                    })}
                                </div>
                            )}
                                </React.Fragment>
                            );
                            })}
                        </React.Fragment>
                    );
                })}
            </nav>

            {/* ── Error indicator ──────────────────────────────────────────── */}
            {hasErrors && (
                <div className={`mx-2 mb-1 flex items-center rounded-lg px-3 py-2 text-xs ${
                    hasCriticalErrors ? 'bg-red-50 text-red-700' : 'bg-yellow-50 text-yellow-700'
                }`}>
                    <AlertCircle className={`w-4 h-4 flex-shrink-0 ${hasCriticalErrors ? 'text-red-500' : 'text-yellow-500'}`} />
                    {!collapsed && (
                        <span className="ml-2 truncate">{errorCount} error{errorCount !== 1 ? 's' : ''}</span>
                    )}
                </div>
            )}

            {/* ── User & Collapse ──────────────────────────────────────────── */}
            <div className="flex-shrink-0 border-t border-default">
                <div className={`flex items-center px-3 py-2.5 ${collapsed ? 'justify-center' : ''}`}>
                    <UserAvatar avatarUrl={avatarUrl} name={username} size="sm" />
                    {!collapsed && (
                        <div className="ml-2.5 flex-1 min-w-0 min-h-0">
                            <div className="text-sm font-medium text-default truncate">{username}</div>
                            {principal ? (
                                <div className="text-[10px] text-muted truncate">{principal.label}</div>
                            ) : isTenantAdmin && (
                                <>
                                    {tenantDisplayName && tenantDisplayName !== tenantId && (
                                        <div className="text-[10px] text-muted truncate" title={tenantDisplayName}>
                                            {tenantDisplayName}
                                        </div>
                                    )}
                                    <div className="text-[10px] text-muted truncate" title={tenantId}>
                                        {tenantId}
                                    </div>
                                </>
                            )}
                        </div>
                    )}
                    {!collapsed && (
                        <button
                            onClick={onLogout}
                            className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors flex-shrink-0"
                            title="Logout"
                        >
                            <LogOut className="w-4 h-4" />
                        </button>
                    )}
                </div>
                <div className={`flex items-center border-t border-default px-3 py-1.5 ${collapsed ? 'justify-center' : 'justify-between'}`}>
                    {!collapsed && <span className="text-[10px] text-muted">v{__APP_VERSION__}</span>}
                    <div className="flex items-center gap-0.5">
                        <button
                            onClick={() => setCurrentTheme(currentTheme === 'dark' ? 'light' : 'dark')}
                            className="p-1 text-muted hover:text-secondary hover:bg-surface-raised rounded transition-colors"
                            title={currentTheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
                        >
                            {currentTheme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
                        </button>
                        <button
                            onClick={() => setCollapsed(c => !c)}
                            className="p-1 text-muted hover:text-secondary hover:bg-surface-raised rounded transition-colors"
                            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                        >
                            {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default AppSidebar;
