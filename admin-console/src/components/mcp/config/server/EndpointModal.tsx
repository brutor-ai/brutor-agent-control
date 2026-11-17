/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Copy, CheckCircle } from 'lucide-react';
import InfoBanner from '../../../common/InfoBanner';
import { asyncHandler } from '../../../../lib/asyncSafe';
import { errorKind } from '../../../../lib/narrow';

interface EndpointModalProps {
    serverId: string;
    serverName: string;
    gatewayBaseUrl: string;
    onClose: () => void;
}

/**
 * EndpointModal - Displays MCP proxy endpoint for a server
 *
 * Shows the gateway proxy URL that clients should use to connect
 * to the MCP server through the gateway.
 */
const EndpointModal: React.FC<EndpointModalProps> = ({
                                                         serverId,
                                                         serverName,
                                                         gatewayBaseUrl,
                                                         onClose
                                                     }) => {
    const [copied, setCopied] = useState(false);
    const proxyUrl = `${gatewayBaseUrl}/v1/proxy/mcp/${encodeURIComponent(serverId)}`;

    const copyToClipboard = async () => {
        try {
            await navigator.clipboard.writeText(proxyUrl);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (err) {
            console.error('Failed to copy URL:', errorKind(err));
        }
    };

    return (
        <div
            className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
            onClick={onClose}
        >
            <div
                className="bg-surface rounded-lg shadow-xl max-w-2xl w-full"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-6 py-4 border-b">
                    <h3 className="text-lg font-semibold text-default">
                        MCP Proxy Endpoint
                    </h3>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                    >
                        <X className="w-5 h-5 text-muted" />
                    </button>
                </div>

                <div className="px-6 py-4">
                    <div className="mb-4">
                        <p className="text-sm text-secondary mb-2">
                            Server: <span className="font-medium text-default">{serverName}</span>
                        </p>
                        <p className="text-sm text-secondary">
                            Use this endpoint to connect MCP clients through the gateway:
                        </p>
                    </div>

                    <div className="bg-surface-alt border border-default rounded-lg p-4 mb-4">
                        <div className="flex items-center justify-between mb-2">
                            <span className="text-xs font-medium text-secondary uppercase">Proxy URL</span>
                            <button
                                onClick={asyncHandler(copyToClipboard, 'click')}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                            >
                                {copied ? (
                                    <>
                                        <CheckCircle className="w-4 h-4" />
                                        <span>Copied!</span>
                                    </>
                                ) : (
                                    <>
                                        <Copy className="w-4 h-4" />
                                        <span>Copy</span>
                                    </>
                                )}
                            </button>
                        </div>
                        <code className="block text-sm break-all text-default font-mono bg-surface border border-default rounded p-3">
                            {proxyUrl}
                        </code>
                    </div>

                    <InfoBanner title="Integration Notes" className="mb-0">
                        <ul className="space-y-1">
                            <li>• Use this URL in your MCP client configuration</li>
                            <li>• The gateway handles authentication and routing</li>
                            <li>• Supports standard MCP protocol over HTTP</li>
                        </ul>
                    </InfoBanner>
                </div>

                <div className="flex justify-end px-6 py-4 border-t bg-surface-alt">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default EndpointModal;