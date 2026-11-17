/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

import { shownValue, storedUnavailable, type UnavailableSetting } from '../../lib/unavailableSettings';
import NotInThisRelease from './NotInThisRelease';

/**
 * Each setting of `settings` the document `doc` holds, read-only, as not
 * available in this release. Nothing for a document that holds none.
 */
const UnavailableSettings: React.FC<{ doc: unknown; settings: readonly UnavailableSetting[]; consequence?: string }> = ({
    doc,
    settings,
    consequence = 'Saving this form removes it.',
}) => {
    const stored = storedUnavailable(doc, settings);
    if (stored.length === 0) return null;
    return (
        <div className="space-y-2" data-testid="unavailable-settings">
            {stored.map((setting) => (
                <NotInThisRelease
                    key={setting.path.join('.')}
                    testId={`unavailable-${setting.path.join('.')}`}
                    label={setting.label}
                    value={shownValue(setting.value)}
                    consequence={consequence}
                />
            ))}
        </div>
    );
};

export default UnavailableSettings;
