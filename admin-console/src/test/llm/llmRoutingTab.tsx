/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the LLM routing tab test share: the toast and
 * gateway client doubles, and the stubs of the tab's three built-in views.
 * The tab's own cases are Community tests
 * (`components/llm/router/__tests__/LLMRoutingTab.test.tsx`); the cases that
 * need a real Enterprise view on the tab are under `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the tab, so that a mock factory can await it.
 */

import { vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

export const toastMock = { success: vi.fn(), error: vi.fn() };
/** `react-hot-toast`. */
export const toastModule = () => ({ default: toastMock });

export const client = { getRouterStatus: vi.fn() };
const gateway = { gatewayClient: client };
/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => gateway });

/** `RoutingGroupsSubTab`: can ask for a refresh and say that its form opened or closed. */
export const groupsSubTabModule = () => ({
    default: ({
        onRefresh,
        onFormStateChange,
    }: {
        onRefresh: () => void;
        onFormStateChange: (active: boolean) => void;
    }) => (
        <div>
            groups view
            <button onClick={onRefresh}>child refresh</button>
            <button onClick={() => onFormStateChange(true)}>open form</button>
            <button onClick={() => onFormStateChange(false)}>close form</button>
        </div>
    ),
});

/** `RouterStatusSubTab`: shows the routing strategy it was given. */
export const statusSubTabModule = () => ({
    default: ({ status }: { status: { routing_strategy: string } | null }) => (
        <div>status view: {status?.routing_strategy ?? 'none'}</div>
    ),
});

/** `RouterTestSubTab`. */
export const testSubTabModule = () => ({ default: () => <div>test view</div> });

export function click(name: string) {
    fireEvent.click(screen.getByRole('button', { name }));
}
