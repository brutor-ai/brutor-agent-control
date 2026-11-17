/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Prev / "Page n of m" / Next for a server-paged list (`offset` + `limit`
 * against a `total`). Shared by the system audit log and the AI System run
 * list. Test ids are `<testIdPrefix>-prev`, `-next` and `-page`.
 */

import { ChevronLeft, ChevronRight } from 'lucide-react';

interface PaginationProps {
    offset: number;
    pageSize: number;
    total: number;
    onChange: (next: number) => void;
    testIdPrefix: string;
    /** Shown at the left, e.g. "runs" ⇒ "Showing 51–100 of 431 runs". */
    noun?: string;
    className?: string;
}

export function Pagination({ offset, pageSize, total, onChange, testIdPrefix, noun, className = '' }: PaginationProps) {
    const page = Math.floor(offset / pageSize) + 1;
    const pages = Math.max(1, Math.ceil(total / pageSize));
    const atFirst = offset === 0;
    const atLast = offset + pageSize >= total;
    const button = 'inline-flex items-center gap-1 px-2 py-1 rounded border border-default disabled:opacity-40';
    return (
        <div className={`flex items-center gap-2 text-xs ${noun ? 'justify-between' : 'justify-end'} ${className}`}>
            {noun && (
                <span className="text-muted" data-testid={`${testIdPrefix}-range`}>
                    Showing {total === 0 ? 0 : offset + 1}–{Math.min(offset + pageSize, total)} of {total} {noun}.
                </span>
            )}
            <div className="flex items-center gap-2">
                <button
                    type="button"
                    disabled={atFirst}
                    data-testid={`${testIdPrefix}-prev`}
                    onClick={() => onChange(Math.max(0, offset - pageSize))}
                    className={button}
                >
                    <ChevronLeft className="w-3 h-3" />
                    Prev
                </button>
                <span className="text-secondary" data-testid={`${testIdPrefix}-page`}>
                    Page {page} of {pages}
                </span>
                <button
                    type="button"
                    disabled={atLast}
                    data-testid={`${testIdPrefix}-next`}
                    onClick={() => onChange(offset + pageSize)}
                    className={button}
                >
                    Next
                    <ChevronRight className="w-3 h-3" />
                </button>
            </div>
        </div>
    );
}
