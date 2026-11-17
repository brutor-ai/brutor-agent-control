/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A Composition node's link: a button that opens another page (App tab id)
 * when the host passes `open` and the page is part of this console, plain
 * text otherwise. Shared by the Simple
 * graph, the Diagram and the List view so every node links the same way.
 */

import React from 'react';

export function LinkText({
  to,
  open,
  className,
  children,
  testId,
}: {
  /** The tab to open; null for a page this console does not have. */
  to: string | null;
  open?: (tab: string) => void;
  className?: string;
  children: React.ReactNode;
  testId?: string;
}) {
  if (!open || to === null) {
    return (
      <span className={className} data-testid={testId}>
        {children}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => open(to)}
      className={`${className ?? ''} text-left hover:underline`}
      data-link={to}
      data-testid={testId}
    >
      {children}
    </button>
  );
}
