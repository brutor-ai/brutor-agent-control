/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Governance → Incident Windows: every declared period in the
 * tenant whose runs are excluded from judgement but kept as evidence — a
 * provider outage across every system, a planned maintenance on one. The
 * same list each AI System shows for itself, tenant-wide.
 */

import { CalendarX } from 'lucide-react';

import IncidentWindowsPanel from '../ai-systems/IncidentWindowsPanel';

export function IncidentWindowsTab() {
    return (
        <div className="p-6 space-y-6" data-testid="incident-windows-tab">
            <div className="flex items-center space-x-3">
                <div className="p-2.5 rounded-xl bg-surface-raised">
                    <CalendarX className="w-7 h-7 text-muted" />
                </div>
                <div>
                    <h1 className="text-2xl font-bold text-default">Incident Windows</h1>
                    <p className="text-sm text-muted">
                        Declared periods excluded from judgement and kept as evidence — unmeasured, not passed.
                    </p>
                </div>
            </div>
            <IncidentWindowsPanel />
        </div>
    );
}

export default IncidentWindowsTab;
