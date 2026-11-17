/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The exported artifact must be exactly the document on screen — this is
 * the canonical, signature-covered form, so the serialisation and the
 * filename are load-bearing, not cosmetic.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { downloadJson } from '../downloadJson';
import { installBlobDownloadSpies, type CapturedDownload } from './helpers/blobDownload';

let captured: CapturedDownload;

beforeEach(() => {
    captured = installBlobDownloadSpies();
});

afterEach(() => {
    vi.restoreAllMocks();
});

const text = () => captured.blobs[0].text();

describe('downloadJson', () => {
    it('names the file and clicks the link exactly once', () => {
        downloadJson('contract-v3.json', { ok: true });
        expect(captured.anchors).toHaveLength(1);
        expect(captured.anchors[0].download).toBe('contract-v3.json');
        expect(captured.anchors[0].href).toBe('blob:mock/1');
        expect(captured.clicks).toEqual([1]);
    });

    it('serialises as pretty-printed application/json', async () => {
        downloadJson('doc.json', { a: 1, b: { c: 2 } });
        expect(captured.blobs[0].type).toBe('application/json');
        expect(await text()).toBe('{\n  "a": 1,\n  "b": {\n    "c": 2\n  }\n}');
    });

    it('preserves key order and nested arrays verbatim', async () => {
        downloadJson('doc.json', { z: [1, 2], a: 'x' });
        const parsed = JSON.parse(await text()) as Record<string, unknown>;
        expect(Object.keys(parsed)).toEqual(['z', 'a']);
        expect(parsed.z).toEqual([1, 2]);
    });

    it('releases the object URL it created', () => {
        downloadJson('doc.json', {});
        expect(captured.revoked).toEqual(['blob:mock/1']);
    });

    it('handles the degenerate payloads without throwing', async () => {
        downloadJson('null.json', null);
        expect(await captured.blobs[0].text()).toBe('null');
        downloadJson('arr.json', []);
        expect(await captured.blobs[1].text()).toBe('[]');
    });

    it('propagates a circular structure rather than downloading a broken file', () => {
        const cyclic: Record<string, unknown> = {};
        cyclic.self = cyclic;
        expect(() => downloadJson('bad.json', cyclic)).toThrow();
        expect(captured.anchors).toHaveLength(0);
    });
});
