/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One line, above a list or a table that shows AI Systems by name, for when
 * the names could not be loaded (`useAISystemNames`): the rows then show
 * ids, and this says so, with a Retry.
 */

import React from 'react';

import ReadFailureNotice from '../common/ReadFailureNotice';
import type { AISystemNames } from '../../hooks/useAISystemNames';

const AISystemNamesNotice: React.FC<{ names: Pick<AISystemNames, 'failure' | 'retry'>; className?: string }> = ({ names, className }) => (
    <ReadFailureNotice compact className={className} failure={names.failure} onRetry={names.retry} data-testid="ai-system-names-failed" />
);

export default AISystemNamesNotice;
