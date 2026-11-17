/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { AlertCircle, CheckCircle } from 'lucide-react';

// Validation error type
export type ValidationErrors = {
    [key: string]: string | undefined;
};

// Validation rules

// Field error display component
interface FieldErrorProps {
    error?: string;
    warning?: string;
}

const FieldError: React.FC<FieldErrorProps> = ({ error, warning }) => {
    if (error) {
        return (
            <div className="mt-1 flex items-start space-x-1 text-xs text-red-600">
                <AlertCircle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                <span>{error}</span>
            </div>
        );
    }
    if (warning) {
        return (
            <div className="mt-1 flex items-start space-x-1 text-xs text-amber-600">
                <AlertCircle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                <span>{warning}</span>
            </div>
        );
    }
    return null;
};

// Field success indicator
interface FieldSuccessProps {
    show: boolean;
}

const FieldSuccess: React.FC<FieldSuccessProps> = ({ show }) => {
    if (!show) return null;
    return (
        <div className="mt-1 flex items-center space-x-1 text-xs text-green-600">
            <CheckCircle className="w-3 h-3" />
            <span>Valid</span>
        </div>
    );
};



// Input component with validation
interface ValidatedInputProps {
    id?: string;
    label: string;
    value: string | number;
    onChange: (value: string) => void;
    onBlur?: () => void;
    error?: string;
    warning?: string;
    showSuccess?: boolean;
    type?: string;
    placeholder?: string;
    required?: boolean;
    disabled?: boolean;
    helpText?: string;
    maxLength?: number;
    step?: string;
    min?: string;
    max?: string;
    list?: string;
}

const ValidatedInput: React.FC<ValidatedInputProps> = ({
                                                           id,
                                                           label,
                                                           value,
                                                           onChange,
                                                           onBlur,
                                                           error,
                                                           warning,
                                                           showSuccess,
                                                           type = 'text',
                                                           placeholder,
                                                           required,
                                                           disabled,
                                                           helpText,
                                                           maxLength,
                                                           step,
                                                           min,
                                                           max,
                                                           list
                                                       }) => {
    const hasError = !!error;
    const hasWarning = !!warning && !error;

    return (
        <div>
            <label className="block text-sm font-medium text-secondary mb-1">
                {label}
                {required && <span className="text-red-500 ml-1">*</span>}
            </label>
            <input
                id={id}
                type={type}
                value={value}
                onChange={(e) => onChange(e.target.value)}
                onBlur={onBlur}
                placeholder={placeholder}
                disabled={disabled}
                maxLength={maxLength}
                step={step}
                min={min}
                max={max}
                list={list}
                className={`w-full px-3 py-2 border rounded-md shadow-sm focus:outline-none focus:ring-2 transition-colors ${
                    hasError
                        ? 'border-red-300 focus:ring-red-500 focus:border-red-500 bg-red-50'
                        : hasWarning
                            ? 'border-amber-300 focus:ring-amber-500 focus:border-amber-500 bg-amber-50'
                            : showSuccess
                                ? 'border-green-300 focus:ring-green-500 focus:border-green-500'
                                : 'border-strong focus:ring-primary-500 focus:border-primary-500'
                } ${disabled ? 'bg-surface-raised cursor-not-allowed' : ''}`}
            />
            <FieldError error={error} warning={warning} />
            {!error && !warning && showSuccess && <FieldSuccess show={true} />}
            {helpText && !error && !warning && (
                <p className="mt-1 text-xs text-muted">{helpText}</p>
            )}
        </div>
    );
};

// Textarea component with validation
interface ValidatedTextareaProps {
    id?: string;
    label: string;
    value: string;
    onChange: (value: string) => void;
    onBlur?: () => void;
    error?: string;
    warning?: string;
    showSuccess?: boolean;
    placeholder?: string;
    required?: boolean;
    rows?: number;
    maxLength?: number;
    showCharCount?: boolean;
    helpText?: string;
}

const ValidatedTextarea: React.FC<ValidatedTextareaProps> = ({
                                                                 id,
                                                                 label,
                                                                 value,
                                                                 onChange,
                                                                 onBlur,
                                                                 error,
                                                                 warning,
                                                                 showSuccess,
                                                                 placeholder,
                                                                 required,
                                                                 rows = 4,
                                                                 maxLength,
                                                                 showCharCount,
                                                                 helpText
                                                             }) => {
    const hasError = !!error;
    const hasWarning = !!warning && !error;
    const charCount = value.length;
    const isNearLimit = maxLength && charCount > maxLength * 0.9;

    return (
        <div>
            <div className="flex items-center justify-between mb-1">
                <label className="block text-sm font-medium text-secondary">
                    {label}
                    {required && <span className="text-red-500 ml-1">*</span>}
                </label>
                {showCharCount && maxLength && (
                    <span className={`text-xs ${
                        charCount > maxLength
                            ? 'text-red-600 font-medium'
                            : isNearLimit
                                ? 'text-amber-600'
                                : 'text-muted'
                    }`}>
                        {charCount.toLocaleString()} / {maxLength.toLocaleString()}
                    </span>
                )}
            </div>
            <textarea
                id={id}
                value={value}
                onChange={(e) => onChange(e.target.value)}
                onBlur={onBlur}
                placeholder={placeholder}
                rows={rows}
                maxLength={maxLength}
                className={`w-full px-3 py-2 border rounded-md shadow-sm focus:outline-none focus:ring-2 transition-colors font-mono text-sm ${
                    hasError
                        ? 'border-red-300 focus:ring-red-500 focus:border-red-500 bg-red-50'
                        : hasWarning
                            ? 'border-amber-300 focus:ring-amber-500 focus:border-amber-500 bg-amber-50'
                            : showSuccess
                                ? 'border-green-300 focus:ring-green-500 focus:border-green-500'
                                : 'border-strong focus:ring-primary-500 focus:border-primary-500'
                }`}
            />
            <FieldError error={error} warning={warning} />
            {!error && !warning && showSuccess && <FieldSuccess show={true} />}
            {helpText && !error && !warning && (
                <p className="mt-1 text-xs text-muted">{helpText}</p>
            )}
        </div>
    );
};

// Validation summary component
interface ValidationSummaryProps {
    errors: ValidationErrors;
    warnings?: ValidationErrors;
}

const ValidationSummary: React.FC<ValidationSummaryProps> = ({ errors, warnings }) => {
    const errorCount = Object.values(errors).filter(Boolean).length;
    const warningCount = warnings ? Object.values(warnings).filter(Boolean).length : 0;

    if (errorCount === 0 && warningCount === 0) return null;

    return (
        <div className="rounded-md bg-red-50 border border-red-200 p-4">
            <div className="flex">
                <AlertCircle className="h-5 w-5 text-red-400 flex-shrink-0" />
                <div className="ml-3 flex-1 min-h-0 min-w-0">
                    <h3 className="text-sm font-medium text-red-800">
                        {errorCount > 0 && `${errorCount} validation error${errorCount !== 1 ? 's' : ''} found`}
                        {errorCount > 0 && warningCount > 0 && ', '}
                        {warningCount > 0 && `${warningCount} warning${warningCount !== 1 ? 's' : ''}`}
                    </h3>
                    <div className="mt-2 text-sm text-red-700">
                        <ul className="list-disc list-inside space-y-1">
                            {Object.entries(errors).map(([field, error]) => error && (
                                <li key={field}>{error}</li>
                            ))}
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    );
};

// Use export type for isolatedModules
export type { ValidationErrors as ValidationErrorsType };
export {
    ValidatedInput,
    ValidatedTextarea,
    FieldError,
    FieldSuccess,
    ValidationSummary
};