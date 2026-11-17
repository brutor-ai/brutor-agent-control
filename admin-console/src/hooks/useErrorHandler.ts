/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Enhanced Error Handling Hook for AI Gateway Admin
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import { asyncHandler } from '../lib/asyncSafe';
import { errorKind } from '../lib/narrow';

/**
 * Structural view of the errors this handler classifies: fetch failures,
 * RegistryAPIError, and plain Errors all fit. Optional everything — the
 * classifiers probe with ?. and default sensibly.
 */
export interface ErrorLike {
  name?: string;
  message?: string;
  status?: number;
  code?: string;
  details?: unknown;
}

export interface ErrorDetails {
  id: string;
  message: string;
  code?: string;
  details?: any;
  timestamp: Date;
  context?: {
    operation?: string;
    resource?: string;
    userId?: string;
    [key: string]: any;
  };
  retryable?: boolean;
  severity: 'low' | 'medium' | 'high' | 'critical';
}

export interface RetryConfig {
  maxAttempts: number;
  backoffMs: number;
  backoffMultiplier: number;
}

export interface ErrorHandlerOptions {
  // Global error reporting
  reportError?: (error: ErrorDetails) => void;
  
  // Default retry configuration
  defaultRetryConfig?: RetryConfig;
  
  // Auto-dismiss configuration
  autoDismissMs?: number;
  
  // Max errors to keep in state
  maxErrors?: number;
}

interface RetryAttempt {
  errorId: string;
  attempt: number;
  nextAttemptAt: Date;
  operation: () => Promise<any>;
}

export const useErrorHandler = (options: ErrorHandlerOptions = {}) => {
  const {
    reportError,
    defaultRetryConfig = { maxAttempts: 3, backoffMs: 1000, backoffMultiplier: 2 },
    autoDismissMs = 10000,
    maxErrors = 10
  } = options;

  const [errors, setErrors] = useState<ErrorDetails[]>([]);
  const [retryAttempts, setRetryAttempts] = useState<Map<string, RetryAttempt>>(new Map());
  
  const timeoutsRef = useRef<Map<string, NodeJS.Timeout>>(new Map());
  const retryTimeoutsRef = useRef<Map<string, NodeJS.Timeout>>(new Map());

  // Generate unique error ID
  const generateErrorId = useCallback(() => {
    return `error_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }, []);

  // Classify error severity based on error details
  const classifyError = useCallback((error: any, context?: any): 'low' | 'medium' | 'high' | 'critical' => {
    if (error?.status >= 500 || error?.code === 'NETWORK_ERROR') {
      return 'critical';
    }
    
    if (error?.status === 401 || error?.status === 403) {
      return 'high';
    }
    
    if (error?.status >= 400 && error?.status < 500) {
      return 'medium';
    }
    
    if (context?.operation?.includes('discovery') || context?.operation?.includes('test')) {
      return 'low';
    }
    
    return 'medium';
  }, []);

  // Determine if error is retryable
  const isRetryable = useCallback((error: any): boolean => {
    if (error?.status >= 500) return true; // Server errors
    if (error?.code === 'NETWORK_ERROR') return true;
    if (error?.code === 'TIMEOUT') return true;
    if (error?.status === 429) return true; // Rate limited
    
    return false;
  }, []);

  // Add error to state
  const addError = useCallback((
    error: any,
    context?: ErrorDetails['context'],
    customSeverity?: ErrorDetails['severity']
  ): string => {
    const errorId = generateErrorId();
    // Callers throughout the app invoke addError two ways:
    //   1) addError(errorObj, context, severity)
    //   2) addError({ message, code, severity, context })  — single object
    // The second form left context+severity stranded on the error
    // object, so consumers filtering by context.resource (e.g. the
    // MCP registry search error banner) never saw anything. Prefer
    // explicit args, then fall back to the same field on `error`.
    const effectiveContext = context ?? error?.context;
    const severity =
      customSeverity ||
      error?.severity ||
      classifyError(error, effectiveContext);
    const retryable = isRetryable(error);

    const errorDetails: ErrorDetails = {
      id: errorId,
      message: error?.message || error?.toString() || 'Unknown error occurred',
      code: error?.code || error?.status?.toString(),
      details: error?.details || error,
      timestamp: new Date(),
      context: effectiveContext,
      retryable,
      severity
    };

    setErrors(prev => {
      const newErrors = [errorDetails, ...prev];
      // Keep only the most recent errors
      return newErrors.slice(0, maxErrors);
    });

    // Report error if handler provided
    if (reportError) {
      try {
        reportError(errorDetails);
      } catch (reportingError) {
        console.error('Error reporting failed:', errorKind(reportingError));
      }
    }

    // Auto-dismiss error if configured
    if (autoDismissMs > 0) {
      const timeoutId = setTimeout(() => {
        dismissError(errorId);
      }, autoDismissMs);
      
      timeoutsRef.current.set(errorId, timeoutId);
    }

    return errorId;
  }, [generateErrorId, classifyError, isRetryable, reportError, autoDismissMs, maxErrors]);

  // Remove error from state
  const dismissError = useCallback((errorId: string) => {
    setErrors(prev => prev.filter(e => e.id !== errorId));
    
    // Clear timeout if exists
    const timeoutId = timeoutsRef.current.get(errorId);
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutsRef.current.delete(errorId);
    }

    // Clear retry timeout if exists
    const retryTimeoutId = retryTimeoutsRef.current.get(errorId);
    if (retryTimeoutId) {
      clearTimeout(retryTimeoutId);
      retryTimeoutsRef.current.delete(errorId);
    }

    // Remove retry attempt
    setRetryAttempts(prev => {
      const newMap = new Map(prev);
      newMap.delete(errorId);
      return newMap;
    });
  }, []);

  // Clear all errors
  const clearErrors = useCallback(() => {
    setErrors([]);
    
    // Clear all timeouts
    timeoutsRef.current.forEach(timeoutId => clearTimeout(timeoutId));
    timeoutsRef.current.clear();
    
    retryTimeoutsRef.current.forEach(timeoutId => clearTimeout(timeoutId));
    retryTimeoutsRef.current.clear();
    
    setRetryAttempts(new Map());
  }, []);

  // Setup retry for an operation
  const setupRetry = useCallback((
    errorId: string,
    operation: () => Promise<any>,
    retryConfig: RetryConfig = defaultRetryConfig
  ) => {
    const existingAttempt = retryAttempts.get(errorId);
    const attemptNumber = existingAttempt ? existingAttempt.attempt + 1 : 1;
    
    if (attemptNumber > retryConfig.maxAttempts) {
      // Max attempts reached, give up
      setErrors(prev => prev.map(error => 
        error.id === errorId 
          ? { ...error, retryable: false }
          : error
      ));
      return;
    }

    const backoffMs = retryConfig.backoffMs * Math.pow(retryConfig.backoffMultiplier, attemptNumber - 1);
    const nextAttemptAt = new Date(Date.now() + backoffMs);
    
    const retryAttempt: RetryAttempt = {
      errorId,
      attempt: attemptNumber,
      nextAttemptAt,
      operation
    };
    
    setRetryAttempts(prev => new Map(prev).set(errorId, retryAttempt));
    
    // Schedule retry
    const retryTimeoutId = setTimeout(asyncHandler(async () => {
      try {
        await operation();
        // Success - dismiss error
        dismissError(errorId);
      } catch {
        // Retry failed - setup next retry or give up
        setupRetry(errorId, operation, retryConfig);
      }
    }, 'timer', 'background'), backoffMs);
    
    retryTimeoutsRef.current.set(errorId, retryTimeoutId);
  }, [retryAttempts, defaultRetryConfig, dismissError]);

  // Manually trigger retry
  const retryError = useCallback(async (errorId: string): Promise<boolean> => {
    const retryAttempt = retryAttempts.get(errorId);
    if (!retryAttempt) {
      return false;
    }

    try {
      await retryAttempt.operation();
      dismissError(errorId);
      return true;
    } catch {
      // Setup next retry
      setupRetry(errorId, retryAttempt.operation);
      return false;
    }
  }, [retryAttempts, dismissError, setupRetry]);

  // Enhanced error handler with automatic retry support
  const handleError = useCallback(async <T>(
    operation: () => Promise<T>,
    context?: ErrorDetails['context'],
    options?: {
      retryConfig?: RetryConfig;
      severity?: ErrorDetails['severity'];
      suppressErrors?: boolean;
    }
  ): Promise<T | null> => {
    try {
      return await operation();
    } catch (error) {
      if (options?.suppressErrors) {
        console.error('Suppressed error:', errorKind(error));
        return null;
      }

      const errorId = addError(error, context, options?.severity);
      
      // Setup automatic retry if error is retryable
      const errorDetails = errors.find(e => e.id === errorId);
      if (errorDetails?.retryable && options?.retryConfig) {
        setupRetry(errorId, operation, options.retryConfig);
      }
      
      throw error;
    }
  }, [addError, errors, setupRetry]);

  // Get errors by severity
  const getErrorsBySeverity = useCallback((severity: ErrorDetails['severity']): ErrorDetails[] => {
    return errors.filter(error => error.severity === severity);
  }, [errors]);

  // Get retryable errors
  const getRetryableErrors = useCallback((): ErrorDetails[] => {
    return errors.filter(error => error.retryable);
  }, [errors]);

  // Check if there are critical errors
  const hasCriticalErrors = useCallback((): boolean => {
    return errors.some(error => error.severity === 'critical');
  }, [errors]);

  // Get retry status for an error
  const getRetryStatus = useCallback((errorId: string) => {
    const retryAttempt = retryAttempts.get(errorId);
    if (!retryAttempt) return null;
    
    const now = new Date();
    const timeUntilRetry = retryAttempt.nextAttemptAt.getTime() - now.getTime();
    
    return {
      attempt: retryAttempt.attempt,
      nextAttemptAt: retryAttempt.nextAttemptAt,
      timeUntilRetryMs: Math.max(0, timeUntilRetry),
      isRetrying: timeUntilRetry <= 0
    };
  }, [retryAttempts]);

  // Cleanup on unmount. The maps are captured here rather than read off the
  // refs inside the cleanup: the refs are stable for the hook's lifetime, and
  // reading `.current` in a cleanup is what react-hooks/exhaustive-deps warns
  // about because a *reassigned* ref would leave the old timers running.
  useEffect(() => {
    const dismissTimeouts = timeoutsRef.current;
    const retryTimeouts = retryTimeoutsRef.current;
    return () => {
      dismissTimeouts.forEach(timeoutId => clearTimeout(timeoutId));
      retryTimeouts.forEach(timeoutId => clearTimeout(timeoutId));
    };
  }, []);

  return {
    // State
    errors,
    hasErrors: errors.length > 0,
    hasCriticalErrors: hasCriticalErrors(),
    
    // Error management
    addError,
    dismissError,
    clearErrors,
    
    // Error queries
    getErrorsBySeverity,
    getRetryableErrors,
    
    // Retry management
    setupRetry,
    retryError,
    getRetryStatus,
    
    // Enhanced operation wrapper
    handleError
  };
};

// Hook for component-level error boundaries
export const useErrorBoundary = () => {
  const [error, setError] = useState<Error | null>(null);
  
  const resetError = useCallback(() => {
    setError(null);
  }, []);
  
  const captureError = useCallback((error: Error) => {
    setError(error);
  }, []);
  
  useEffect(() => {
    const handleUnhandledRejection = (event: PromiseRejectionEvent) => {
      console.error('Unhandled promise rejection:', errorKind(event.reason));
      if (event.reason instanceof Error) {
        captureError(event.reason);
      } else {
        captureError(new Error(`Unhandled rejection: ${event.reason}`));
      }
    };
    
    const handleError = (event: ErrorEvent) => {
      console.error('Unhandled error:', errorKind(event.error));
      captureError(event.error || new Error(event.message));
    };
    
    window.addEventListener('unhandledrejection', handleUnhandledRejection);
    window.addEventListener('error', handleError);
    
    return () => {
      window.removeEventListener('unhandledrejection', handleUnhandledRejection);
      window.removeEventListener('error', handleError);
    };
  }, [captureError]);
  
  return {
    error,
    resetError,
    captureError,
    hasError: error !== null
  };
};

// Utility functions for error analysis
export const errorUtils = {
  /**
   * Check if an error indicates network connectivity issues
   */
  isNetworkError: (error: any): boolean => {
    return (
      error?.code === 'NETWORK_ERROR' ||
      error?.message?.includes('fetch') ||
      error?.message?.includes('Network Error') ||
      error?.status === 0
    );
  },

  /**
   * Check if an error indicates authentication issues
   */
  isAuthError: (error: any): boolean => {
    return error?.status === 401 || error?.status === 403;
  },

  /**
   * Check if an error indicates server issues
   */
  isServerError: (error: any): boolean => {
    return error?.status >= 500 && error?.status < 600;
  },

  /**
   * Check if an error indicates client issues (user error)
   */
  isClientError: (error: any): boolean => {
    return error?.status >= 400 && error?.status < 500;
  },

  /**
   * Extract user-friendly message from error
   */
  getUserFriendlyMessage: (error: any): string => {
    if (errorUtils.isNetworkError(error)) {
      return 'Network connection error. Please check your internet connection.';
    }
    
    if (errorUtils.isAuthError(error)) {
      return 'Authentication required. Please log in again.';
    }
    
    if (errorUtils.isServerError(error)) {
      return 'Server error. Please try again later.';
    }
    
    if (error?.status === 429) {
      return 'Too many requests. Please wait a moment before trying again.';
    }
    
    if (error?.status === 404) {
      return 'The requested resource was not found.';
    }
    
    return error?.message || 'An unexpected error occurred.';
  },

  /**
   * Determine appropriate action for error
   */
  getRecommendedAction: (error: any): string => {
    if (errorUtils.isNetworkError(error)) {
      return 'Check your internet connection and try again.';
    }
    
    if (errorUtils.isAuthError(error)) {
      return 'Please log in again to continue.';
    }
    
    if (errorUtils.isServerError(error)) {
      return 'This appears to be a server issue. Please try again in a few minutes.';
    }
    
    if (error?.status === 429) {
      return 'Please wait a moment before making another request.';
    }
    
    return 'Please try the operation again.';
  }
};