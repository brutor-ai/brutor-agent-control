/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What an entry point of the console does: the error
 * boundary, the loading screen, the toast container and the first render.
 * Each distribution has an entry point of its own (`src/index.tsx` is the
 * Community one) and this is everything they share; they differ only in the
 * extensions they pass.
 */

import React from 'react';
import { createRoot } from 'react-dom/client';
import { Toaster } from 'react-hot-toast';

import App from '../App';
import { debugConfiguration } from '../lib/debugLog';
import type { AdminExtension } from './extensions';
import { ErrorBoundary, LoadingScreen } from './MountParts';

/**
 * Mount the console in the page's `#root`, composed with `extensions`. An entry point calls this once: the Community one with
 * none, another distribution's with its own.
 */
export function mountConsole(extensions: readonly AdminExtension[] = []): void {
  debugConfiguration();

  const container = document.getElementById('root');
  
  if (!container) {
    console.error('Failed to find the root element');
    return;
  }

  const root = createRoot(container);

  // Show loading screen briefly to ensure smooth startup
  root.render(<LoadingScreen />);

  // Small delay to show loading screen, then render main app
  setTimeout(() => {
    root.render(
      <React.StrictMode>
        <ErrorBoundary>
          <App extensions={extensions} />
          {/*
            Global toast container. Without this, every
            `toast.error(...)` / `toast.success(...)` call
            across the admin UI is a silent no-op — the
            library writes to a portal that never gets
            mounted, so the user sees nothing: a failed
            save or delete would look like "I hit Save and
            nothing happened, no error message".

            `containerStyle.zIndex` is bumped above every
            modal in the app (the highest fixed-modal z-
            index in `components/llm/router/RoutingGroupForm`
            etc. is 1000) so an error toast surfaced from
            INSIDE a modal still reads. Default duration
            keeps successes brief and errors longer because
            the user actually needs time to read the failure
            reason.
          */}
          <Toaster
            position="top-right"
            containerStyle={{ zIndex: 10000 }}
            toastOptions={{
              duration: 4000,
              error: { duration: 6000 },
            }}
          />
        </ErrorBoundary>
      </React.StrictMode>
    );
  }, 100);
}
