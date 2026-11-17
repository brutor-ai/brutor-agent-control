/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A delete the Control Plane refused because the row is in use.
 *
 * The refusal is a 409 whose `detail` holds the sentence (`message`) and
 * what uses the row, by kind (`used_by`: `virtual_servers`,
 * `server_configs`, `administrators`, `oauth_app_clients`, `mcp_servers`,
 * `agent_identities`), each a list of `{id, name}`. A kind nothing of which
 * uses the row is left out. The console lists what uses the row from this
 * structure; the sentence is shown above it as the Control Plane words it.
 */

import type { components } from './generated/control-plane-api';
import { errorStatus, getArray, getRecord, getString, isRecord } from './narrow';

/** A row that depends on the one that was to be deleted. */
export type InUseRow = components['schemas']['UsedBy'];

/** The kinds of row the console names, in the order it lists them. */
const KIND_LABELS: ReadonlyArray<readonly [string, string]> = [
    ['virtual_servers', 'Virtual servers'],
    ['server_configs', 'Server configurations'],
    ['administrators', 'Administrators'],
    ['oauth_app_clients', 'OAuth app clients'],
    ['mcp_servers', 'MCP servers'],
    ['agent_identities', 'Agent identities'],
];

export interface InUseGroup {
    /** The key under `used_by`. */
    kind: string;
    /** What the console calls the kind; the key itself for a kind it does not know. */
    label: string;
    rows: InUseRow[];
}

export interface InUseRefusal {
    message: string;
    groups: InUseGroup[];
}

function rowsOf(value: unknown): InUseRow[] {
    if (!Array.isArray(value)) return [];
    return value.flatMap((row: unknown) => {
        const id = getString(row, 'id');
        const name = getString(row, 'name');
        return id !== undefined && name !== undefined ? [{ id, name }] : [];
    });
}

/**
 * What uses the row, when `error` is the Control Plane's 409 refusing a
 * delete for that reason; null for any other error.
 */
export function inUseRefusalOf(error: unknown): InUseRefusal | null {
    if (errorStatus(error) !== 409 || !isRecord(error)) return null;
    const detail = getRecord(error.details, 'detail');
    const usedBy = getRecord(detail, 'used_by');
    if (!detail || !usedBy) return null;
    const known = KIND_LABELS.map(([kind]) => kind);
    const kinds = [...known.filter((kind) => kind in usedBy), ...Object.keys(usedBy).filter((kind) => !known.includes(kind))];
    const groups = kinds
        .map((kind) => ({
            kind,
            label: KIND_LABELS.find(([key]) => key === kind)?.[1] ?? kind,
            rows: rowsOf(getArray(usedBy, kind)),
        }))
        .filter((group) => group.rows.length > 0);
    return { message: getString(detail, 'message') ?? '', groups };
}
