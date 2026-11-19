/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The trade-offs of the ways an agent can authenticate, shown wherever an
 * administrator chooses one: what each option proves, what a copy of its
 * credential can do, and the docs guide that compares them.
 *
 * `TradeOffList` takes any options. The identity policy editor shows the
 * credential levels (`LevelTradeOffs`), the API key dialog an API key
 * against workload identity (`KeyTradeOffs`). Any other choice of options
 * (identity provider presets, say) is shown through `TradeOffList` too, with
 * its rows beside the others in `lib/identityPolicy.ts`.
 */

import React from 'react';

import { ExternalLink } from 'lucide-react';

import {
    CHOOSING_IDENTITY_URL,
    KEY_PRODUCTION_NOTE,
    KEY_TRADE_OFFS,
    LEVEL_TRADE_OFFS,
    levelsToOffer,
    type TradeOff,
} from '../../lib/identityPolicy';
import { TABLE_TBODY, TABLE_TH_TYPE } from '../ui/tableStyles';

interface TradeOffListProps {
    rows: readonly TradeOff[];
    /** One line under the rows. */
    note?: string;
    /** The option currently chosen, marked. */
    selected?: string;
    testId: string;
}

export const TradeOffList: React.FC<TradeOffListProps> = ({ rows, note, selected, testId }) => (
    <div className="border border-default rounded-lg bg-surface-alt p-3 text-xs space-y-2" data-testid={testId}>
        <table className="w-full">
            <thead>
                <tr className="text-left">
                    <th className={`${TABLE_TH_TYPE} pr-3 pb-1`}> </th>
                    <th className={`${TABLE_TH_TYPE} pr-3 pb-1`}>Proves</th>
                    <th className={`${TABLE_TH_TYPE} pb-1`}>A copied credential</th>
                </tr>
            </thead>
            <tbody className={TABLE_TBODY}>
                {rows.map((row) => (
                    <tr
                        key={row.option}
                        data-testid={`trade-off-${row.option}`}
                        data-selected={selected === row.option ? 'true' : 'false'}
                        className={selected === row.option ? 'text-default font-medium' : 'text-secondary'}
                    >
                        <td className="pr-3 py-0.5 align-top whitespace-nowrap">{row.label}</td>
                        <td className="pr-3 py-0.5 align-top">{row.proves}</td>
                        <td className="py-0.5 align-top">{row.copied}</td>
                    </tr>
                ))}
            </tbody>
        </table>
        {note && (
            <p className="text-secondary" data-testid="trade-offs-note">
                {note}
            </p>
        )}
        <a
            href={CHOOSING_IDENTITY_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-primary-600 hover:underline"
        >
            Choosing how agents authenticate <ExternalLink className="w-3 h-3" />
        </a>
    </div>
);

/** The credential levels the policy is offered, beside the policy's minimum level (`levelsToOffer`). */
export const LevelTradeOffs: React.FC<{ selected?: string }> = ({ selected }) => {
    const offered = levelsToOffer(selected);
    return <TradeOffList rows={LEVEL_TRADE_OFFS.filter((row) => offered.some((level) => level === row.option))} selected={selected} testId="trade-offs-levels" />;
};

/** An API key against workload identity, in the API key dialog. */
export const KeyTradeOffs: React.FC = () => <TradeOffList rows={KEY_TRADE_OFFS} note={KEY_PRODUCTION_NOTE} testId="trade-offs-keys" />;

export default TradeOffList;
