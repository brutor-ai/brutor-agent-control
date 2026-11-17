/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Copy, ExternalLink, Eye, EyeOff, Info, CheckCircle, Globe, Wifi, Radio } from 'lucide-react';

import { env } from '../../../../env.ts';
import { asyncHandler } from '../../../../lib/asyncSafe';
import { errorKind } from '../../../../lib/narrow';

interface ProxyUrlGeneratorProps {
  serverId: string;
  serverName: string;
  gatewayBaseUrl?: string;
}

interface CopyState {
  [key: string]: boolean;
}

const ProxyUrlGenerator: React.FC<ProxyUrlGeneratorProps> = ({
  serverId,
  serverName,
  gatewayBaseUrl = env.BRUTOR_CONTROL_PLANE_PUBLIC_URL
}) => {
  const [copied, setCopied] = useState<CopyState>({});
  const [showUrls, setShowUrls] = useState(false);

  // Generate the proxy URLs for different transport methods
  const proxyUrls = {
    http: `${gatewayBaseUrl}/v1/admin/mcp/transport/${encodeURIComponent(serverId)}`,
    websocket: `${gatewayBaseUrl.replace(/^http/, 'ws')}/v1/admin/mcp/transport/${encodeURIComponent(serverId)}`,
    sse: `${gatewayBaseUrl}/v1/admin/mcp/transport/${encodeURIComponent(serverId)}/sse`
  };

  const copyToClipboard = async (url: string, type: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(prev => ({ ...prev, [type]: true }));
      setTimeout(() => {
        setCopied(prev => ({ ...prev, [type]: false }));
      }, 2000);
    } catch (err) {
      console.error('Failed to copy URL:', errorKind(err));
    }
  };

  const openProxyInfo = () => {
    const infoUrl = `${proxyUrls.http}/info`;
    window.open(infoUrl, '_blank');
  };

  const transportOptions = [
    {
      key: 'http',
      name: 'HTTP Transport',
      url: proxyUrls.http,
      icon: <Globe className="w-4 h-4" />,
      description: 'Standard HTTP POST requests with JSON-RPC 2.0 messages',
      usage: 'Recommended for most MCP client libraries',
      colorClass: 'bg-primary-50 border-sky-border',
      buttonClass: 'bg-primary-100 text-primary-700 hover:bg-blue-200'
    },
    {
      key: 'websocket',
      name: 'WebSocket',
      url: proxyUrls.websocket,
      icon: <Wifi className="w-4 h-4" />,
      description: 'Real-time bidirectional communication via WebSocket',
      usage: 'Best for interactive applications requiring live updates',
      colorClass: 'bg-green-50 border-green-200',
      buttonClass: 'bg-green-100 text-green-700 hover:bg-green-200'
    },
    {
      key: 'sse',
      name: 'Server-Sent Events',
      url: proxyUrls.sse,
      icon: <Radio className="w-4 h-4" />,
      description: 'Server-initiated events and notifications',
      usage: 'For applications that need server push notifications',
      colorClass: 'bg-primary-50 border-primary-200',
      buttonClass: 'bg-primary-100 text-primary-700 hover:bg-purple-200'
    }
  ];

  return (
    <div className="bg-primary-50 border border-sky-border rounded-lg p-4">
      <div className="flex items-start space-x-3">
        <Globe className="w-5 h-5 text-primary mt-0.5 flex-shrink-0" />
        <div className="flex-1 min-w-0 min-h-0">
          <h4 className="font-medium text-primary-900 mb-2">MCP Proxy Endpoints</h4>
          <p className="text-sm text-primary-700 mb-3">
            Connect MCP clients through the gateway to "{serverName}" using these endpoints
          </p>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <button
                onClick={() => setShowUrls(!showUrls)}
                className="flex items-center space-x-1 text-sm text-primary hover:text-primary-800 transition-colors"
              >
                {showUrls ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                <span>{showUrls ? 'Hide' : 'Show'} Proxy URLs</span>
              </button>

              <button
                onClick={openProxyInfo}
                className="flex items-center space-x-1 px-2 py-1 bg-primary-100 text-primary-700 rounded hover:bg-blue-200 text-sm transition-colors"
              >
                <ExternalLink className="w-3 h-3" />
                <span>Endpoint Info</span>
              </button>
            </div>

            {showUrls && (
              <div className="space-y-3">
                {transportOptions.map((transport) => (
                  <div
                    key={transport.key}
                    className={`rounded-lg border p-3 ${transport.colorClass}`}
                  >
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center space-x-2">
                        {transport.icon}
                        <span className="font-medium text-sm text-default">
                          {transport.name}
                        </span>
                      </div>
                      <button
                        onClick={asyncHandler(() => copyToClipboard(transport.url, transport.key), 'click')}
                        className={`flex items-center space-x-1 px-2 py-1 rounded text-xs transition-colors ${transport.buttonClass}`}
                      >
                        {copied[transport.key] ? (
                          <CheckCircle className="w-3 h-3" />
                        ) : (
                          <Copy className="w-3 h-3" />
                        )}
                        <span>{copied[transport.key] ? 'Copied!' : 'Copy'}</span>
                      </button>
                    </div>

                    <div className="bg-surface rounded border p-2 mb-2">
                      <code className="text-xs break-all text-default font-mono">
                        {transport.url}
                      </code>
                    </div>

                    <div className="text-xs space-y-1">
                      <p className="text-secondary font-medium">
                        {transport.description}
                      </p>
                      <p className="text-secondary">
                        {transport.usage}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="mt-4 flex items-start space-x-2">
            <Info className="w-4 h-4 text-primary-500 mt-0.5 flex-shrink-0" />
            <div className="text-xs text-primary">
              <p className="font-medium mb-1">Integration Notes:</p>
              <ul className="space-y-1">
                <li>• HTTP: Use with standard MCP client libraries and tools</li>
                <li>• WebSocket: Connect for real-time bidirectional communication</li>
                <li>• SSE: Subscribe to server-initiated events and notifications</li>
                <li>• All endpoints support OAuth authentication when configured</li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ProxyUrlGenerator;