/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tool-approvals client module: which request each method makes.
 *
 * Approve and reject differ by one path segment and are opposite decisions,
 * so each method's verb, path and body are pinned one by one (the route
 * contract test only knows that both paths exist).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ToolApprovalsApi } from '../toolApprovals';

class Client extends ToolApprovalsApi {}

function client(response: unknown) {
    const c = Object.create(Client.prototype) as Client;
    const request = vi.fn().mockResolvedValue(response);
    (c as unknown as { request: typeof request }).request = request;
    return { c, request };
}

describe('ToolApprovalsApi', () => {
    beforeEach(() => vi.clearAllMocks());

    it('lists with the filter and paging the caller gave, and with none when given none', async () => {
        const page = { items: [], total: 0, page: 1, page_size: 100 };
        const { c, request } = client(page);
        await expect(c.listToolApprovals({ status_filter: 'expired', page: 2, page_size: 50 })).resolves.toBe(page);
        expect(request).toHaveBeenLastCalledWith('/v1/admin/approvals?status_filter=expired&page=2&page_size=50');
        await c.listToolApprovals();
        expect(request).toHaveBeenLastCalledWith('/v1/admin/approvals');
        await c.listToolApprovals({ status_filter: 'all', trace_root_id: 'root/1' });
        expect(request).toHaveBeenLastCalledWith('/v1/admin/approvals?status_filter=all&trace_root_id=root%2F1');
    });

    it('reads the pending count as a number', async () => {
        const { c, request } = client({ pending_count: 3 });
        await expect(c.getToolApprovalsPendingCount()).resolves.toBe(3);
        expect(request).toHaveBeenCalledWith('/v1/admin/approvals/pending-count');
    });

    it('reads one request, with its id escaped', async () => {
        const approval = { id: 'a/1' };
        const { c, request } = client({ approval });
        await expect(c.getToolApproval('a/1')).resolves.toBe(approval);
        expect(request).toHaveBeenCalledWith('/v1/admin/approvals/a%2F1');
    });

    it('approve posts to …/approve and reject to …/reject, each with its note', async () => {
        const { c, request } = client({ id: 'a1', status: 'x' });
        await c.approveToolApproval('a1', 'looks right');
        expect(request).toHaveBeenLastCalledWith('/v1/admin/approvals/a1/approve', {
            method: 'POST',
            body: JSON.stringify({ note: 'looks right' }),
        });
        await c.rejectToolApproval('a1', 'over the limit');
        expect(request).toHaveBeenLastCalledWith('/v1/admin/approvals/a1/reject', {
            method: 'POST',
            body: JSON.stringify({ note: 'over the limit' }),
        });
        await c.approveToolApproval('../x');
        expect(request).toHaveBeenLastCalledWith('/v1/admin/approvals/..%2Fx/approve', {
            method: 'POST',
            body: JSON.stringify({ note: null }),
        });
    });
});
