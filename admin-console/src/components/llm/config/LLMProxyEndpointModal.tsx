/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Link, Copy, Check } from 'lucide-react';
import { asyncHandler } from '../../../lib/asyncSafe';
import { errorKind } from '../../../lib/narrow';

interface LLMProxyEndpointModalProps {
    isOpen: boolean;
    onClose: () => void;
    proxyUrl: string;
}

/**
 * LLM Proxy Endpoint Modal - Shows proxy endpoint information and usage examples
 */
const LLMProxyEndpointModal: React.FC<LLMProxyEndpointModalProps> = ({
                                                                         isOpen,
                                                                         onClose,
                                                                         proxyUrl
                                                                     }) => {
    const [copied, setCopied] = useState(false);

    const handleCopyEndpoint = async () => {
        try {
            await navigator.clipboard.writeText(proxyUrl);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (error) {
            console.error('Failed to copy:', errorKind(error));
        }
    };

    // Don't render if modal is closed
    if (!isOpen) {
        return null;
    }

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full mx-4 max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div className="flex items-center space-x-2">
                        <Link className="w-5 h-5 text-primary-600" />
                        <h2 className="text-xl font-semibold text-default">
                            LLM Proxy Endpoint
                        </h2>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Content */}
                <div className="scroll-region px-6 py-6">
                    <div className="space-y-6">
                        {/* Endpoint URL */}
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-2">
                                Proxy URL
                            </label>
                            <div className="flex items-center space-x-2">
                                <div className="flex-1 px-4 py-3 bg-blue-50 border border-blue-200 rounded-lg min-h-0 min-w-0">
                                    <code className="text-sm text-blue-900 break-all">
                                        {proxyUrl}
                                    </code>
                                </div>
                                <button
                                    onClick={asyncHandler(handleCopyEndpoint, 'click')}
                                    className="px-4 py-3 bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors flex items-center space-x-2"
                                    title="Copy to clipboard"
                                >
                                    {copied ? (
                                        <>
                                            <Check className="w-4 h-4 text-green-600" />
                                            <span className="text-sm text-green-600">Copied!</span>
                                        </>
                                    ) : (
                                        <>
                                            <Copy className="w-4 h-4 text-secondary" />
                                            <span className="text-sm text-secondary">Copy</span>
                                        </>
                                    )}
                                </button>
                            </div>
                        </div>

                        {/* Usage Instructions */}
                        <div className="bg-surface-alt border border-default rounded-lg p-4 space-y-4">
                            <div>
                                <h3 className="text-sm font-semibold text-default mb-2">
                                    How to Use This Endpoint
                                </h3>
                                <p className="text-sm text-secondary">
                                    Use this URL as your base URL in OpenAI-compatible chat clients and applications.
                                    You'll need an API key to authenticate (manage API keys in the Groups section).
                                </p>
                            </div>

                            <div className="space-y-3">
                                <div>
                                    <h4 className="text-sm font-medium text-default mb-1">
                                        Example with OpenAI Python SDK:
                                    </h4>
                                    <div className="bg-gray-900 rounded-md p-3 overflow-x-auto">
                                        <code className="text-xs text-green-400 font-mono">
                                            <div>from openai import OpenAI</div>
                                            <div className="mt-1">client = OpenAI(</div>
                                            <div className="ml-4">base_url="{proxyUrl}",</div>
                                            <div className="ml-4">api_key="your-api-key"</div>
                                            <div>)</div>
                                        </code>
                                    </div>
                                </div>

                                <div>
                                    <h4 className="text-sm font-medium text-default mb-1">
                                        Example with cURL:
                                    </h4>
                                    <div className="bg-gray-900 rounded-md p-3 overflow-x-auto">
                                        <code className="text-xs text-green-400 font-mono whitespace-pre">
                                            {`curl ${proxyUrl}/chat/completions \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "claude-sonnet-5-5",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`}
                                        </code>
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Features */}
                        <div>
                            <h3 className="text-sm font-semibold text-default mb-2">
                                Features
                            </h3>
                            <ul className="space-y-2 text-sm text-secondary">
                                <li className="flex items-start space-x-2">
                                    <span className="text-green-600 mt-0.5">✓</span>
                                    <span>OpenAI-compatible API format</span>
                                </li>
                                <li className="flex items-start space-x-2">
                                    <span className="text-green-600 mt-0.5">✓</span>
                                    <span>Automatic model routing and load balancing</span>
                                </li>
                                <li className="flex items-start space-x-2">
                                    <span className="text-green-600 mt-0.5">✓</span>
                                    <span>Built-in authentication with API keys</span>
                                </li>
                                <li className="flex items-start space-x-2">
                                    <span className="text-green-600 mt-0.5">✓</span>
                                    <span>Cost tracking and usage analytics</span>
                                </li>
                                <li className="flex items-start space-x-2">
                                    <span className="text-green-600 mt-0.5">✓</span>
                                    <span>Group-based access control</span>
                                </li>
                            </ul>
                        </div>

                        {/* API Keys Info */}
                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                            <div className="flex items-start space-x-2">
                                <div className="flex-1 min-h-0 min-w-0">
                                    <p className="text-sm font-medium text-blue-900">
                                        Need API Keys?
                                    </p>
                                    <p className="text-sm text-blue-700 mt-1">
                                        API keys are managed at the Resource Group level. Go to the <strong>Groups</strong> section,
                                        select a group, and navigate to the <strong>API Keys</strong> tab to create and manage keys.
                                    </p>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                {/* Footer */}
                <div className="px-6 py-4 border-t border-default bg-surface-alt rounded-b-lg flex justify-end">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default LLMProxyEndpointModal;