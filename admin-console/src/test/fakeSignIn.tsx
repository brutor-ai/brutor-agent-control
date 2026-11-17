/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A sign-in item that is nobody's (slot `app.sign-in`): a tenant field and a
 * footer line.
 * The Community tests of the sign-in form use it for what the form does with
 * a contributed item; the real one is the Enterprise extension's.
 */

import type { SignInSlotItem, SlotRegistry } from '../app/slots';

export const FAKE_SIGN_IN: SignInSlotItem = {
    id: 'fake',
    tenant: {
        initial: 'first',
        Field: ({ value, onChange, disabled }) => (
            <input placeholder="Enter tenant ID" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
        ),
    },
    footer: 'Fake footer line',
};

export const FAKE_SIGN_IN_SLOTS: SlotRegistry = { 'app.sign-in': [FAKE_SIGN_IN] };
