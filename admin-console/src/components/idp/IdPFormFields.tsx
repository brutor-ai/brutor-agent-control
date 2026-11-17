/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The labelled text field and checkbox of the identity provider form, shared
 * by the form and by the fields a protocol contributes to it. Defined outside
 * the form so that a render of the form does not remount them.
 */

import React from 'react';
import { AlertCircle } from 'lucide-react';

interface TextFieldProps {
    label: string;
    name: string;
    value: string | number;
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
    errors?: Record<string, string>;
    required?: boolean;
    helpText?: string;
    placeholder?: string;
    type?: string;
}

export const TextField: React.FC<TextFieldProps> = ({
                                                 label,
                                                 name,
                                                 value,
                                                 onChange,
                                                 errors = {},
                                                 required = false,
                                                 helpText = '',
                                                 placeholder = '',
                                                 type = 'text'
                                             }) => (
    <div className="mb-4">
        <label className="block text-sm font-medium text-secondary mb-1">
            {label} {required && <span className="text-red-500">*</span>}
        </label>
        <input
            type={type}
            name={name}
            value={value}
            onChange={onChange}
            required={required}
            placeholder={placeholder}
            className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                errors[name] ? 'border-red-500' : 'border-strong'
            }`}
        />
        {helpText && (
            <p className="text-xs text-muted mt-1">{helpText}</p>
        )}
        {errors[name] && (
            <div className="flex items-center gap-1 mt-1 text-red-500 text-sm">
                <AlertCircle size={14} />
                <span>{errors[name]}</span>
            </div>
        )}
    </div>
);

interface CheckboxFieldProps {
    label: string;
    name: string;
    checked: boolean;
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
    helpText?: string;
}

export const CheckboxField: React.FC<CheckboxFieldProps> = ({
                                                         label,
                                                         name,
                                                         checked,
                                                         onChange,
                                                         helpText = ''
                                                     }) => (
    <div className="mb-4">
        <label className="flex items-center">
            <input
                type="checkbox"
                name={name}
                checked={checked}
                onChange={onChange}
                className="mr-2"
            />
            <span className="text-sm font-medium text-secondary">{label}</span>
        </label>
        {helpText && (
            <p className="text-xs text-muted mt-1 ml-6">{helpText}</p>
        )}
    </div>
);
