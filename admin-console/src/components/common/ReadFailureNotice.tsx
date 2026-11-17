/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What a part of a screen shows where its read failed (`useRead`,
 * `useReadFailures`): the sentence of the failure and a Retry. It is not
 * the empty state: the empty state says there is nothing, this says the
 * page does not know.
 *
 *   - Nothing is shown of the part (`loadedAt` null): the sentence, as an
 *     alert.
 *   - The part still shows what it loaded before: "Could not refresh;
 *     showing what was loaded at HH:MM", then the sentence, as a status.
 *     The mark is the words; the colour only goes with them.
 *
 * Renders nothing for a read that did not fail, so a page places it once,
 * beside the part, and passes the failure it holds.
 */

import React from 'react';

import InfoBanner from './InfoBanner';
import { loadedAtClock, type ReadFailure } from '../../hooks/useRead';

interface ReadFailureNoticeProps {
    failure: ReadFailure | null | undefined;
    /** Asks again. Without it no Retry is offered. */
    onRetry?: () => void;
    /** One line of small text, for a field, a card or a cell. The default is a banner for a panel or a section. */
    compact?: boolean;
    className?: string;
    'data-testid'?: string;
}

const ReadFailureNotice: React.FC<ReadFailureNoticeProps> = ({ failure, onRetry, compact = false, className, 'data-testid': testId }) => {
    if (!failure) return null;
    const stale = failure.loadedAt !== null;
    const words = (
        <>
            {stale && <span className="font-medium">Could not refresh; showing what was loaded at {loadedAtClock(failure.loadedAt ?? 0)}. </span>}
            <span>{failure.sentence}</span>
        </>
    );
    const retry = onRetry && (
        <button
            type="button"
            onClick={onRetry}
            className={compact ? 'ml-2 underline font-medium' : 'ml-3 px-2 py-0.5 text-xs font-medium border border-current rounded hover:opacity-80'}
        >
            Retry
        </button>
    );
    const role = stale ? 'status' : 'alert';
    const id = testId ?? (stale ? 'read-stale' : 'read-failed');

    if (compact) {
        return (
            // A span, so that it may stand in a paragraph, a cell or a label as well as in a block.
            <span role={role} data-testid={id} className={['block text-xs', stale ? 'text-amber-700' : 'text-red-600', className ?? ''].filter(Boolean).join(' ')}>
                {words}
                {retry}
            </span>
        );
    }
    return (
        <div role={role} data-testid={id}>
            <InfoBanner variant={stale ? 'warning' : 'error'} className={className ?? 'mb-3 !p-3'}>
                {words}
                {retry}
            </InfoBanner>
        </div>
    );
};

export default ReadFailureNotice;
