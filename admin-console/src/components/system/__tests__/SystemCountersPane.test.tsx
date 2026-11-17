/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemCountersPane.
 *
 * Coverage matches the structured-output contract for this slice:
 *
 *   1. Renders the table with one row per fetched counter entry,
 *      including the percent-bar element and the cooldown badge
 *      where applicable.
 *   2. Filters narrow the row set (group, kind, only-cooling-down).
 *   3. Action buttons are gated on `system:operate` — disabled for
 *      auditors, enabled for operators.
 *   4. Confirm modal opens on click and BLOCKS the action until the
 *      operator clicks Confirm. Cancel never fires the API.
 *   5. Confirm-then-fire calls the matching gatewayClient method
 *      with the expected payload shape.
 *   6. Detail drawer opens on row click and shows the disclaimer
 *      about authoritative state.
 *
 * `useGateway` and `useAuth` are mocked so the tests run hermetic —
 * no AuthProvider / fetch wiring needed. The mocks match how the
 * real hooks are consumed inside the pane.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import type {
    CounterEntry,
    CounterListResponse,
} from '../../../lib/systemOpsTypes';

// ─── Mocks ───────────────────────────────────────────────────────

const mockListSystemCounters = vi.fn();
const mockResetSystemCounter = vi.fn();
const mockReleaseSystemCooldown = vi.fn();
const mockHasPermission = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listSystemCounters: mockListSystemCounters,
            resetSystemCounter: mockResetSystemCounter,
            releaseSystemCooldown: mockReleaseSystemCooldown,
        },
    }),
}));

vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({
        hasPermission: mockHasPermission,
    }),
}));

// Import AFTER the mocks above so the pane picks up the doubles.
import SystemCountersPane from '../SystemCountersPane';

// ─── Fixtures ────────────────────────────────────────────────────

const baseEntry = (overrides: Partial<CounterEntry> = {}): CounterEntry => ({
    tenant_id: 'tnt_a',
    group_id: 'grp_red',
    subject_id: 'gpt-4o',
    subject_label: 'gpt-4o',
    kind: 'llm_request_rate',
    window: '60s',
    used: 42,
    limit: 100,
    cooldown_active: false,
    cooldown_until: null,
    last_updated_at: '2026-06-07T12:00:00Z',
    ...overrides,
});

const fixture = (entries: CounterEntry[]): CounterListResponse => ({
    tenant_id: 'tnt_a',
    generated_at: '2026-06-07T12:00:00Z',
    counters: entries,
});

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

// ─── Tests ───────────────────────────────────────────────────────

describe('SystemCountersPane', () => {
    beforeEach(() => {
        mockListSystemCounters.mockReset();
        mockResetSystemCounter.mockReset();
        mockReleaseSystemCooldown.mockReset();
        mockHasPermission.mockReset();
        // Deep-link URL state syncs to window.location via the
        // useUrlFilterSync hook. jsdom shares window between tests;
        // without an explicit reset, an opened drawer or pinned
        // scope from one test leaks into the next and hydrates the
        // pane with unexpected filters.
        window.history.replaceState(null, '', '/');
    });

    describe('table rendering', () => {
        it('renders one row per counter entry with the percent bar', async () => {
            grant(['system:read', 'system:operate']);
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({ subject_id: 'gpt-4o' }),
                    baseEntry({
                        subject_id: 'claude-opus-4',
                        subject_label: 'claude-opus-4',
                        used: 95,
                        limit: 100,
                    }),
                ]),
            );

            render(<SystemCountersPane />);

            // The pane mounts loading; wait for the first poll to land.
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            expect(
                screen.getByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            ).toBeDefined();
            expect(
                screen.getByTestId(
                    'system-counters-row-llm_request_rate-claude-opus-4',
                ),
            ).toBeDefined();

            // Both rows should expose a percent bar element (limit > 0).
            expect(
                screen.getByTestId(
                    'system-counters-bar-llm_request_rate-gpt-4o',
                ),
            ).toBeDefined();
            expect(
                screen.getByTestId(
                    'system-counters-bar-llm_request_rate-claude-opus-4',
                ),
            ).toBeDefined();
        });

        it('renders the cooldown badge when cooldown_active is true', async () => {
            grant(['system:read']);
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({
                        subject_id: 'gpt-4o',
                        cooldown_active: true,
                        cooldown_until: '2026-06-07T12:05:00Z',
                    }),
                ]),
            );

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId(
                        'system-counters-cooldown-badge-llm_request_rate-gpt-4o',
                    ),
                ).toBeDefined(),
            );
        });

        it('shows the approximation banner so operators see the caveat', async () => {
            grant(['system:read']);
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));
            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId(
                        'system-counters-approximation-banner',
                    ),
                ).toBeDefined(),
            );
        });
    });

    describe('filters', () => {
        it('only-cooling-down toggle filters out non-cooling rows', async () => {
            grant(['system:read']);
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({ subject_id: 'gpt-4o', cooldown_active: false }),
                    baseEntry({
                        subject_id: 'claude-opus-4',
                        cooldown_active: true,
                    }),
                ]),
            );

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            // Both rows visible initially.
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            ).not.toBeNull();
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-claude-opus-4',
                ),
            ).not.toBeNull();

            // Toggle the filter on.
            fireEvent.click(screen.getByTestId('system-counters-filter-cooling'));

            // Only the cooling row should remain.
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            ).toBeNull();
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-claude-opus-4',
                ),
            ).not.toBeNull();
        });

        it('kind filter narrows to the selected kind', async () => {
            grant(['system:read']);
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({
                        subject_id: 'gpt-4o',
                        kind: 'llm_request_rate',
                    }),
                    baseEntry({
                        subject_id: 'jira',
                        kind: 'mcp_tool_rate',
                    }),
                ]),
            );

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            fireEvent.change(
                screen.getByTestId('system-counters-filter-kind'),
                { target: { value: 'mcp_tool_rate' } },
            );

            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            ).toBeNull();
            expect(
                screen.queryByTestId('system-counters-row-mcp_tool_rate-jira'),
            ).not.toBeNull();
        });
    });

    describe('permission gating', () => {
        it('disables Reset / Release buttons when system:operate is absent', async () => {
            grant(['system:read']); // Auditor: read but not operate
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            const reset = screen.getByTestId(
                'system-counters-reset-llm_request_rate-gpt-4o',
            );
            const release = screen.getByTestId(
                'system-counters-release-llm_request_rate-gpt-4o',
            );
            expect(reset.hasAttribute('disabled')).toBe(true);
            expect(release.hasAttribute('disabled')).toBe(true);
        });

        it('enables Reset / Release buttons when system:operate is present', async () => {
            grant(['system:read', 'system:operate']);
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            const reset = screen.getByTestId(
                'system-counters-reset-llm_request_rate-gpt-4o',
            );
            expect(reset.hasAttribute('disabled')).toBe(false);
        });
    });

    describe('confirm modal', () => {
        it('opens the confirm modal on Reset click and does not call the API until Confirm', async () => {
            grant(['system:read', 'system:operate']);
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));
            mockResetSystemCounter.mockResolvedValue({ status: 'queued' });

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            fireEvent.click(
                screen.getByTestId(
                    'system-counters-reset-llm_request_rate-gpt-4o',
                ),
            );

            // Modal up, but API not yet called.
            expect(screen.getByTestId('system-counters-confirm')).toBeDefined();
            expect(mockResetSystemCounter).not.toHaveBeenCalled();

            // Cancel should also leave the API alone.
            fireEvent.click(
                screen.getByTestId('system-counters-confirm-cancel'),
            );
            expect(mockResetSystemCounter).not.toHaveBeenCalled();
            expect(
                screen.queryByTestId('system-counters-confirm'),
            ).toBeNull();
        });

        it('calls resetSystemCounter with the row-derived payload on Confirm', async () => {
            grant(['system:read', 'system:operate']);
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));
            mockResetSystemCounter.mockResolvedValue({ status: 'queued' });

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            fireEvent.click(
                screen.getByTestId(
                    'system-counters-reset-llm_request_rate-gpt-4o',
                ),
            );
            fireEvent.click(
                screen.getByTestId('system-counters-confirm-fire'),
            );

            await waitFor(() => {
                expect(mockResetSystemCounter).toHaveBeenCalledTimes(1);
            });
            const payload = mockResetSystemCounter.mock.calls[0][0];
            // Row had a group_id, so the scope must be 'group' and the id
            // is the group id — that's the contract the backend
            // CounterResetRequest model expects.
            expect(payload.scope).toBe('group');
            expect(payload.id).toBe('grp_red');
            expect(payload.limit_types).toEqual(['llm_request_rate']);
        });

        it('calls releaseSystemCooldown on a Release confirmation', async () => {
            grant(['system:read', 'system:operate']);
            mockListSystemCounters.mockResolvedValue(
                fixture([baseEntry({ cooldown_active: true })]),
            );
            mockReleaseSystemCooldown.mockResolvedValue({ status: 'queued' });

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            fireEvent.click(
                screen.getByTestId(
                    'system-counters-release-llm_request_rate-gpt-4o',
                ),
            );
            fireEvent.click(
                screen.getByTestId('system-counters-confirm-fire'),
            );

            await waitFor(() => {
                expect(mockReleaseSystemCooldown).toHaveBeenCalledTimes(1);
            });
            const payload = mockReleaseSystemCooldown.mock.calls[0][0];
            expect(payload.scope).toBe('group');
            expect(payload.id).toBe('grp_red');
        });
    });

    describe('detail drawer', () => {
        it('opens on row click and shows the approximation disclaimer', async () => {
            grant(['system:read']);
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(
                    screen.getByTestId('system-counters-pane'),
                ).toBeDefined(),
            );

            fireEvent.click(
                screen.getByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            );

            const drawer = screen.getByTestId('system-counters-drawer');
            expect(drawer).toBeDefined();
            // Drawer text must call out that authoritative state lives
            // in the gateway — operators need to know what's true.
            expect(drawer.textContent).toMatch(/gateway holds the authoritative state/i);
        });
    });

    // ────────────────────────────────────────────────────────────────
    // Deep-link plumbing
    //
    // Covers the bi-directional incident flow:
    //   * URL → pane state hydration (PagerDuty / Proxy Logs landing)
    //   * pane state → URL sync (operator's bookmark / share)
    //   * outbound links from the drawer to Audit + Proxy Logs
    // ────────────────────────────────────────────────────────────────
    describe('deep-link URL hydration', () => {
        it('hydrates scope + subject filters from URL on mount', async () => {
            grant(['system:read']);
            window.history.replaceState(
                null,
                '',
                '/?cnt-scope=model&cnt-subject=claude-opus-4',
            );
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({ subject_id: 'gpt-4o' }),
                    baseEntry({ subject_id: 'claude-opus-4', subject_label: 'claude-opus-4' }),
                ]),
            );

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(screen.getByTestId('system-counters-pane')).toBeDefined(),
            );

            // Only the matching subject's row should render — the
            // gpt-4o row is filtered out by the pinned subjectFilter.
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-claude-opus-4',
                ),
            ).not.toBeNull();
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            ).toBeNull();

            // The "arrived from a deep link" chip strip is visible and
            // shows the two pinned constraints.
            const chips = screen.getByTestId('system-counters-deeplink-chips');
            expect(chips.textContent).toContain('scope');
            expect(chips.textContent).toContain('model');
            expect(chips.textContent).toContain('subject');
            expect(chips.textContent).toContain('claude-opus-4');
        });

        it('passes scope param through to the API call when URL pins it', async () => {
            grant(['system:read']);
            window.history.replaceState(null, '', '/?cnt-scope=mcp_server');
            mockListSystemCounters.mockResolvedValue(fixture([]));

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(mockListSystemCounters).toHaveBeenCalled(),
            );

            // The API gets the scope filter so the backend can suppress
            // the off-scope family — the operator's empty state then
            // accurately reads "no rows for scope=mcp_server".
            expect(mockListSystemCounters).toHaveBeenCalledWith({
                scope: 'mcp_server',
            });
        });

        it('ignores an unknown scope value rather than passing it through', async () => {
            grant(['system:read']);
            window.history.replaceState(
                null,
                '',
                '/?cnt-scope=not-a-real-scope',
            );
            mockListSystemCounters.mockResolvedValue(fixture([baseEntry()]));

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(mockListSystemCounters).toHaveBeenCalled(),
            );

            // Invalid scope tokens must NOT be forwarded — the API would
            // 422 on them. Empty filter is the safe fallback.
            expect(mockListSystemCounters).toHaveBeenCalledWith({});
            // And the deep-link chip strip should not render for an
            // invalid hydration.
            expect(
                screen.queryByTestId('system-counters-deeplink-chips'),
            ).toBeNull();
        });

        it('auto-opens the drawer when ?cnt-drawer=open matches exactly one row', async () => {
            grant(['system:read']);
            window.history.replaceState(
                null,
                '',
                '/?cnt-subject=gpt-4o&cnt-drawer=open',
            );
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({ subject_id: 'gpt-4o' }),
                    baseEntry({ subject_id: 'claude-opus-4' }),
                ]),
            );

            render(<SystemCountersPane />);

            // Drawer should auto-open onto the single matching row.
            await waitFor(() =>
                expect(
                    screen.queryByTestId('system-counters-drawer'),
                ).not.toBeNull(),
            );
        });

        it('clears a chip filter on chip click', async () => {
            grant(['system:read']);
            window.history.replaceState(null, '', '/?cnt-subject=gpt-4o');
            mockListSystemCounters.mockResolvedValue(
                fixture([
                    baseEntry({ subject_id: 'gpt-4o' }),
                    baseEntry({ subject_id: 'claude-opus-4' }),
                ]),
            );

            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(screen.getByTestId('system-counters-pane')).toBeDefined(),
            );

            // Only the matching row is visible to start.
            expect(
                screen.queryByTestId(
                    'system-counters-row-llm_request_rate-claude-opus-4',
                ),
            ).toBeNull();

            fireEvent.click(
                screen.getByTestId('system-counters-chip-subject'),
            );

            // After clearing the chip both rows are visible again.
            await waitFor(() =>
                expect(
                    screen.queryByTestId(
                        'system-counters-row-llm_request_rate-claude-opus-4',
                    ),
                ).not.toBeNull(),
            );
        });
    });

    describe('drawer outbound links', () => {
        beforeEach(() => {
            grant(['system:read']);
        });

        it('renders View Audit history link with derived scope_kind', async () => {
            mockListSystemCounters.mockResolvedValue(
                fixture([baseEntry({ subject_id: 'gpt-4o' })]),
            );
            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(screen.getByTestId('system-counters-pane')).toBeDefined(),
            );
            fireEvent.click(
                screen.getByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            );

            const auditLink = screen.getByTestId<HTMLAnchorElement>(
                'system-counters-drawer-audit-link',
            );
            // LLM kinds derive to scope_kind=model so the Audit page
            // filter lands on rows for this specific LLM model.
            expect(auditLink.href).toContain('scope_kind=model');
            expect(auditLink.href).toContain('scope_id=gpt-4o');
        });

        it('renders View Proxy Logs link with resource_id + last_1h', async () => {
            mockListSystemCounters.mockResolvedValue(
                fixture([baseEntry({ subject_id: 'gpt-4o' })]),
            );
            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(screen.getByTestId('system-counters-pane')).toBeDefined(),
            );
            fireEvent.click(
                screen.getByTestId(
                    'system-counters-row-llm_request_rate-gpt-4o',
                ),
            );

            const logsLink = screen.getByTestId<HTMLAnchorElement>(
                'system-counters-drawer-proxy-logs-link',
            );
            expect(logsLink.href).toContain('resource_id=gpt-4o');
            expect(logsLink.href).toContain('time=last_1h');
        });

        it('omits outbound links when the row has no subject_id', async () => {
            mockListSystemCounters.mockResolvedValue(
                fixture([baseEntry({ subject_id: null, subject_label: null })]),
            );
            render(<SystemCountersPane />);
            await waitFor(() =>
                expect(screen.getByTestId('system-counters-pane')).toBeDefined(),
            );
            // The row test id is keyed on subject_id, so use a query
            // approach that finds the first table row instead.
            const rows = document.querySelectorAll('[data-testid^="system-counters-row-"]');
            expect(rows.length).toBeGreaterThan(0);
            fireEvent.click(rows[0]);

            // Drawer opens but the outbound links are absent —
            // there's no meaningful subject to link to.
            expect(
                screen.queryByTestId('system-counters-drawer'),
            ).not.toBeNull();
            expect(
                screen.queryByTestId('system-counters-drawer-audit-link'),
            ).toBeNull();
            expect(
                screen.queryByTestId('system-counters-drawer-proxy-logs-link'),
            ).toBeNull();
        });
    });
});
