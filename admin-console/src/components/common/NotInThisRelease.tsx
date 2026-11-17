/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { Info } from 'lucide-react';

/**
 * A stored value of a setting the console no longer offers, because the
 * gateway does not act on it in this release. It is shown read-only, with
 * what it is, so an administrator is not misled by a value that has no
 * effect; nothing here changes it. `consequence` says what a save of the
 * form does with it, where that is not "keeps it".
 */
const NotInThisRelease: React.FC<{
    label: string;
    value: React.ReactNode;
    testId?: string;
    consequence?: string;
}> = ({ label, value, testId, consequence }) => (
    <div
        data-testid={testId}
        className="flex items-start gap-2 rounded-md border border-default bg-surface-alt px-3 py-2 text-xs text-secondary"
    >
        <Info className="w-4 h-4 mt-0.5 flex-shrink-0 text-muted" aria-hidden="true" />
        <p>
            <span className="font-medium text-default">{label}</span>: {value} — not available in this release: it has
            no effect.{consequence ? ` ${consequence}` : ''}
        </p>
    </div>
);

export default NotInThisRelease;
