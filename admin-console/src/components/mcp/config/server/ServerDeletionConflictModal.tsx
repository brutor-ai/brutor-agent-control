/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { X, AlertTriangle, ExternalLink } from 'lucide-react';

import type { InUseRefusal } from '../../../../lib/inUseRefusal';
import InUseList from '../../../common/InUseList';

interface ServerDeletionConflictModalProps {
    isOpen: boolean;
    onClose: () => void;
    serverName: string;
    /** The refusal as the Control Plane words it. */
    errorDetail: string;
    /** What uses the server, when the refusal was a 409 that lists it (`detail.used_by`). */
    inUse: InUseRefusal | null;
    onNavigateToVirtualServers?: () => void;
}

/**
 * Why a delete of an MCP server was refused. A 409 that lists the virtual
 * servers using it (`detail.used_by.virtual_servers`) is shown as that
 * list, with the way to take the server out of them; any other refusal is
 * shown as the Control Plane words it.
 */
const ServerDeletionConflictModal: React.FC<ServerDeletionConflictModalProps> = ({
    isOpen,
    onClose,
    serverName,
    errorDetail,
    inUse,
    onNavigateToVirtualServers,
}) => {
    if (!isOpen) return null;

    const virtualServers = inUse?.groups.find((group) => group.kind === 'virtual_servers')?.rows ?? [];
    const otherUses = inUse?.groups.filter((group) => group.kind !== 'virtual_servers') ?? [];
    const usedByVirtualServers = virtualServers.length > 0;

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b">
                    <div className="flex items-center space-x-3">
                        <AlertTriangle className="w-6 h-6 text-amber-600" />
                        <div>
                            <h2 className="text-xl font-semibold text-default">
                                Cannot Delete Server
                            </h2>
                            {usedByVirtualServers && (
                                <p className="text-sm text-secondary mt-0.5">
                                    Server is in use by virtual servers
                                </p>
                            )}
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
                    {/* Main Message: the virtual servers the answer lists, or the answer as it is worded. */}
                    {usedByVirtualServers ? (
                        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
                            <p className="text-amber-900 font-medium mb-2">
                                The server "{serverName}" cannot be deleted because it is currently
                                being used by {virtualServers.length} virtual server{virtualServers.length !== 1 ? 's' : ''}.
                            </p>
                            <p className="text-sm text-amber-800">
                                Deleting this server would break these virtual servers.
                            </p>
                        </div>
                    ) : (
                        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
                            <p className="text-amber-900 font-medium mb-2">
                                The server "{serverName}" was not deleted.
                            </p>
                            <p className="text-sm text-amber-800 whitespace-pre-wrap break-words" data-testid="deletion-refusal">
                                {errorDetail}
                            </p>
                        </div>
                    )}

                    {/* Virtual Servers List */}
                    {virtualServers.length > 0 && (
                        <div>
                            <h3 className="text-lg font-semibold text-default mb-3">
                                Virtual Servers Using This Server
                            </h3>
                            <div className="space-y-3">
                                {virtualServers.map((vs) => (
                                    <div
                                        key={vs.id}
                                        className="bg-surface-alt border border-default rounded-lg p-4"
                                    >
                                        <div className="font-medium text-default mb-2">
                                            {vs.name}
                                        </div>
                                        <div className="text-sm text-secondary mb-2">
                                            ID: <code className="text-xs bg-surface-inset px-1 py-0.5 rounded">{vs.id}</code>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {otherUses.length > 0 && <InUseList groups={otherUses} />}

                    {/* Instructions and the answer in full: only where the answer listed virtual servers. */}
                    {usedByVirtualServers && (
                    <>
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                        <h4 className="font-medium text-blue-900 mb-2">
                            To delete this server:
                        </h4>
                        <ol className="text-sm text-blue-800 space-y-2 list-decimal list-inside">
                            <li>Go to the Virtual Servers tab</li>
                            <li>Edit each virtual server listed above</li>
                            <li>Remove the capabilities that reference this server</li>
                            <li>Save each virtual server</li>
                            <li>Return here and try deleting this server again</li>
                        </ol>
                    </div>

                    <details className="text-sm">
                        <summary className="cursor-pointer text-secondary hover:text-default">
                            Show the Control Plane's answer
                        </summary>
                        <pre className="mt-2 p-3 bg-surface-raised rounded text-xs overflow-x-auto">
                            {errorDetail}
                        </pre>
                    </details>
                    </>
                    )}
                </div>

                {/* Footer */}
                <div className="flex justify-between items-center space-x-3 p-6 border-t bg-surface-alt">
                    {usedByVirtualServers && onNavigateToVirtualServers && (
                        <button
                            onClick={() => {
                                onNavigateToVirtualServers();
                                onClose();
                            }}
                            className="px-4 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-md hover:bg-primary-100 flex items-center space-x-2"
                        >
                            <ExternalLink className="w-4 h-4" />
                            <span>Go to Virtual Servers</span>
                        </button>
                    )}
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm font-medium text-white bg-primary-600 border border-transparent rounded-md hover:bg-primary-700 ml-auto"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default ServerDeletionConflictModal;