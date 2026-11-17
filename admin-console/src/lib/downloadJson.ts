/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Client-side JSON download. Deliberately not a server round-trip when the
 * document is already on screen: the exported artifact must be exactly the
 * reviewed one. Canonical documents fetched for export are downloaded
 * verbatim for the same reason.
 */
export function downloadJson(filename: string, data: unknown): void {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}
