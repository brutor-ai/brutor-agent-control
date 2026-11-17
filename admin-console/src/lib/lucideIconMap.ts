/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * lucideIconMap.ts
 *
 * Shared mapping of icon name strings ↔ lucide-react components.
 * Used to persist icon choices as strings (server-side) and resolve
 * them back to React components (client-side).
 *
 * IMPORTANT: Only icons confirmed to exist in the project's lucide-react
 * version are included. Do not add newer icons without verifying.
 */

import {
    // Organizational
    Building2,
    Building,
    Briefcase,
    Package,
    Users,
    User,
    Crown,
    Landmark,
    Store,

    // Work / Projects
    FolderKanban,
    FolderTree,
    FolderOpen,
    Folder,
    Target,
    Layers,
    LayoutGrid,
    Calendar,
    List,

    // Tech / AI
    AppWindow,
    Bot,
    Sparkles,
    Server,
    Brain,
    Cpu,
    Database,
    HardDrive,
    Cloud,
    Globe,
    Wifi,
    Terminal,
    Code2,

    // Communication
    MessageSquare,
    Mail,
    Phone,
    Video,
    Bell,

    // Security / Access
    Shield,
    ShieldCheck,
    Lock,
    Key,
    Fingerprint,
    Eye,

    // Analytics / Business
    TrendingUp,
    PieChart,
    Activity,
    DollarSign,
    CreditCard,

    // Creative / Content
    Palette,
    Pen,
    Image,
    FileText,
    BookOpen,
    Lightbulb,

    // Navigation / Symbols
    Compass,
    Map,
    Navigation,
    Rocket,
    Zap,
    Flame,
    Star,
    Heart,
    Award,
    Trophy,
    Gem,
    Home,

    // Infrastructure
    Settings,
    Wrench,
    Cog,
    Network,
    GitBranch,
    Share2,
    Link,
    Box,

    type LucideIcon
} from 'lucide-react';

// ============================================================================
// ICON REGISTRY
// ============================================================================

export interface IconEntry {
    component: LucideIcon;
    label: string;
    category: string;
}

/**
 * Master icon registry — keyed by the string name stored on the server.
 * Categories are used by the picker for grouping.
 */
export const ICON_REGISTRY: Record<string, IconEntry> = {
    // Organizational
    building2:      { component: Building2,     label: 'Organization',   category: 'Organizational' },
    building:       { component: Building,      label: 'Building',       category: 'Organizational' },
    briefcase:      { component: Briefcase,     label: 'Briefcase',      category: 'Organizational' },
    package:        { component: Package,       label: 'Package',        category: 'Organizational' },
    users:          { component: Users,         label: 'Team',           category: 'Organizational' },
    user:           { component: User,          label: 'User',           category: 'Organizational' },
    crown:          { component: Crown,         label: 'Crown',          category: 'Organizational' },
    landmark:       { component: Landmark,      label: 'Landmark',       category: 'Organizational' },
    store:          { component: Store,         label: 'Store',          category: 'Organizational' },

    // Work / Projects
    'folder-kanban': { component: FolderKanban, label: 'Kanban',         category: 'Work' },
    'folder-tree':   { component: FolderTree,   label: 'Folder Tree',    category: 'Work' },
    'folder-open':   { component: FolderOpen,   label: 'Folder Open',    category: 'Work' },
    folder:          { component: Folder,       label: 'Folder',         category: 'Work' },
    target:          { component: Target,       label: 'Target',         category: 'Work' },
    layers:          { component: Layers,       label: 'Layers',         category: 'Work' },
    'layout-grid':   { component: LayoutGrid,   label: 'Grid',           category: 'Work' },
    calendar:        { component: Calendar,     label: 'Calendar',       category: 'Work' },
    list:            { component: List,         label: 'List',           category: 'Work' },

    // Tech / AI
    'app-window':    { component: AppWindow,    label: 'App',            category: 'Tech' },
    bot:             { component: Bot,          label: 'Bot',            category: 'Tech' },
    sparkles:        { component: Sparkles,     label: 'AI / Sparkles',  category: 'Tech' },
    server:          { component: Server,       label: 'Server',         category: 'Tech' },
    brain:           { component: Brain,        label: 'Brain',          category: 'Tech' },
    cpu:             { component: Cpu,          label: 'CPU',            category: 'Tech' },
    database:        { component: Database,     label: 'Database',       category: 'Tech' },
    'hard-drive':    { component: HardDrive,    label: 'Storage',        category: 'Tech' },
    cloud:           { component: Cloud,        label: 'Cloud',          category: 'Tech' },
    globe:           { component: Globe,        label: 'Globe',          category: 'Tech' },
    wifi:            { component: Wifi,         label: 'WiFi',           category: 'Tech' },
    terminal:        { component: Terminal,     label: 'Terminal',       category: 'Tech' },
    code2:           { component: Code2,        label: 'Code',           category: 'Tech' },

    // Communication
    'message-square':{ component: MessageSquare,label: 'Chat',           category: 'Communication' },
    mail:            { component: Mail,         label: 'Email',          category: 'Communication' },
    phone:           { component: Phone,        label: 'Phone',          category: 'Communication' },
    video:           { component: Video,        label: 'Video',          category: 'Communication' },
    bell:            { component: Bell,         label: 'Notifications',  category: 'Communication' },

    // Security / Access
    shield:          { component: Shield,       label: 'Shield',         category: 'Security' },
    'shield-check':  { component: ShieldCheck,  label: 'Verified',       category: 'Security' },
    lock:            { component: Lock,         label: 'Lock',           category: 'Security' },
    key:             { component: Key,          label: 'Key',            category: 'Security' },
    fingerprint:     { component: Fingerprint,  label: 'Fingerprint',    category: 'Security' },
    eye:             { component: Eye,          label: 'View',           category: 'Security' },

    // Analytics / Business
    'trending-up':   { component: TrendingUp,   label: 'Trending',       category: 'Analytics' },
    'pie-chart':     { component: PieChart,     label: 'Pie Chart',      category: 'Analytics' },
    activity:        { component: Activity,     label: 'Activity',       category: 'Analytics' },
    'dollar-sign':   { component: DollarSign,   label: 'Dollar',         category: 'Analytics' },
    'credit-card':   { component: CreditCard,   label: 'Card',           category: 'Analytics' },

    // Creative / Content
    palette:         { component: Palette,      label: 'Palette',        category: 'Creative' },
    pen:             { component: Pen,          label: 'Pen',            category: 'Creative' },
    image:           { component: Image,        label: 'Image',          category: 'Creative' },
    'file-text':     { component: FileText,     label: 'Document',       category: 'Creative' },
    'book-open':     { component: BookOpen,     label: 'Book',           category: 'Creative' },
    lightbulb:       { component: Lightbulb,    label: 'Idea',           category: 'Creative' },

    // Symbols
    compass:         { component: Compass,      label: 'Compass',        category: 'Symbols' },
    map:             { component: Map,          label: 'Map',            category: 'Symbols' },
    navigation:      { component: Navigation,   label: 'Navigate',       category: 'Symbols' },
    rocket:          { component: Rocket,       label: 'Rocket',         category: 'Symbols' },
    zap:             { component: Zap,          label: 'Zap',            category: 'Symbols' },
    flame:           { component: Flame,        label: 'Flame',          category: 'Symbols' },
    star:            { component: Star,         label: 'Star',           category: 'Symbols' },
    heart:           { component: Heart,        label: 'Heart',          category: 'Symbols' },
    award:           { component: Award,        label: 'Award',          category: 'Symbols' },
    trophy:          { component: Trophy,       label: 'Trophy',         category: 'Symbols' },
    gem:             { component: Gem,          label: 'Gem',            category: 'Symbols' },
    home:            { component: Home,         label: 'Home',           category: 'Symbols' },

    // Infrastructure
    settings:        { component: Settings,     label: 'Settings',       category: 'Infrastructure' },
    wrench:          { component: Wrench,       label: 'Wrench',         category: 'Infrastructure' },
    cog:             { component: Cog,          label: 'Cog',            category: 'Infrastructure' },
    network:         { component: Network,      label: 'Network',        category: 'Infrastructure' },
    'git-branch':    { component: GitBranch,    label: 'Branch',         category: 'Infrastructure' },
    share2:          { component: Share2,       label: 'Share',          category: 'Infrastructure' },
    link:            { component: Link,         label: 'Link',           category: 'Infrastructure' },
    box:             { component: Box,          label: 'Box',            category: 'Infrastructure' },
};

// ============================================================================
// CATEGORY LIST (ordered)
// ============================================================================

export const ICON_CATEGORIES = [
    'Organizational',
    'Work',
    'Tech',
    'Communication',
    'Security',
    'Analytics',
    'Creative',
    'Symbols',
    'Infrastructure',
] as const;

// ============================================================================
// GROUP TYPE → DEFAULT ICON MAPPING
// ============================================================================

/**
 * Maps GroupType enum values to their default icon keys.
 * Used as fallback when no custom icon is set.
 */
export const GROUP_TYPE_DEFAULT_ICONS: Record<string, string> = {
    organization:  'building2',
    division:      'building',
    department:    'briefcase',
    business_unit: 'package',
    team:          'users',
    project:       'folder-kanban',
    program:       'folder-tree',
    initiative:    'target',
    ai_system:     'layers',
    custom:        'folder',
};

/**
 * Maps AI System kinds to their default icon keys.
 *
 * The distinction between agent, assistant, application and service lives
 * on `system_kind`, and the iconography follows it — an AI System of kind
 * `agent` should look like an agent in the tree, not like a generic box.
 */
export const SYSTEM_KIND_DEFAULT_ICONS: Record<string, string> = {
    agent:       'bot',
    assistant:   'sparkles',
    application: 'app-window',
    integration: 'share2',
    workflow:    'git-branch',
    service:     'server',
};

// ============================================================================
// RESOLVER
// ============================================================================

/**
 * Resolve an icon name string to a lucide-react component.
 *
 * Falls back through: custom icon → AI System kind default → group type
 * default → Folder.
 *
 * The kind is checked before the group type because `ai_system` is the
 * broad category and the kind is the specific one — an AI System of kind
 * `agent` should read as an agent at a glance, not as a generic stack of
 * layers. `systemKind` is only ever set on AI-System groups, so this ordering cannot affect any other group type.
 */
export function resolveGroupIcon(
    iconName?: string,
    groupType?: string,
    systemKind?: string | null
): LucideIcon {
    // 1. Try custom icon
    if (iconName && ICON_REGISTRY[iconName]) {
        return ICON_REGISTRY[iconName].component;
    }

    // 2. Try AI System kind default (more specific than the group type)
    if (systemKind) {
        const kindKey = SYSTEM_KIND_DEFAULT_ICONS[systemKind];
        if (kindKey && ICON_REGISTRY[kindKey]) {
            return ICON_REGISTRY[kindKey].component;
        }
    }

    // 3. Try group type default
    if (groupType) {
        const defaultKey = GROUP_TYPE_DEFAULT_ICONS[groupType];
        if (defaultKey && ICON_REGISTRY[defaultKey]) {
            return ICON_REGISTRY[defaultKey].component;
        }
    }

    // 4. Fallback
    return Folder;
}

/**
 * Get icon entry by name (for label, category access)
 */
export function getIconEntry(iconName: string): IconEntry | undefined {
    return ICON_REGISTRY[iconName];
}

/**
 * Get all icon names
 */
export function getAllIconNames(): string[] {
    return Object.keys(ICON_REGISTRY);
}

/**
 * Get icon names by category
 */
export function getIconsByCategory(category: string): string[] {
    return Object.entries(ICON_REGISTRY)
        .filter(([_, entry]) => entry.category === category)
        .map(([name]) => name);
}

// ============================================================================
// COLOR PALETTE
// ============================================================================

export interface ColorEntry {
    label: string;
    /** Foreground (icon) hex */
    fg: string;
    /** Background (badge) hex — light mode */
    bg: string;
    /** Background (badge) hex — dark mode */
    bgDark: string;
}

/**
 * Predefined color palette for group icons.
 * Keys are stored on the server in the `color` field.
 * Rendering uses inline styles to avoid Tailwind purge issues.
 */
export const COLOR_PALETTE: Record<string, ColorEntry> = {
    sky:      { label: 'Sky',      fg: '#0284c7', bg: '#f0f9ff', bgDark: '#0c2d3f' },
    blue:     { label: 'Blue',     fg: '#2563eb', bg: '#eff6ff', bgDark: '#172554' },
    indigo:   { label: 'Indigo',   fg: '#4f46e5', bg: '#eef2ff', bgDark: '#1e1b4b' },
    violet:   { label: 'Violet',   fg: '#7c3aed', bg: '#f5f3ff', bgDark: '#2e1065' },
    purple:   { label: 'Purple',   fg: '#9333ea', bg: '#faf5ff', bgDark: '#3b0764' },
    fuchsia:  { label: 'Fuchsia',  fg: '#c026d3', bg: '#fdf4ff', bgDark: '#4a044e' },
    pink:     { label: 'Pink',     fg: '#db2777', bg: '#fdf2f8', bgDark: '#500724' },
    rose:     { label: 'Rose',     fg: '#e11d48', bg: '#fff1f2', bgDark: '#4c0519' },
    red:      { label: 'Red',      fg: '#dc2626', bg: '#fef2f2', bgDark: '#450a0a' },
    orange:   { label: 'Orange',   fg: '#ea580c', bg: '#fff7ed', bgDark: '#431407' },
    amber:    { label: 'Amber',    fg: '#d97706', bg: '#fffbeb', bgDark: '#451a03' },
    yellow:   { label: 'Yellow',   fg: '#ca8a04', bg: '#fefce8', bgDark: '#422006' },
    lime:     { label: 'Lime',     fg: '#65a30d', bg: '#f7fee7', bgDark: '#1a2e05' },
    green:    { label: 'Green',    fg: '#16a34a', bg: '#f0fdf4', bgDark: '#052e16' },
    emerald:  { label: 'Emerald',  fg: '#059669', bg: '#ecfdf5', bgDark: '#022c22' },
    teal:     { label: 'Teal',     fg: '#0d9488', bg: '#f0fdfa', bgDark: '#042f2e' },
    cyan:     { label: 'Cyan',     fg: '#0891b2', bg: '#ecfeff', bgDark: '#083344' },
    slate:    { label: 'Slate',    fg: '#475569', bg: '#f8fafc', bgDark: '#1e293b' },
    gray:     { label: 'Gray',     fg: '#4b5563', bg: '#f9fafb', bgDark: '#1f2937' },
    stone:    { label: 'Stone',    fg: '#57534e', bg: '#fafaf9', bgDark: '#1c1917' },
};

/** Default color key used when no custom color is set */
export const DEFAULT_COLOR_KEY = 'sky';

/**
 * Resolve a color key to foreground/background hex values.
 * Falls back to sky blue (the current default).
 */
export function resolveGroupColor(colorKey?: string, theme?: string): ColorEntry {
    const entry = (colorKey && COLOR_PALETTE[colorKey])
        ? COLOR_PALETTE[colorKey]
        : COLOR_PALETTE[DEFAULT_COLOR_KEY];

    // Use dark background when dark theme is active
    const isDark = theme
        ? theme === 'dark'
        : document.documentElement.getAttribute('data-theme') === 'dark';
    if (isDark) {
        return { ...entry, bg: entry.bgDark };
    }
    return entry;
}

/**
 * Get all color keys
 */
export function getAllColorKeys(): string[] {
    return Object.keys(COLOR_PALETTE);
}
