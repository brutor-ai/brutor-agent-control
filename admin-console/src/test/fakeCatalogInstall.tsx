/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An installer of containerised catalog entries that is nobody's (slot
 * `mcp-catalog-import.steps`): it adds `deploy_container` to the import,
 * gives the call five minutes, and its gate lets the wizard through unless
 * the entry is named "Blocked". The Community tests of the import wizard use
 * it for what the wizard does with a contributed installer; the real one is
 * the Enterprise extension's.
 */

import type { CatalogInstallSlotItem, SlotRegistry } from '../app/slots';

export const FAKE_CATALOG_INSTALL: CatalogInstallSlotItem = {
    id: 'fake',
    runtime: 'containerized',
    request: { deploy_container: true },
    timeoutMs: 300_000,
    Gate: ({ entry, onClose, children }) =>
        entry.name === 'Blocked' ? (
            <div>
                <p>the fake installer cannot install {entry.name}</p>
                <button onClick={onClose}>Close the fake gate</button>
            </div>
        ) : (
            <>{children}</>
        ),
};

export const FAKE_CATALOG_INSTALL_SLOTS: SlotRegistry = { 'mcp-catalog-import.steps': [FAKE_CATALOG_INSTALL] };
