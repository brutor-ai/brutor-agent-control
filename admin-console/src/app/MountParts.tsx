/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What `mount.tsx` renders around the console: the error boundary that
 * catches what a page throws, and the screen shown until the first render.
 */

import { isDevelopmentBuild } from '../env';
import React from 'react';

import { consoleName } from './productName';
import { errorKind } from '../lib/narrow';

// Error boundary component for better error handling
export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error?: Error }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error(`${consoleName()} Error:`, errorKind(error));
    // The names of the components above the one that failed: no data of a user is in them.
    console.error(`Component stack:${errorInfo.componentStack ?? ''}`);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-surface-alt flex items-center justify-center p-4">
          <div className="max-w-md w-full bg-surface rounded-lg shadow-lg p-6 text-center">
            <div className="w-16 h-16 mx-auto mb-4 bg-red-100 rounded-full flex items-center justify-center">
              <svg className="w-8 h-8 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.732 16.5c-.77.833.192 2.5 1.732 2.5z" />
              </svg>
            </div>
            <h1 className="text-xl font-bold text-default mb-2">Something went wrong</h1>
            <p className="text-secondary mb-6">
              The {consoleName()} encountered an unexpected error. Please try refreshing the page.
            </p>
            <div className="space-y-3">
              <button
                onClick={() => window.location.reload()}
                className="w-full px-4 py-2 bg-primary text-primary rounded-md hover:bg-primary-600 transition-colors"
              >
                Refresh Page
              </button>
              {isDevelopmentBuild && this.state.error && (
                <details className="mt-4 text-left">
                  <summary className="cursor-pointer text-sm text-secondary hover:text-default">
                    Show Error Details (Development)
                  </summary>
                  <pre className="mt-2 p-3 bg-surface-raised rounded text-xs overflow-auto max-h-32">
                    {this.state.error.stack}
                  </pre>
                </details>
              )}
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

// Loading component for initial app load
export const LoadingScreen: React.FC = () => (
  <div className="min-h-screen bg-surface-alt flex items-center justify-center">
    <div className="text-center">
      <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
      <h2 className="text-lg font-semibold text-default mb-2">{consoleName()}</h2>
      <p className="text-secondary">Loading application...</p>
    </div>
  </div>
);
