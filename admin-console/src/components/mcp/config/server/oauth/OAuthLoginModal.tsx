/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * OAuth Information Modal - Capability Discovery Instructions
 * Informs admin how capabilities will be discovered when clients connect
 */

import React from 'react';
import { X, Shield, Info, Sparkles, ArrowRight, CheckCircle } from 'lucide-react';
import { MCPServerConfig } from '../../../../../lib/types.ts';

interface OAuthLoginModalProps {
    server: MCPServerConfig;
    onClose: () => void;
}

const OAuthLoginModal: React.FC<OAuthLoginModalProps> = ({
                                                             server,
                                                             onClose
                                                         }) => {
    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b">
                    <div className="flex items-center space-x-3">
                        <Shield className="w-6 h-6 text-primary-600" />
                        <div>
                            <h2 className="text-xl font-semibold text-default">
                                OAuth Server Capability Discovery
                            </h2>
                            <p className="text-sm text-secondary mt-0.5">
                                {server.name}
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                    >
                        <X className="w-5 h-5 text-muted" />
                    </button>
                </div>

                {/* Content */}
                <div className="p-6 space-y-6">
                    {/* Information Banner */}
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                        <div className="flex items-start space-x-3">
                            <Info className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
                            <div className="flex-1 min-h-0 min-w-0">
                                <p className="font-medium text-blue-900 mb-2">
                                    Automatic Capability Discovery
                                </p>
                                <p className="text-sm text-blue-800">
                                    This OAuth-protected MCP server's capabilities (tools, resources, and prompts)
                                    will be automatically discovered when the first MCP client connects through the gateway.
                                </p>
                            </div>
                        </div>
                    </div>

                    {/* How It Works */}
                    <div>
                        <h3 className="text-lg font-semibold text-default mb-4 flex items-center space-x-2">
                            <Sparkles className="w-5 h-5 text-primary-600" />
                            <span>How It Works</span>
                        </h3>

                        <div className="space-y-4">
                            <div className="flex items-start space-x-3">
                                <div className="flex-shrink-0 w-8 h-8 bg-primary-100 rounded-full flex items-center justify-center">
                                    <span className="text-primary-700 font-semibold text-sm">1</span>
                                </div>
                                <div className="flex-1 min-h-0 min-w-0">
                                    <p className="font-medium text-default mb-1">Client Connects</p>
                                    <p className="text-sm text-secondary">
                                        An MCP client connects to this server through the gateway's proxy endpoint.
                                    </p>
                                </div>
                            </div>

                            <div className="flex items-start space-x-3">
                                <div className="flex-shrink-0 w-8 h-8 bg-primary-100 rounded-full flex items-center justify-center">
                                    <span className="text-primary-700 font-semibold text-sm">2</span>
                                </div>
                                <div className="flex-1 min-h-0 min-w-0">
                                    <p className="font-medium text-default mb-1">OAuth Authentication</p>
                                    <p className="text-sm text-secondary">
                                        The client authenticates with OAuth through the gateway (CORS-safe flow).
                                    </p>
                                </div>
                            </div>

                            <div className="flex items-start space-x-3">
                                <div className="flex-shrink-0 w-8 h-8 bg-primary-100 rounded-full flex items-center justify-center">
                                    <span className="text-primary-700 font-semibold text-sm">3</span>
                                </div>
                                <div className="flex-1 min-h-0 min-w-0">
                                    <p className="font-medium text-default mb-1">Capability Discovery</p>
                                    <p className="text-sm text-secondary">
                                        Gateway automatically queries <code className="text-xs bg-surface-raised px-1 py-0.5 rounded">tools/list</code>,
                                        <code className="text-xs bg-surface-raised px-1 py-0.5 rounded ml-1">resources/list</code>,
                                        and <code className="text-xs bg-surface-raised px-1 py-0.5 rounded ml-1">prompts/list</code>.
                                    </p>
                                </div>
                            </div>

                            <div className="flex items-start space-x-3">
                                <div className="flex-shrink-0 w-8 h-8 bg-green-100 rounded-full flex items-center justify-center">
                                    <CheckCircle className="w-4 h-4 text-green-700" />
                                </div>
                                <div className="flex-1 min-h-0 min-w-0">
                                    <p className="font-medium text-default mb-1">Capabilities Stored</p>
                                    <p className="text-sm text-secondary">
                                        Discovered capabilities are saved to this server configuration for future reference.
                                    </p>
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Benefits */}
                    <div className="bg-green-50 border border-green-200 rounded-lg p-4">
                        <h4 className="font-medium text-green-900 mb-2 flex items-center space-x-2">
                            <CheckCircle className="w-4 h-4" />
                            <span>Why This Approach?</span>
                        </h4>
                        <ul className="text-sm text-green-800 space-y-2">
                            <li className="flex items-start space-x-2">
                                <ArrowRight className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                <span><strong>CORS-Safe:</strong> All OAuth and MCP traffic flows through the gateway</span>
                            </li>
                            <li className="flex items-start space-x-2">
                                <ArrowRight className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                <span><strong>Accurate:</strong> Capabilities reflect the actual authenticated session</span>
                            </li>
                            <li className="flex items-start space-x-2">
                                <ArrowRight className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                <span><strong>Automatic:</strong> No manual admin authentication or testing required</span>
                            </li>
                            <li className="flex items-start space-x-2">
                                <ArrowRight className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                <span><strong>Client-Driven:</strong> Happens naturally during normal client usage</span>
                            </li>
                        </ul>
                    </div>

                    {/* Next Steps */}
                    <div className="bg-surface-alt border border-default rounded-lg p-4">
                        <h4 className="font-medium text-default mb-2">Next Steps</h4>
                        <p className="text-sm text-secondary mb-3">
                            To discover capabilities for this server:
                        </p>
                        <ol className="text-sm text-secondary space-y-2 list-decimal list-inside">
                            <li>Configure your MCP client to connect to the gateway proxy endpoint</li>
                            <li>Ensure OAuth client credentials are properly configured</li>
                            <li>Connect with your MCP client - capabilities will be discovered automatically</li>
                            <li>Return to this admin UI to view the discovered capabilities</li>
                        </ol>
                    </div>
                </div>

                {/* Footer */}
                <div className="flex justify-end space-x-3 p-6 border-t bg-surface-alt">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm font-medium text-white bg-primary-600 border border-transparent rounded-md hover:bg-primary-700"
                    >
                        Got It
                    </button>
                </div>
            </div>
        </div>
    );
};

export default OAuthLoginModal;