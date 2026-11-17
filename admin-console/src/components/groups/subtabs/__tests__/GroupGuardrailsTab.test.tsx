/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The group's effective guardrails, read-only. An operator reads this to answer
 * "what protects this group?", so the row must not understate a config: a check
 * active only through a per-surface override still has to appear, and inherited
 * configs must name their source. Filtering an empty view must not claim a search
 * failed when nothing was searched for.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const gateway = { fetchEffectiveGuardrails: vi.fn() };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import GroupGuardrailsTab from '../GroupGuardrailsTab';

const GROUP = { id: 'rg-1', name: 'eng' } as ResourceGroup;

const config = (id: string, extra: object = {}) => ({
    id,
    name: id,
    description: null,
    is_inherited: false,
    is_default: false,
    banned_word_count: 0,
    banned_pattern_count: 0,
    provider_count: 0,
    surfaces: {},
    fail_open: false,
    max_check_timeout_ms: 500,
    priority: 0,
    ...extra,
});

const DIRECT = config('strict', {
    description: 'Blocks everything',
    pii_enabled: true,
    prompt_injection_enabled: true,
    jailbreak_enabled: true,
    toxic_content_enabled: true,
    secrets_enabled: true,
    banned_word_count: 1,
    banned_pattern_count: 2,
    provider_count: 1,
    per_surface_counts: { pii: 2, secrets: 1 },
    surfaces: { chat_input: true },
    priority: 3,
    is_default: true,
});
const HEIR = config('from-org', {
    is_inherited: true,
    source_group_name: 'Org',
    fail_open: true,
    banned_word_count: 0,
    banned_pattern_count: 0,
    provider_count: 2,
    per_surface_counts: { banned_words: 1, banned_patterns: 2 },
});

function payload(direct: object[], inherited: object[]) {
    return {
        effective_configs: [...direct, ...inherited],
        direct_count: direct.length,
        inherited_count: inherited.length,
        effective_count: direct.length + inherited.length,
    };
}

async function open() {
    render(<GroupGuardrailsTab group={GROUP} />);
    await screen.findByRole('button', { name: /^All/ });
    expect(gateway.fetchEffectiveGuardrails).toHaveBeenCalledWith('rg-1');
}
const rowOf = (name: string) => within(screen.getByText(name).closest('tr') as HTMLElement);

describe('GroupGuardrailsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gateway.fetchEffectiveGuardrails.mockReset();
        gateway.fetchEffectiveGuardrails.mockResolvedValue(payload([DIRECT], [HEIR]));
    });

    afterEach(cleanup);

    it('lists every active check, flagging per-surface overrides', async () => {
        await open();
        const r = rowOf('strict');
        for (const label of ['PII (+2 surfaces)', 'Injection', 'Jailbreak', 'Toxic', 'Secrets (+1 surface)', '1 word', '2 patterns', '1 provider']) {
            expect(r.getByText(label)).toBeInTheDocument();
        }
        expect(r.getByText('Direct')).toBeInTheDocument();
        expect(r.getByText('Default')).toBeInTheDocument();
        expect(r.getByText('Fail-closed')).toBeInTheDocument();
        expect(r.getByText('500ms')).toBeInTheDocument();
        expect(r.getByText('P3')).toBeInTheDocument();
        expect(r.getByText('Blocks everything')).toBeInTheDocument();
    });

    it('still shows lists active only through overrides, and names the source', async () => {
        await open();
        const r = rowOf('from-org');
        expect(r.getByText('words (+1 surface)')).toBeInTheDocument();
        expect(r.getByText('patterns (+2 surfaces)')).toBeInTheDocument();
        expect(r.getByText('2 providers')).toBeInTheDocument();
        expect(r.getByText('Inherited')).toBeInTheDocument();
        expect(r.getByText('from Org')).toBeInTheDocument();
        expect(r.getByText('Fail-open')).toBeInTheDocument();
    });

    it('splits by source, and an empty view says so without claiming a failed search', async () => {
        gateway.fetchEffectiveGuardrails.mockResolvedValue(payload([], [HEIR]));
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Direct (0)' }));
        expect(screen.queryByText('from-org')).toBeNull();
        expect(screen.getByText('This group has no direct guardrails')).toBeInTheDocument();
        expect(screen.queryByText(/No guardrails match/)).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Inherited (1)' }));
        expect(screen.getByText('from-org')).toBeInTheDocument();
    });

    it('offers search only when the list is long enough, matching name and description', async () => {
        await open();
        expect(screen.queryByPlaceholderText('Search...')).toBeNull();
        cleanup();

        gateway.fetchEffectiveGuardrails.mockResolvedValue(payload([DIRECT, config('b'), config('c')], [HEIR]));
        await open();
        const search = screen.getByPlaceholderText('Search...');
        fireEvent.change(search, { target: { value: 'BLOCKS' } });
        expect(screen.getByText('strict')).toBeInTheDocument();
        expect(screen.queryByText('b')).toBeNull();

        fireEvent.change(search, { target: { value: 'zzz' } });
        expect(screen.getByText('No guardrails match "zzz"')).toBeInTheDocument();
    });

    it('says when none apply, and shows a load failure', async () => {
        gateway.fetchEffectiveGuardrails.mockResolvedValue(payload([], []));
        await open();
        expect(screen.getByText('No Guardrails Apply')).toBeInTheDocument();
        cleanup();

        vi.spyOn(console, 'error').mockImplementation(() => {});
        gateway.fetchEffectiveGuardrails.mockRejectedValue(new Error('cp down'));
        render(<GroupGuardrailsTab group={GROUP} />);
        expect(await screen.findByText('cp down')).toBeInTheDocument();
        vi.restoreAllMocks();
    });
});
