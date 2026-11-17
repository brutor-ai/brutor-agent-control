/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tiny CSV writer for "export exactly what's on screen" buttons.
 *
 * RFC 4180 quoting, and a UTF-8 BOM so Excel opens the file with the
 * right encoding — the FinOps consumer of these files lives in Excel.
 */

export function toCsv(headers: string[], rows: Array<Array<string | number | null | undefined>>): string {
    const esc = (v: string | number | null | undefined): string => {
        if (v === null || v === undefined) return '';
        const s = String(v);
        return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [headers, ...rows].map((r) => r.map(esc).join(',')).join('\r\n') + '\r\n';
}

export function downloadCsv(
    filename: string,
    headers: string[],
    rows: Array<Array<string | number | null | undefined>>,
): void {
    downloadBlob(filename, new Blob(['﻿' + toCsv(headers, rows)], { type: 'text/csv;charset=utf-8' }));
}

export function downloadBlob(filename: string, blob: Blob): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
