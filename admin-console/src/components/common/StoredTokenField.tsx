/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The field of a token, or another secret, the Control Plane stores and
 * never returns.
 *
 * The Control Plane answers whether a token is stored, never the token. The
 * field therefore holds only what is typed now: it starts empty, says
 * whether a token is stored, and says what saving does to it. Removing a
 * stored token is asked for in so many words, never read from an empty
 * field; it is not offered for a secret its owner cannot do without.
 */

import React from 'react';

interface StoredTokenFieldProps {
    /** The id of the input; the label points at it. */
    id: string;
    label: string;
    /** What is typed now. */
    value: string;
    onChange: (typed: string) => void;
    /** Whether a token is stored. Undefined for something that is not saved yet. */
    stored?: boolean;
    /** Whether removing the stored token is asked for. */
    removeStored: boolean;
    onRemoveStoredChange: (remove: boolean) => void;
    /** What the token belongs to, in "Type the token of this ...". */
    owner: string;
    /** The test id of the sentence that says whether a token is stored. */
    storedTestId: string;
    /** What the secret is called in the sentences, after "a". "token" unless given. */
    noun?: string;
    /** Whether a stored secret may be removed. False for one its owner needs. True unless given. */
    removable?: boolean;
    /** The placeholder while nothing is stored. */
    emptyPlaceholder?: string;
}

const StoredTokenField: React.FC<StoredTokenFieldProps> = ({
    id,
    label,
    value,
    onChange,
    stored,
    removeStored,
    onRemoveStoredChange,
    owner,
    storedTestId,
    noun = 'token',
    removable = true,
    emptyPlaceholder = 'Enter your bearer token',
}) => (
    <div>
        <label htmlFor={id} className="block text-sm font-medium text-secondary mb-2">
            {label}
        </label>
        <input
            id={id}
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={removeStored}
            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-50"
            placeholder={stored ? `Leave empty to keep the stored ${noun}` : emptyPlaceholder}
        />
        {stored !== undefined && (
            <p className="text-xs text-muted mt-1" data-testid={storedTestId}>
                {stored
                    ? `A ${noun} is stored. It is never shown. Leave the field empty to keep it, or type a new one to replace it.`
                    : `No ${noun} is stored. Type the ${noun} of this ${owner}.`}
            </p>
        )}
        {stored && removable && (
            <label className="flex items-center space-x-2 mt-2 text-sm text-secondary cursor-pointer">
                <input
                    type="checkbox"
                    checked={removeStored}
                    onChange={(e) => {
                        onRemoveStoredChange(e.target.checked);
                        if (e.target.checked) onChange('');
                    }}
                    className="rounded border-strong"
                />
                <span>Remove the stored {noun}</span>
            </label>
        )}
    </div>
);

export default StoredTokenField;
