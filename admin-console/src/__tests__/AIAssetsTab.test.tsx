/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * AI Assets — AIAssetsTab test.
 *
 * Scope:
 *   • Rows render (display_name + kind badge visible)
 *   • Empty state when the list is empty
 *   • "Sync now" calls syncAIAssets then re-fetches the list
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import AIAssetsTab from '../components/ai-assets/AIAssetsTab';
import {
    buildAsset,
    mockCreateAIAsset,
    mockDeleteAIAsset,
    mockFetchAssetCoverage,
    mockListAIAssets,
    mockSyncAIAssets,
    mockUpdateAIAsset,
    stableToast,
} from '../test/aiAssets/aiAssetsTab';

const shared = vi.hoisted(() => () => import('../test/aiAssets/aiAssetsTab'));
vi.mock('../components/common/Toast', async () => (await shared()).toastModule());
vi.mock('../components/common/ConfirmationDialog', async () => (await shared()).confirmModule());
vi.mock('../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../hooks/useAuth', async () => (await shared()).useAuthModule());

describe('AIAssetsTab', () => {
    beforeEach(() => {
        mockListAIAssets.mockReset();
        mockSyncAIAssets.mockReset();
        mockCreateAIAsset.mockReset();
        mockUpdateAIAsset.mockReset();
        mockDeleteAIAsset.mockReset();
        mockFetchAssetCoverage.mockReset();
        // Default: empty coverage so the scorecard/shadow-AI strip stays inert
        // unless a test opts into richer data.
        mockFetchAssetCoverage.mockResolvedValue({
            coverage: { governed: 0, observed: 0, ungoverned: 0, total_seen: 0 },
            discovered_ungoverned: [],
        });
    });

    it('renders one row per asset with display name and kind badge', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [
                buildAsset({ id: 'a', display_name: 'GPT-4o', ref_id: 'gpt-4o' }),
                buildAsset({
                    id: 'b',
                    display_name: 'Slack MCP',
                    ref_id: 'slack',
                    asset_kind: 'mcp_server',
                }),
            ],
            total: 2,
        });
        render(<AIAssetsTab />);
        await waitFor(() => {
            expect(screen.getByText('GPT-4o')).toBeInTheDocument();
            expect(screen.getByText('Slack MCP')).toBeInTheDocument();
        });
        // Kind badges resolved through ASSET_KIND_LABEL. The label also
        // appears in the filter <select>, so assert at-least-one match.
        expect(screen.getAllByText('LLM Model').length).toBeGreaterThan(0);
        expect(screen.getAllByText('MCP Server').length).toBeGreaterThan(0);
    });

    it('marks an observed (imported) asset with an Observed badge', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [
                buildAsset({ id: 'g', display_name: 'GPT-5.5', ref_id: 'gpt-5.5' }),
                buildAsset({
                    id: 'o',
                    display_name: 'Claude (imported)',
                    ref_id: 'claude-haiku-4-5',
                    observed: true,
                }),
            ],
            total: 2,
        });
        render(<AIAssetsTab />);
        await waitFor(() => {
            expect(screen.getByText('Claude (imported)')).toBeInTheDocument();
        });
        // Exactly one row carries the Observed badge (the imported model).
        const badges = screen.getAllByTitle(/not governed inline/i);
        expect(badges).toHaveLength(1);
    });

    it('shows the empty state when the list is empty', async () => {
        mockListAIAssets.mockResolvedValue({ assets: [], total: 0 });
        render(<AIAssetsTab />);
        await waitFor(() => {
            expect(screen.getByText(/No AI assets yet/i)).toBeInTheDocument();
        });
    });

    it('renders the per-asset icon for all three icon_url shapes', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [
                // (a) Normal URL → <img>
                buildAsset({
                    id: 'url',
                    display_name: 'OpenAI',
                    ref_id: 'openai',
                    asset_kind: 'llm_provider',
                    icon_url: 'https://cdn.example.com/openai.svg',
                }),
                // (b) lucide:{name} sentinel (skill) → mapped Lucide component (SVG)
                buildAsset({
                    id: 'skill',
                    display_name: 'Summariser',
                    ref_id: 'summarise',
                    asset_kind: 'skill',
                    icon_url: 'lucide:sparkles',
                }),
                // (c) null → neutral letter fallback
                buildAsset({
                    id: 'none',
                    display_name: 'Routing X',
                    ref_id: 'routing-x',
                    asset_kind: 'routing_group',
                    icon_url: null,
                }),
            ],
            total: 3,
        });
        const { container } = render(<AIAssetsTab />);
        await waitFor(() => {
            expect(screen.getByText('OpenAI')).toBeInTheDocument();
        });

        // (a) URL → an <img> pointing at the icon_url.
        const img = container.querySelector(
            'img[src="https://cdn.example.com/openai.svg"]',
        );
        expect(img).not.toBeNull();
        expect(img).toHaveAttribute('alt', 'OpenAI');

        // (b) lucide sentinel → an inline SVG (the lucide-react component).
        // The skill row's name is unique; its cell contains an <svg>.
        const skillRow = screen.getByText('Summariser').closest('tr');
        expect(skillRow?.querySelector('svg')).not.toBeNull();

        // (c) null → letter fallback box with the first letter of the name.
        const noneRow = screen.getByText('Routing X').closest('tr');
        // The Asset cell's fallback span carries aria-label === display name
        // and shows the leading letter.
        const fallback = noneRow?.querySelector('span[aria-label="Routing X"]');
        expect(fallback).not.toBeNull();
        expect(fallback?.textContent).toBe('R');
    });

    it('prefixes a leading-slash icon path with the gateway base URL (MCP server)', async () => {
        // The registry ships control-plane blob paths like
        // /v1/admin/mcp-servers/{id}/icon for MCP servers (and blob-only
        // providers). AssetIcon must resolve them via the gateway base so
        // the <img> loads from the control plane, not the admin UI origin.
        mockListAIAssets.mockResolvedValue({
            assets: [
                buildAsset({
                    id: 'mcp1',
                    display_name: 'Files MCP',
                    ref_id: 'files-mcp',
                    asset_kind: 'mcp_server',
                    icon_url: '/v1/admin/mcp-servers/mcp1/icon',
                }),
            ],
            total: 1,
        });
        const { container } = render(<AIAssetsTab />);
        await waitFor(() => {
            expect(screen.getByText('Files MCP')).toBeInTheDocument();
        });
        // resolveAssetUrl (mocked) prefixes leading-slash paths with the base.
        const img = container.querySelector(
            'img[src="http://cp/v1/admin/mcp-servers/mcp1/icon"]',
        );
        expect(img).not.toBeNull();
        expect(img).toHaveAttribute('alt', 'Files MCP');
    });

    it('falls back to the letter box when an icon URL fails to load', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [
                buildAsset({
                    id: 'broken',
                    display_name: 'Broken Brand',
                    ref_id: 'broken',
                    asset_kind: 'llm_provider',
                    icon_url: 'https://cdn.example.com/broken.svg',
                }),
            ],
            total: 1,
        });
        const { container } = render(<AIAssetsTab />);
        await waitFor(() => screen.getByText('Broken Brand'));

        const img = container.querySelector(
            'img[src="https://cdn.example.com/broken.svg"]',
        );
        expect(img).not.toBeNull();
        fireEvent.error(img as HTMLImageElement);

        await waitFor(() => {
            const fallback = container.querySelector(
                'span[aria-label="Broken Brand"]',
            );
            expect(fallback).not.toBeNull();
            expect(fallback?.textContent).toBe('B');
        });
    });

    it('calls syncAIAssets and re-fetches when Sync now is clicked', async () => {
        mockListAIAssets.mockResolvedValue({ assets: [], total: 0 });
        mockSyncAIAssets.mockResolvedValue({
            created: 3,
            updated: 1,
            orphaned: 0,
            total: 4,
        });
        render(<AIAssetsTab />);
        await waitFor(() => screen.getByText(/No AI assets yet/i));

        // Initial fetch on mount.
        expect(mockListAIAssets).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole('button', { name: /Sync now/i }));

        await waitFor(() => {
            expect(mockSyncAIAssets).toHaveBeenCalledTimes(1);
        });
        // Sync triggers a refresh (a second list call).
        await waitFor(() => {
            expect(mockListAIAssets).toHaveBeenCalledTimes(2);
        });
        expect(stableToast.success).toHaveBeenCalled();
    });
});

// ─────────────────────────────────────────────────────────────────────────
// The register's edge view + GPAI / operated-by chips
// ─────────────────────────────────────────────────────────────────────────

describe('the regulated-role and conformity fields of the register', () => {
    beforeEach(() => {
        mockListAIAssets.mockReset();
        mockFetchAssetCoverage.mockResolvedValue(null);
    });

    it('a component asset renders its DERIVED tier with provenance', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [buildAsset({
                risk_tier: null,
                derived_risk_tier: 'high',
                owning_systems: [
                    { id: 'sys-1', name: 'Revenue Copilot', eu_ai_act_risk_tier: 'high' },
                    { id: 'sys-2', name: 'Triage', eu_ai_act_risk_tier: 'limited' },
                ],
            })],
            total: 1,
        });
        render(<AIAssetsTab />);
        const cell = await screen.findByTestId('derived-risk-asset-1');
        expect(cell).toHaveTextContent('High');
        expect(cell).toHaveTextContent('via 2 systems');
        expect(cell).toHaveAttribute('title', expect.stringContaining('Revenue Copilot'));
    });

    it('an ai_system row shows its own declared tier, not a derivation', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [buildAsset({ asset_kind: 'ai_system', ref_id: 'group-1', risk_tier: 'high' })],
            total: 1,
        });
        render(<AIAssetsTab />);
        expect(await screen.findByText('High')).toBeInTheDocument();
        expect(screen.queryByTestId('derived-risk-asset-1')).toBeNull();
    });

    it('GPAI and third-party chips render with their explainer titles', async () => {
        mockListAIAssets.mockResolvedValue({
            assets: [buildAsset({
                gpai_classification: 'gpai_systemic_risk',
                operated_by: 'third_party',
            })],
            total: 1,
        });
        render(<AIAssetsTab />);
        const gpai = await screen.findByTestId('gpai-chip-asset-1');
        expect(gpai).toHaveTextContent('GPAI · SR');
        expect(gpai.title).toMatch(/Art 55/);
        const third = screen.getByTestId('thirdparty-chip-asset-1');
        expect(third.title).toMatch(/supply-chain/);
    });

    it('the page is titled Asset Register (the ITAM tier, not the accountability register)', async () => {
        mockListAIAssets.mockResolvedValue({ assets: [], total: 0 });
        render(<AIAssetsTab />);
        expect(await screen.findByText('Asset Register')).toBeInTheDocument();
    });
});
