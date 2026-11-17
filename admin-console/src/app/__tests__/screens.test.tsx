/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The screen registry (`app/screens.tsx`): which component answers which tab id.
 *
 * `screens.recorded.json` is the record of what the Community console
 * renders for each tab id its registry lists: the component, the tenant flag
 * it is behind and whether it is padded. The first block below holds the
 * registry to it, so a screen cannot change unnoticed. A screen is added,
 * removed or changed together with its row.
 *
 * Screens are checked by calling `render` and looking at the element it
 * returns. Nothing is mounted, so no screen fetches anything.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

import { describe, it, expect } from 'vitest';

import { composeAdmin } from '../extensions';
import { FALLBACK_SCREEN, renderScreen, resolveScreen } from '../screens';
import {
    COMMUNITY_ROWS,
    ctx,
    element,
    nameOf,
    navLeaves,
    navParents,
    recordedScreenCases,
    rendererOf,
    type NavNode,
} from '../../test/screensRecord';

// The Community console: the built-in registries, composed with no
// extension. The rows the Enterprise extension answers, and the pins that
// name its entries, are held beside the extension, with their own record.
const { screens: SCREENS, navSections: NAV_SECTIONS } = composeAdmin([]);

const render = rendererOf(SCREENS);

describe('the registry renders what is recorded', () => {
    it('the record has one row per tab id', () => {
        const ids = COMMUNITY_ROWS.map((r) => r.id);
        expect(new Set(ids).size).toBe(ids.length);
    });

    recordedScreenCases(SCREENS, COMMUNITY_ROWS);

    it('two screens are padded', () => {
        expect(COMMUNITY_ROWS.filter((r) => r.padded).map((r) => r.id)).toEqual([
            'monitoring-audit-logs',
            'monitoring-proxy-logs',
        ]);
    });

    it('every id the registry lists has a row, and every row is an id the registry lists', () => {
        // Resource Groups is held by the deep-link block below: what it renders depends on the id.
        const listed = SCREENS.flatMap((s) => s.ids ?? []).filter((id) => !['groups', 'groups-create'].includes(id));
        expect([...listed].sort()).toEqual(COMMUNITY_ROWS.map((r) => r.id).sort());
    });

    it('an id no screen claims renders the dashboard', () => {
        expect(resolveScreen(SCREENS, 'no-such-screen')).toBeUndefined();
        expect(nameOf(render('no-such-screen'))).toBe('MissionControlDashboard');
        // Only `inbox:reviews:` is a deep link into the Inbox.
        expect(nameOf(render('inbox:other'))).toBe('MissionControlDashboard');
        expect(nameOf(element(FALLBACK_SCREEN.render(ctx('x'))))).toBe('MissionControlDashboard');
    });
});

describe('deep links', () => {
    it('ai-systems:<id>:<section>:<run> opens the drilldown; the run is not part of the key', () => {
        const el = render('ai-systems:g1:composition:run-9');
        expect(nameOf(el)).toBe('AISystemsFleetTab');
        expect(el.key).toBe('ai-systems:g1:composition');
        expect(el.props).toMatchObject({ initialDrilldownId: 'g1', initialSection: 'composition', routeRunId: 'run-9' });

        const bare = render('ai-systems:g1');
        expect(bare.key).toBe('ai-systems:g1:');
        expect(bare.props).toMatchObject({ initialDrilldownId: 'g1', initialSection: undefined, routeRunId: null });

        const landing = render('ai-systems');
        expect(landing.key).toBeNull();
        expect(landing.props.initialDrilldownId).toBeUndefined();
    });

    it('a2a-identities:<agent>:<section> opens the identity on a section', () => {
        const el = render('a2a-identities:agent-7:implementation');
        expect(nameOf(el)).toBe('AgentIdentitiesResourceTab');
        expect(el.key).toBe('a2a-identities:agent-7:implementation');
        expect(el.props).toMatchObject({ initialAgentId: 'agent-7', initialSection: 'implementation' });
    });

    it('config:<tab>:<sub-tab> opens Settings on a sub-tab', () => {
        const el = render('config:tenant:evidence');
        expect(nameOf(el)).toBe('ConfigurationTab');
        expect(el.key).toBe('config:tenant:evidence');
        expect(el.props).toMatchObject({ initialTab: 'tenant', initialTenantSubTab: 'evidence' });
        expect(el.props.currentUser).toEqual({ username: 'admin' });
    });

    it('inbox:reviews:<check> opens the Inbox on a review queue', () => {
        const el = render('inbox:reviews:check-3');
        expect(nameOf(el)).toBe('AssuranceInboxPage');
        expect(el.key).toBe('inbox:reviews:check-3');
        expect(el.props.reviewCheckId).toBe('check-3');
        expect(render('inbox').props.reviewCheckId).toBeUndefined();
    });

    it('groups, groups:<id>:<tab>:<section> and groups-create go to Resource Groups', () => {
        const landing = render('groups');
        expect(nameOf(landing)).toBe('ResourceGroupsTab');
        expect(landing.props).toMatchObject({ selectedGroupId: undefined, showCreateModal: false });

        const detail = render('groups:u-1:general:ownership');
        expect(detail.props).toMatchObject({
            selectedGroupId: 'u-1',
            initialDetailTab: 'general',
            initialGeneralSection: 'ownership',
            showCreateModal: false,
        });

        expect(render('groups-create').props).toMatchObject({ selectedGroupId: undefined, showCreateModal: true });
    });

    it('Resource Groups navigates by setting a groups tab id', () => {
        const context = ctx('groups');
        const el = element(renderScreen(SCREENS, context));
        const navigate = el.props.onNavigateToGroup as (id: string | null) => void;
        navigate('u-2');
        navigate(null);
        expect(context.setActiveTab).toHaveBeenNthCalledWith(1, 'groups:u-2');
        expect(context.setActiveTab).toHaveBeenNthCalledWith(2, 'groups');
        expect(el.props.onGroupTreeChanged).toBe(context.refreshGroupTree);
    });

    it('llm-models:<id> opens LLM Models focused on a model, behind the models flag', () => {
        expect(render('llm-models:m-1').props.initialFocusModelId).toBe('m-1');
        expect(render('llm-models').props.initialFocusModelId).toBeNull();
        expect(nameOf(render('llm-models:m-1', () => false))).toBe('FeatureDisabled');
    });

    it('mission-control-metrics:<group> opens Analytics scoped to a group', () => {
        const el = render('mission-control-metrics:g-4');
        expect(nameOf(el)).toBe('MissionControl');
        expect(el.props.initialGroupId).toBe('g-4');
        expect(render('mission-control-metrics').props.initialGroupId).toBeUndefined();
    });

    it('the console shells are told the active tab', () => {
        expect(render('compliance-evidence').props.activeTab).toBe('compliance-evidence');
        expect(render('system-chains').props.activeTab).toBe('system-chains');
    });
});

describe('the registry is well formed', () => {
    it('screen names are unique', () => {
        const names = SCREENS.map((s) => s.name);
        expect(new Set(names).size).toBe(names.length);
    });

    it('no tab id is claimed by two screens', () => {
        const ids = SCREENS.flatMap((s) => s.ids ?? []);
        expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);
    });

    it('no deep-link prefix overlaps another, or an exact id', () => {
        const prefixes = SCREENS.flatMap((s) => s.prefixes ?? []);
        const ids = SCREENS.flatMap((s) => s.ids ?? []);
        for (const prefix of prefixes) {
            expect(prefix.endsWith(':'), `${prefix} ends with ":"`).toBe(true);
            expect(prefixes.filter((other) => other !== prefix && other.startsWith(prefix))).toEqual([]);
            expect(ids.filter((id) => id.startsWith(prefix))).toEqual([]);
        }
    });

    it('every screen claims something', () => {
        for (const screen of SCREENS) {
            expect({ screen: screen.name, claims: (screen.ids?.length ?? 0) + (screen.prefixes?.length ?? 0) > 0 }).toEqual({
                screen: screen.name,
                claims: true,
            });
        }
    });
});

describe('every sidebar entry you can open has a screen', () => {
    // A nav id with no screen falls through to the dashboard, so the click
    // appears to do nothing. Checked for every entry the sidebar navigates
    // to, at every depth, by resolving it.
    //
    // An entry with children is not a click target: the sidebar expands it
    // (or, collapsed, opens its first child), so it has no screen.
    const items = NAV_SECTIONS.flatMap((s) => s.items) as NavNode[];
    const leafIds = navLeaves(items);

    it('the sidebar has entries', () => {
        expect(leafIds.length).toBeGreaterThan(50);
    });

    it.each(leafIds)('%s resolves to a screen', (id) => {
        expect(resolveScreen(SCREENS, id), `${id} has no screen: it would show the dashboard`).toBeDefined();
    });

    it('the sidebar has parents', () => {
        expect(navParents(items)).toEqual(expect.arrayContaining(['resources', 'compliance', 'async-jobs', 'monitoring', 'system']));
    });

    it.each(navParents(items))('%s, a parent, has no screen: it only expands', (id) => {
        expect(resolveScreen(SCREENS, id)).toBeUndefined();
        expect(nameOf(render(id))).toBe('MissionControlDashboard');
    });

    it.each(['eu-ai-act', 'iso42001', 'agent-identities'])('%s is not a tab id', (id) => {
        expect(resolveScreen(SCREENS, id)).toBeUndefined();
    });
});

describe('the app shell routes through the registry', () => {
    // The registry is the one place a tab id is mapped to a screen. A `case`
    // in `App.tsx` would be a screen the registry, and anything that extends
    // it, cannot see.
    it('App.tsx renders the active tab with renderScreen and has no case labels of its own', () => {
        const app = readFileSync(path.resolve(__dirname, '../../App.tsx'), 'utf8');
        // The list is the built-in screens composed with the extensions given
        // to <App>; with none it is SCREENS.
        expect(app).toMatch(/renderScreen\(composition\.screens, \{ activeTab, /);
        expect(app).toMatch(/const composition = React\.useMemo\(\(\) => composeAdmin\(extensions\), \[extensions\]\);/);
        expect(app).not.toMatch(/^\s*case\s+['"`]/m);
    });
});
