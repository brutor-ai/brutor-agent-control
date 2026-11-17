/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Slots: named places in a page where items contributed from outside the page
 * are rendered.
 *
 * A page that has a slot does not import what fills it. It asks for the items
 * of the slot (`useSlot`) and renders them, passing each the props the slot
 * defines. With nothing contributed, the page renders without them. That is
 * what lets another distribution add a tab to a page it does not own, and lets
 * the page exist without that tab.
 *
 * `SlotItems` is the list of slots: its keys are the slot names and each value
 * says what an item of that slot looks like and what it is given. Adding a
 * slot means adding a key here and rendering it in the host page.
 *
 * The items come from the nearest `SlotProvider`. `App.tsx` provides the
 * built-in ones (`builtinSlots.tsx`). Outside a provider every slot is empty.
 */

import { createContext, useContext } from 'react';
import type React from 'react';

import type { EnterpriseFeature } from './editions';
import type { useAuth } from '../hooks/useAuth';
import type { PasswordSignIn } from '../lib/gatewayApi/base';
import type { GatewayApiClient } from '../lib/gatewayApiClient';
import type { AssetCoverageData, AssetCoverageResponse } from '../lib/assetCoverageTypes';
import type { LLModelInfo, ResourceGroup, SystemAlert } from '../lib/groupTypes';
import type { LLModelCreateRequest } from '../lib/llmTypes';
import type { CatalogImportRequest, MCPCatalogEntry } from '../lib/mcpCatalogTypes';
import type { TenantModelOption } from '../lib/tenantModelOptions';
import type { ApiSendableBody } from '../lib/api/controlPlaneTypes';
import type { components } from '../lib/generated/control-plane-api';

export type SlotIcon = React.ComponentType<{ className?: string; style?: React.CSSProperties }>;

/** Something rendered in a slot. `P` is what the host page passes to it. */
export interface SlotItem<P> {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    render: (props: P) => React.ReactNode;
}

/** A tab contributed to a host page's tab strip. */
export interface TabSlotItem<P> extends SlotItem<P> {
    label: string;
    icon: SlotIcon;
    /**
     * The id of the tab this one is placed after. Absent, or naming a tab
     * that does not exist: the tab is placed last.
     */
    after?: string;
}

/** What an import source hands the MCP server form to fill itself from. */
export interface ImportedServer {
    name: string;
    description: string;
    /** The server's MCP URL, when the source knows one. */
    url?: string;
}

/**
 * A way to import an MCP server into the server form: a section that offers
 * it and the dialog it opens. The form holds which dialog is open.
 */
export interface ServerImportSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** Shown at the top of the General tab while a server is being created. */
    Section: React.ComponentType<{ onOpen: () => void }>;
    /** Mounted for as long as the form is, open or not. */
    Dialog: React.ComponentType<{
        creating: boolean;
        isOpen: boolean;
        onClose: () => void;
        onImport: (server: ImportedServer) => void;
    }>;
}

/**
 * Another place to find a model from the LLM Models page: a button in the
 * action bar and the dialog it opens. The dialog hands back the start of a
 * new model; the page opens its create form with it.
 */
export interface ModelImportSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** Shown in the action bar, after Browse and Search. */
    Button: React.ComponentType<{ onOpen: () => void }>;
    /** Mounted for as long as the page is, open or not. */
    Dialog: React.ComponentType<{
        isOpen: boolean;
        onClose: () => void;
        onCreateModel: (model: Partial<LLModelCreateRequest>) => void;
    }>;
}

/** What the tenant settings page passes to a contributed sub-tab. */
export interface TenantSettingsSlotProps {
    /** The tenant being shown, as loaded by the page. */
    tenant: {
        id: string;
        name: string;
        quotas?: Record<string, number | null>;
        features?: Record<string, boolean>;
        billing_plan?: string | null;
        billing_email?: string | null;
    };
}

/**
 * A contributed sub-tab of the tenant settings page. It has no part in the
 * page's save: it is read-only (the page marks it so), or it saves what it
 * shows with a button of its own (`savesItself`: no read-only mark).
 */
export interface TenantSettingsTabSlotItem extends TabSlotItem<TenantSettingsSlotProps> {
    savesItself?: boolean;
}

/** What the tenant settings page passes to a contributed model section. */
/**
 * A field of the tenant's settings that holds the id of a model, by the
 * name the update of the settings gives it: a contributed section names one
 * of these, so what it adds to the save is a field the contract has.
 */
export type TenantModelField = Extract<keyof ApiSendableBody<'/v1/admin/tenants/current', 'patch'>, `default_${string}_model_id`>;

export interface TenantModelSectionProps {
    tenantId: string;
    /** The model stored for the tenant, or '' for none. */
    stored: string;
    /** The models to pick from, as the section's `loadModels` answered. */
    models: TenantModelOption[];
    /** The model picked, or '' for none. Held by the page; saved with it. */
    value: string;
    onChange: (modelId: string) => void;
    /** Another model is picked than the one the page loaded or last saved. */
    changed: boolean;
    /** Save was pressed once with a changed model: the next press saves. */
    confirming: boolean;
}

/**
 * A model the tenant has one of, picked on the Default Models sub-tab of the
 * tenant settings page, above the judge model. The page loads and saves the
 * field; a change to another model needs Save pressed twice, and the section
 * says in between what the change costs. The section stays mounted while the
 * page is open.
 */
export interface TenantModelSectionSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    title: string;
    icon: SlotIcon;
    /** The field of the tenant that holds the model's id, as the update of the tenant's settings names it. */
    field: TenantModelField;
    /** Part of the page's load: when it fails the page does not open. */
    loadModels: (client: { request: (path: string) => Promise<unknown>; listLLMs: GatewayApiClient['listLLMs'] }) => Promise<TenantModelOption[]>;
    Section: React.ComponentType<TenantModelSectionProps>;
}

/** What the models table of a Resource Group passes to a contributed cell. */
export interface ModelColumnCellProps {
    group: ResourceGroup;
    model: LLModelInfo;
    /** Call before writing anything: the tab clears its error. */
    onBeforeChange: () => void;
    /** Call after a write: the tab reloads its models. */
    onChanged: () => Promise<void>;
}

/** A column added to the models table of a Resource Group, before Actions. */
export interface ModelColumnSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    header: string;
    /** Tooltip of the column header. */
    headerTitle?: string;
    Cell: React.ComponentType<ModelColumnCellProps>;
}

/** What the Mission Control alerts drawer passes to a contributed action. */
export interface AlertActionProps {
    alert: SystemAlert;
    /** The action is in flight. Held by the drawer's owner, so it is still
     *  true when the drawer has been closed and reopened meanwhile. */
    busy: boolean;
    setBusy: (busy: boolean) => void;
    /** Call when the action has resolved the alert: it leaves the list. */
    onResolved: () => void;
}

/** An action offered on a system alert in the Mission Control alerts drawer. */
export interface AlertActionSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** Whether the action is offered for this alert. */
    appliesTo: (alert: SystemAlert) => boolean;
    Action: React.ComponentType<AlertActionProps>;
}

/** What the sidebar passes to the probe of a gate. */
export interface NavGateProbeProps {
    /** Report whether the gate is open. Until a probe reports, it is closed. */
    onChange: (open: boolean) => void;
}

/**
 * Something a deployment may or may not have, that a sidebar entry can
 * require (`requiresGate` in `navigation.tsx`). The probe draws nothing: it
 * finds out, and reports. An entry is shown only while its gate is reported
 * open; a gate nobody contributes is never open.
 */
export interface NavGateSlotItem {
    /** The name entries require the gate by. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    Probe: React.ComponentType<NavGateProbeProps>;
}

/**
 * An action of a page: a button in the page's action bar and the dialog it
 * opens. `B` and `D` are what the page passes to each besides. The page holds
 * which dialog is open and mounts it only while it is: closing a dialog
 * discards what was entered in it.
 */
export interface ActionSlotItem<B, D> {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    Button: React.ComponentType<B & { onOpen: () => void }>;
    Dialog: React.ComponentType<D & { onClose: () => void }>;
}

/** What Mission Control passes to a contributed section of its Governance tab. */
export interface GovernanceSectionProps {
    /**
     * The governance posture as the Control Plane answered it, for the period
     * and the group shown. A section reads its own part and draws nothing
     * when that part is absent.
     */
    posture: unknown;
    /** Call after changing what the posture reports: the page reads it again. */
    onChanged: () => Promise<void>;
}

/** The slots, and what an item of each is. */
/**
 * A full-page view opened from a page's header: a button, and the view that
 * replaces the page until it is closed. The button decides whether it shows
 * (a permission, for instance).
 */
export interface PageViewSlotItem {
    id: string;
    edition?: EnterpriseFeature;
    Button: React.ComponentType<{ onOpen: () => void }>;
    View: React.ComponentType<{ onBack: () => void }>;
}

/**
 * Coverage panels of the Asset Register. The register calls `load` when it
 * opens and after a sync, a save or a delete, and waits for it; `null`, or a
 * failure, shows nothing. `Summary` is placed above the filters, `Detail`
 * below the register.
 */
export interface AssetCoverageSlotItem {
    id: string;
    edition?: EnterpriseFeature;
    load: (client: { fetchAssetCoverage: () => Promise<AssetCoverageResponse> }) => Promise<AssetCoverageData | null>;
    Summary: React.ComponentType<{ data: AssetCoverageData }>;
    Detail: React.ComponentType<{ data: AssetCoverageData }>;
}

/**
 * A principal of the console beside the tenant administrator: another kind of
 * administrator that can sign in, named by the `admin_type` its sessions
 * carry. The auth provider knows the tenant administrator and nobody else; a
 * session, a stored session or a sign-in answer of any other `admin_type` is
 * accepted only while an item here has that id, and is otherwise no session
 * at all. A contributed principal is not of a tenant: it has no tenant id,
 * no tenant feature flags, and the entries that need a tenant administrator
 * are not shown to it.
 *
 * Everything the console does differently for the principal is on the item,
 * so that without the item none of it exists.
 */
export interface PrincipalSlotItem {
    /** The principal's `admin_type`. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** Its option on the sign-in form, and the line under its name in the sidebar. */
    label: string;
    /** The tab the console opens on. */
    landingTab: string;
    /**
     * Whether this administrator holds every permission, whatever the session
     * lists. Asked only of an administrator signed in as this principal, as
     * the control plane answers it (sign-in and `/v1/admin/users/me` both
     * carry `permissions`).
     */
    holdsEveryPermission: (admin: Pick<components['schemas']['CurrentAdminResponse'], 'permissions'>) => boolean;
    /**
     * The choice of a tenant, for a page that shows one tenant's data
     * (`TenantScoped`): the principal is of no tenant, and the control plane
     * asks it to name one. `value` is the chosen tenant's id, '' for none.
     * Without it such a page says that it needs a tenant administrator.
     */
    TenantChoice?: React.ComponentType<{ value: string; onChange: (tenantId: string) => void }>;
    /** Sign in with a password. Answers as the tenant administrator's sign-in does. */
    signIn: (client: GatewayApiClient, credentials: { username: string; password: string }) => Promise<PasswordSignIn>;
    /** The sign-in form while its option is picked. */
    signInForm: {
        icon: SlotIcon;
        /** Under the options. */
        hint: string;
        /** The submit button. */
        submit: string;
        /** Above the username field. */
        Note: React.ComponentType;
    };
    /** The profile page (My Profile). */
    profile: {
        /** Ends "Changes here don't affect other admins". */
        scope: string;
        /** The Tenant row. */
        tenant: string;
        /** After the introduction of the Security tab. */
        SecurityNote: React.ComponentType;
        /** Above the change-password form. */
        PasswordNotice: React.ComponentType;
    };
}

/**
 * A page of the console that pages of the Community console link to when
 * it is there (`links.ts`).
 */
export interface LinkSlotItem {
    /** The link answered: one of `CONTRIBUTED_LINKS`. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** The tab id the link opens: one a screen of the same contribution answers. */
    tab: string;
}

/**
 * What installs the catalog entries of one runtime (the MCP catalog's import
 * wizard). The console itself imports an entry as configuration: a server
 * the administrator runs. An item here runs the server as well.
 */
export interface CatalogInstallSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** The catalog runtime whose entries the item installs. One item per runtime: the first is used. */
    runtime: string;
    /** What installing adds to the import request. */
    request: Partial<CatalogImportRequest>;
    /** How long the import call may take, in milliseconds: installing is one synchronous call. */
    timeoutMs: number;
    /**
     * Mounted while the wizard is open on such an entry. It shows `children`
     * (the wizard) where this installation can install the entry, and
     * otherwise says why it cannot, with a way to close.
     */
    Gate: React.ComponentType<{ entry: MCPCatalogEntry; onClose: () => void; children: React.ReactNode }>;
}

/**
 * What the sign-in of a console that serves more than one tenant adds to the
 * sign-in form. The console's own form signs a tenant administrator in with
 * a user name and a password and asks for no tenant; with an item here it
 * also asks which tenant. The first item is the one used.
 */
export interface SignInSlotItem {
    /** Unique within the slot. */
    id: string;
    /** Enterprise feature, or absent for the Community Edition. */
    edition?: EnterpriseFeature;
    /** The tenant a tenant administrator signs in to, asked on the console's own form. */
    tenant: {
        /** What the field holds when the form opens, and again after the administrator type is switched. */
        initial: string;
        /** The field, above the user name. The form holds its value and signs in with it, trimmed. */
        Field: React.ComponentType<{ value: string; onChange: (value: string) => void; disabled: boolean }>;
    };
    /** The line under the version, at the foot of the console's own form. */
    footer: string;
}

export interface SlotItems {
    /** Full-page views opened from the header of the Asset Register. */
    'asset-register.views': PageViewSlotItem;
    /** Coverage panels of the Asset Register. */
    'asset-register.coverage': AssetCoverageSlotItem;
    /** Gates that sidebar entries can require. */
    'app.nav-gates': NavGateSlotItem;
    /** Principals of the console beside the tenant administrator. */
    'app.principals': PrincipalSlotItem;
    /** Pages that Community pages link to when they are there. */
    'app.links': LinkSlotItem;
    /** What a console of more than one tenant adds to the sign-in. */
    'app.sign-in': SignInSlotItem;
    /** Banners above the page content, for the signed-in administrator. */
    'app.banners': SlotItem<{ user: ReturnType<typeof useAuth>['user'] }>;
    /**
     * Why a feature is off for the tenant, on the page shown in place of its
     * screen. The first item is asked; it shows `fallback` (the page's own
     * words) for as long as it does not know.
     */
    'feature-disabled.reason': SlotItem<{ flag: string; featureName: string; fallback: React.ReactNode }>;
    /** Actions of the End Users list, between Create User and Export. */
    'end-users.actions': ActionSlotItem<unknown, unknown>;
    /** Tabs of the Routing Groups page. */
    'llm-routing.tabs': TabSlotItem<void>;
    /** Import sources of the LLM Models page. */
    'llm-models.import': ModelImportSlotItem;
    /** Tabs of Mission Control. A tab is given the hours of the period shown. */
    'mission-control.tabs': TabSlotItem<{ hours: number }>;
    /** Sections of Mission Control's Governance tab, above Compliance Coverage. */
    'mission-control.governance-sections': SlotItem<GovernanceSectionProps>;
    /** Tiles among the performance tiles of Mission Control's Reliability tab. */
    'mission-control.performance-tiles': SlotItem<{ hours: number }>;
    /** Actions on a system alert in the Mission Control alerts drawer. */
    'mission-control.alert-actions': AlertActionSlotItem;
    /** What installs catalog entries of a runtime, in the MCP catalog's import wizard. */
    'mcp-catalog-import.steps': CatalogInstallSlotItem;
    /** Import sources of the MCP server form. */
    'mcp-server-form.import': ServerImportSlotItem;
    /**
     * Actions on the members of a Resource Group, before Add Members. The
     * button is told how many members the group has.
     */
    'resource-group.member-actions': ActionSlotItem<
        { group: ResourceGroup; memberCount: number },
        { group: ResourceGroup }
    >;
    /** Columns of a Resource Group's models table. */
    'resource-group.model-columns': ModelColumnSlotItem;
    /** Banners under the header of the Resource Group detail panel. */
    'resource-group.header': SlotItem<{ group: ResourceGroup }>;
    /** Top-level tabs of the Resource Group detail panel. */
    'resource-group.tabs': TabSlotItem<{ group: ResourceGroup; onUpdate: () => void }>;
    /** Sub-tabs under the Resources tab of the Resource Group detail panel. */
    'resource-group.resource-tabs': TabSlotItem<{ group: ResourceGroup }>;
    /** Tabs of the Settings page. */
    'settings.tabs': TabSlotItem<void>;
    /** Sub-tabs of the tenant settings page (Settings → Tenant): read-only, or saving themselves. */
    'tenant-settings.tabs': TenantSettingsTabSlotItem;
    /** Model sections of the Default Models sub-tab of the tenant settings page. */
    'tenant-settings.model-sections': TenantModelSectionSlotItem;
}

export type SlotName = keyof SlotItems;

/** Items per slot. A slot that is not listed is empty. */
export type SlotRegistry = { readonly [K in SlotName]?: readonly SlotItems[K][] };

const EMPTY_REGISTRY: SlotRegistry = {};
const NO_ITEMS: readonly never[] = [];

/** Read with `useSlot`; provided by `SlotProvider` (`SlotProvider.tsx`). */
export const SlotContext = createContext<SlotRegistry>(EMPTY_REGISTRY);

/** The items contributed to a slot, in order. Empty outside a `SlotProvider`. */
export function useSlot<K extends SlotName>(name: K): readonly SlotItems[K][] {
    return useContext(SlotContext)[name] ?? NO_ITEMS;
}

export interface MergedTabs<H, C> {
    /** The host's tabs and the contributed ones, in display order. */
    tabs: Array<H | C>;
    /** The contributed tabs that were placed, by id. */
    contributed: ReadonlyMap<string, C>;
}

/**
 * Place contributed tabs among a host page's own tabs.
 *
 * Each contributed tab goes directly after the tab its `after` names; several
 * naming the same tab keep the order they were contributed in. A contributed
 * tab whose `after` is absent or unknown goes last. A contributed tab that
 * reuses the id of a tab already present is dropped: the tab that was there
 * first keeps the id.
 */
export function mergeTabs<H extends { id: string }, C extends { id: string; after?: string }>(
    host: readonly H[],
    items: readonly C[],
): MergedTabs<H, C> {
    const tabs: Array<H | C> = [...host];
    const contributed = new Map<string, C>();
    // Where the next tab naming a given anchor goes: after the last one placed there.
    const lastPlacedAfter = new Map<string, string>();

    for (const item of items) {
        if (tabs.some((tab) => tab.id === item.id)) continue;
        const anchor = item.after === undefined ? undefined : (lastPlacedAfter.get(item.after) ?? item.after);
        const index = anchor === undefined ? -1 : tabs.findIndex((tab) => tab.id === anchor);
        if (index < 0) {
            tabs.push(item);
        } else {
            tabs.splice(index + 1, 0, item);
        }
        if (item.after !== undefined && index >= 0) lastPlacedAfter.set(item.after, item.id);
        contributed.set(item.id, item);
    }
    return { tabs, contributed };
}
