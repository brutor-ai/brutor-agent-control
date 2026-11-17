/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import MissionControlAlertsDrawer from '../MissionControlAlertsDrawer';
import { isMirroredInInbox } from '../../../lib/systemAlerts';
import type { SystemAlert } from '../../../lib/groupTypes';

function alert(overrides: Partial<SystemAlert>): SystemAlert {
    return {
        id: 'a1', tenant_id: 't1', severity: 'high', category: 'downstream_provider', source: 'openai',
        resource_type: null, resource_id: null, resource_name: null, error_code: null,
        title: 'Provider failing', message: '', context: null, status: 'active',
        first_seen_at: null, last_seen_at: null, occurrence_count: 1,
        acknowledged_at: null, acknowledged_by: null, resolved_at: null, resolved_by: null,
        resolution_note: null, dedup_key: 'k', ...overrides,
    };
}

describe('MissionControlAlertsDrawer — ops-only', () => {
    it('points at the Inbox for open findings instead of listing drift', () => {
        const onOpenInbox = vi.fn();
        render(
            <MissionControlAlertsDrawer
                usageAlerts={[]}
                systemAlerts={[alert({})]}
                driftFeed={[{ id: 'd1', severity: 'high', drift_class: 'x', metric_key: 'cost' } as never]}
                busyActions={new Set()}
                onActionBusy={vi.fn()}
                onSystemAlertResolved={vi.fn()}
                inboxOpen={7}
                onOpenInbox={onOpenInbox}
                onClose={vi.fn()}
                onAcknowledgeUsage={vi.fn()}
                onAcknowledgeSystem={vi.fn()}
                onResolveSystem={vi.fn()}
            />,
        );
        const line = screen.getByTestId('mc-drawer-inbox-line');
        expect(line).toHaveTextContent('7 open assurance findings');
        fireEvent.click(line);
        expect(onOpenInbox).toHaveBeenCalled();
        expect(screen.queryByText(/Open drift findings/)).not.toBeInTheDocument();
        expect(screen.getByTestId('mc-system-alert-a1')).toBeInTheDocument();
    });

    it('recognises the system alerts that are also inbox findings', () => {
        expect(isMirroredInInbox(alert({ category: 'ai_system_liveness', source: 'ai_system.sys-1', resource_type: 'ai_system' }))).toBe(true);
        expect(isMirroredInInbox(alert({ category: 'downstream_provider' }))).toBe(false);
    });
});
