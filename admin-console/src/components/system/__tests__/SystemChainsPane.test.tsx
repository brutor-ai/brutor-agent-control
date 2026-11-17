/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemChainsPane — audit-chain verification.
 *
 * The pane's whole job is to report an integrity verdict honestly, so the
 * cases that matter are the ones where it must NOT say "fine":
 *
 *   - an unsigned chain reads as `unattested`, never as verified
 *   - a failed check reports that no check ran, and clears any prior verdict
 *   - `forged` (valid signature, untrusted key) is surfaced distinctly from
 *     `broken`, because it is deliberate rather than possibly corruption
 *   - a truncated scan says so, so a partial pass is not read as a whole-chain pass
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockListAuditChains = vi.fn();
const mockVerifyAuditChain = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listAuditChains: mockListAuditChains,
            verifyAuditChain: mockVerifyAuditChain,
        },
    }),
}));

import SystemChainsPane from '../SystemChainsPane';

const report = (over: Record<string, unknown> = {}) => ({
    chain_id: 'chain-a',
    verdict: 'unattested',
    rows_checked: 12,
    seq_start: 0,
    seq_end: 11,
    first_bad_seq: null,
    gaps: [],
    checkpoints_checked: 0,
    checkpoints_valid: 0,
    checkpoints_untrusted: 0,
    truncated: false,
    ...over,
});

beforeEach(() => {
    vi.clearAllMocks();
    mockListAuditChains.mockResolvedValue({ chains: ['chain-a'] });
});

describe('SystemChainsPane', () => {
    it('lists chains on mount', async () => {
        render(<SystemChainsPane />);
        expect(await screen.findByTestId('chain-chain-a')).toBeInTheDocument();
        expect(mockListAuditChains).toHaveBeenCalledTimes(1);
    });

    it('reports an unsigned chain as unattested, not verified', async () => {
        // The honest verdict for a deployment with no signing key. Calling
        // this "verified" would assert integrity the evidence cannot support.
        mockVerifyAuditChain.mockResolvedValue(report());
        render(<SystemChainsPane />);

        fireEvent.click(await screen.findByRole('button', { name: /verify/i }));

        expect(await screen.findByText('Unattested')).toBeInTheDocument();
        expect(screen.queryByText('Verified')).not.toBeInTheDocument();
    });

    it('distinguishes a forged attestation from a tampered row', async () => {
        // A valid signature from a key we never trusted is deliberate, and
        // must not be reported with the same words as a hash mismatch.
        mockVerifyAuditChain.mockResolvedValue(
            report({ verdict: 'forged', checkpoints_checked: 1, checkpoints_untrusted: 1 }),
        );
        render(<SystemChainsPane />);

        fireEvent.click(await screen.findByRole('button', { name: /verify/i }));

        expect(await screen.findByText('Forged attestation')).toBeInTheDocument();
        expect(screen.getByText('untrusted keys')).toBeInTheDocument();
        expect(screen.queryByText('Tampered')).not.toBeInTheDocument();
    });

    it('names the first bad row when a chain is broken', async () => {
        mockVerifyAuditChain.mockResolvedValue(
            report({ verdict: 'broken', first_bad_seq: 7 }),
        );
        render(<SystemChainsPane />);

        fireEvent.click(await screen.findByRole('button', { name: /verify/i }));

        expect(await screen.findByText('Tampered')).toBeInTheDocument();
        expect(screen.getByText('#7')).toBeInTheDocument();
    });

    it('a failed check reports that no check ran, rather than staying silent', async () => {
        mockVerifyAuditChain.mockRejectedValue(new Error('proxy unreachable'));
        render(<SystemChainsPane />);

        fireEvent.click(await screen.findByRole('button', { name: /verify/i }));

        expect(
            await screen.findByText(/Verification could not run: proxy unreachable/),
        ).toBeInTheDocument();
    });

    it('clears a previous verdict when a later check fails', async () => {
        // Leaving the old verdict on screen would let a stale "verified"
        // stand in for a check that did not happen.
        mockVerifyAuditChain.mockResolvedValueOnce(report({ verdict: 'verified' }));
        render(<SystemChainsPane />);

        const btn = await screen.findByRole('button', { name: /verify/i });
        fireEvent.click(btn);
        expect(await screen.findByText('Verified')).toBeInTheDocument();

        mockVerifyAuditChain.mockRejectedValueOnce(new Error('gone'));
        fireEvent.click(btn);

        await waitFor(() =>
            expect(screen.queryByText('Verified')).not.toBeInTheDocument(),
        );
        expect(screen.getByText(/Verification could not run/)).toBeInTheDocument();
    });

    it('says when a scan was truncated', async () => {
        mockVerifyAuditChain.mockResolvedValue(report({ truncated: true }));
        render(<SystemChainsPane />);

        fireEvent.click(await screen.findByRole('button', { name: /verify/i }));

        expect(
            await screen.findByText(/describes the range examined, not the whole chain/),
        ).toBeInTheDocument();
    });

    it('surfaces a listing failure without implying anything about integrity', async () => {
        mockListAuditChains.mockRejectedValue(new Error('boom'));
        render(<SystemChainsPane />);

        expect(await screen.findByText(/Could not list chains: boom/)).toBeInTheDocument();
        expect(
            screen.getByText(/says nothing about the integrity of the records/),
        ).toBeInTheDocument();
    });
});
