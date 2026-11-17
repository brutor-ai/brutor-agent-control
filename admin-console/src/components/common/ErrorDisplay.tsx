/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { ErrorDetails } from '../../hooks/useErrorHandler';
import { withoutRedactedInput } from '../../lib/validationErrors';

export interface ErrorDisplayProps {
  errors: ErrorDetails[];
  onDismiss: (errorId: string) => void;
  onRetry?: (errorId: string) => void;
  className?: string;
}

export const ErrorDisplay: React.FC<ErrorDisplayProps> = ({
  errors,
  onDismiss,
  onRetry,
  className = ''
}) => {
  if (errors.length === 0) return null;

  const getSeverityColor = (severity: ErrorDetails['severity']) => {
    switch (severity) {
      case 'critical': return 'bg-red-50 border-red-200 text-red-800';
      case 'high': return 'bg-orange-50 border-orange-200 text-orange-800';
      case 'medium': return 'bg-yellow-50 border-yellow-200 text-yellow-800';
      case 'low': return 'bg-primary-50 border-sky-border text-primary-800';
      default: return 'bg-surface-alt border-default text-default';
    }
  };

  const getSeverityIcon = (severity: ErrorDetails['severity']) => {
    switch (severity) {
      case 'critical': return '🔴';
      case 'high': return '🟠';
      case 'medium': return '🟡';
      case 'low': return '🔵';
      default: return '⚪';
    }
  };

  return (
    <div className={`space-y-2 ${className}`}>
      {errors.map((error) => (
        <div
          key={error.id}
          className={`p-3 rounded-md border ${getSeverityColor(error.severity)}`}
        >
          <div className="flex items-start justify-between">
            <div className="flex items-start space-x-2 flex-1 min-h-0 min-w-0">
              <span className="text-sm">{getSeverityIcon(error.severity)}</span>
              <div className="flex-1 min-h-0 min-w-0">
                <p className="text-sm font-medium">{error.message}</p>
                {error.context?.operation && (
                  <p className="text-xs opacity-75 mt-1">
                    Operation: {error.context.operation}
                  </p>
                )}
                <p className="text-xs opacity-75">
                  {error.timestamp.toLocaleTimeString()}
                </p>
              </div>
            </div>
            
            <div className="flex items-center space-x-2 ml-4">
              {error.retryable && onRetry && (
                <button
                  onClick={() => onRetry(error.id)}
                  className="px-2 py-1 text-xs bg-surface border border-current rounded hover:bg-opacity-80 transition-colors"
                  title="Retry operation"
                >
                  Retry
                </button>
              )}
              <button
                onClick={() => onDismiss(error.id)}
                className="px-2 py-1 text-xs bg-surface border border-current rounded hover:bg-opacity-80 transition-colors"
                title="Dismiss error"
              >
                ×
              </button>
            </div>
          </div>
          
          {error.details && (
            <details className="mt-2">
              <summary className="text-xs cursor-pointer opacity-75 hover:opacity-100">
                Show details
              </summary>
              <pre className="mt-1 text-xs bg-black bg-opacity-10 p-2 rounded overflow-auto max-h-32">
                {JSON.stringify(withoutRedactedInput(error.details), null, 2)}
              </pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
};