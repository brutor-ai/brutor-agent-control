/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Shared harness for the two client-side "save this file" helpers
 * (`downloadJson`, `downloadBlobAsFile`). Both mint an object URL, click a
 * detached anchor and revoke the URL, so both need the same three things
 * intercepted. Kept in one place so the two tests cannot drift.
 *
 * Call `installBlobDownloadSpies()` in `beforeEach` and
 * `vi.restoreAllMocks()` in `afterEach` — every spy is installed with
 * `vi.spyOn`, so nothing needs manual teardown.
 */

import { vi } from 'vitest';

export interface CapturedDownload {
    /** Blobs handed to `URL.createObjectURL`, in order. */
    blobs: Blob[];
    /** URLs passed to `URL.revokeObjectURL`, in order. */
    revoked: string[];
    /** Every `<a>` the code under test created. */
    anchors: HTMLAnchorElement[];
    /** How many times each anchor was clicked, by anchor index. */
    clicks: number[];
}

export function installBlobDownloadSpies(): CapturedDownload {
    const captured: CapturedDownload = { blobs: [], revoked: [], anchors: [], clicks: [] };

    vi.spyOn(URL, 'createObjectURL').mockImplementation((blob: Blob | MediaSource) => {
        captured.blobs.push(blob as Blob);
        return `blob:mock/${captured.blobs.length}`;
    });
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation((url: string) => {
        captured.revoked.push(url);
    });

    const realCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
        const el = realCreateElement(tag);
        if (el instanceof HTMLAnchorElement) {
            const index = captured.anchors.length;
            captured.anchors.push(el);
            captured.clicks.push(0);
            el.addEventListener('click', (e) => {
                // Detached anchors would otherwise ask happy-dom to navigate.
                e.preventDefault();
                captured.clicks[index] += 1;
            });
        }
        return el;
    });

    return captured;
}
