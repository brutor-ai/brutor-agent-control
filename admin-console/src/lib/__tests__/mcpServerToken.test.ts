/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { serverTokenForCreate, serverTokenForUpdate } from '../mcpServerToken';

describe('the static token of an MCP server, as a save names it', () => {
    it('an update that types nothing does not name the token, so the stored one is kept', () => {
        expect(serverTokenForUpdate({ authType: 'bearer', typed: '  ', removeStored: false, stored: true })).toBeUndefined();
    });

    it('an update that types a token sends it', () => {
        expect(serverTokenForUpdate({ authType: 'bearer', typed: ' tok ', removeStored: false, stored: true })).toBe('tok');
    });

    it('an update that asks to remove the stored token sends an empty string', () => {
        expect(serverTokenForUpdate({ authType: 'bearer', typed: '', removeStored: true, stored: true })).toBe('');
    });

    it('a server saved without bearer authentication keeps no stored token', () => {
        // The gateway sends a stored token whatever the authentication type says.
        expect(serverTokenForUpdate({ authType: 'none', typed: 'tok', removeStored: false, stored: true })).toBe('');
        expect(serverTokenForUpdate({ authType: 'oauth', typed: '', removeStored: false, stored: true })).toBe('');
    });

    it('a server without bearer authentication and with no stored token does not name the token', () => {
        expect(serverTokenForUpdate({ authType: 'none', typed: 'tok', removeStored: false, stored: false })).toBeUndefined();
    });

    it('a new server sends the typed token, for bearer authentication only', () => {
        expect(serverTokenForCreate({ authType: 'bearer', typed: ' tok ' })).toBe('tok');
        expect(serverTokenForCreate({ authType: 'bearer', typed: '' })).toBeUndefined();
        expect(serverTokenForCreate({ authType: 'oauth', typed: 'tok' })).toBeUndefined();
    });
});
