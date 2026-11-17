/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

import type { InUseGroup } from '../../lib/inUseRefusal';

/**
 * What uses a row the Control Plane refused to delete: one list per kind,
 * each row by name with its id. Renders nothing for no group.
 */
const InUseList: React.FC<{ groups: readonly InUseGroup[] }> = ({ groups }) => {
    if (groups.length === 0) return null;
    return (
        <div data-testid="in-use-list" className="space-y-2">
            {groups.map((group) => (
                <div key={group.kind}>
                    <p className="text-xs font-semibold text-default">{group.label}</p>
                    <ul className="mt-1 space-y-0.5">
                        {group.rows.map((row) => (
                            <li key={row.id} className="text-xs text-secondary flex items-center gap-2">
                                <span>{row.name}</span>
                                <code className="text-[11px] bg-surface-inset px-1 py-0.5 rounded">{row.id}</code>
                            </li>
                        ))}
                    </ul>
                </div>
            ))}
        </div>
    );
};

export default InUseList;
