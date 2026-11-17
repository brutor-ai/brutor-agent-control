/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Key,
    Plus,
    RefreshCw,
    AlertCircle,
    Loader,
    Search,
    Filter
} from 'lucide-react';
import { useGateway } from '../../../../../hooks/useGateway.ts';
import { useToast } from '../../../../common/Toast.tsx';
import OAuthAppClientRow from './OAuthAppClientRow.tsx';
import OAuthAppClientForm, { type OAuthAppClientPayload } from './OAuthAppClientForm.tsx';
import GenericDeleteConfirmationModal from '../../../../common/GenericDeleteConfirmationModal.tsx';
import InfoBanner from '../../../../common/InfoBanner.tsx';
import { OAuthAppClient } from '../../../../../lib/types.ts';
import { errorKind, errorMessage } from '../../../../../lib/narrow';
import { inUseRefusalOf, type InUseRefusal } from '../../../../../lib/inUseRefusal';
import { asyncHandler, runBackground } from '../../../../../lib/asyncSafe';

interface IdP {
    id: string;
    name: string;
    enabled: boolean;
}

const OAuthAppClientTab: React.FC = () => {
    const {
        fetchOAuthAppClients,
        createOAuthAppClient,
        updateOAuthAppClient,
        deleteOAuthAppClient,
        fetchIdPConfigs
    } = useGateway();

    const toast = useToast();

    const [clients, setClients] = useState<OAuthAppClient[]>([]);
    const [idps, setIdps] = useState<IdP[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [filterAppType, setFilterAppType] = useState<string>('all');
    const [filterClientType, setFilterClientType] = useState<'all' | 'confidential' | 'public' | 'public_with_secret'>('all');
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [editingClient, setEditingClient] = useState<OAuthAppClient | null>(null);

    // Delete confirmation modal state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        clientId: string;
        clientName: string;
        clientInfo: any;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [deleteInUse, setDeleteInUse] = useState<InUseRefusal | null>(null);

    useEffect(() => {
        runBackground(loadData(), 'data');
    }, []);

    const loadData = async () => {
        setLoading(true);
        setError('');
        try {
            const [clientsResponse, idpsResponse] = await Promise.all([
                fetchOAuthAppClients(),
                fetchIdPConfigs({ enabled_only: false })
            ]);

            setClients(clientsResponse.clients || []);
            setIdps(Object.values(idpsResponse.idps).map((idp: any) => ({
                id: idp.id,
                name: idp.name,
                enabled: idp.enabled
            })));
        } catch (err) {
            console.error('Failed to load data:', errorKind(err));
            setError(errorMessage(err, 'Failed to load OAuth app clients'));
            toast.error('Failed to Load OAuth Clients', errorMessage(err, 'Unable to load OAuth app clients'));
        } finally {
            setLoading(false);
        }
    };

    // Failures propagate to the form, which is what surfaces them to the user.
    const handleCreateOrUpdateClient = async (clientData: OAuthAppClientPayload) => {
        if (editingClient) {
            // The application type, the identity provider and the client
            // type are fixed at creation.
            const { app_type, idp_id, client_type, ...update } = clientData;
            await updateOAuthAppClient(editingClient.id, update);
            toast.success('Client Updated', `${clientData.name} has been updated successfully.`);
        } else {
            await createOAuthAppClient(clientData);
            toast.success('Client Created', `${clientData.name} has been created successfully.`);
        }

        setShowCreateModal(false);
        setEditingClient(null);
        await loadData();
    };

    const handleEdit = (client: OAuthAppClient) => {
        setEditingClient(client);
        setShowCreateModal(true);
    };

    const handleDeleteClient = async (clientId: string) => {
        const client = clients.find(c => c.id === clientId);

        if (!client) {
            toast.error('Client Not Found', 'Unable to locate the OAuth client to delete.');
            return;
        }

        // Show delete confirmation modal
        setDeleteError(null);
        setDeleteInUse(null);
        setDeleteConfirmation({
            clientId: client.id,
            clientName: client.name,
            clientInfo: {
                id: client.id,
                app_type: client.app_type,
                client_type: client.client_type,
                client_id: client.client_id
            }
        });
    };

    const confirmDeleteClient = async () => {
        if (!deleteConfirmation) return;

        const { clientId, clientName } = deleteConfirmation;
        setIsDeleting(true);

        try {
            await deleteOAuthAppClient(clientId);
            await loadData();

            toast.success('Client Deleted', `${clientName} has been deleted successfully.`);
            setDeleteConfirmation(null);
        } catch (err) {
            console.error('Failed to delete client:', errorKind(err));
            setDeleteError(errorMessage(err, `Unable to delete ${clientName}.`));
            setDeleteInUse(inUseRefusalOf(err));
        } finally {
            setIsDeleting(false);
        }
    };

    const getIdPName = (idpId: string): string => {
        const idp = idps.find(i => i.id === idpId);
        return idp ? idp.name : idpId;
    };

    const filteredClients = clients.filter(client => {
        const matchesSearch =
            client.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            client.client_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
            getIdPName(client.idp_id).toLowerCase().includes(searchTerm.toLowerCase());

        const matchesAppType = filterAppType === 'all' || client.app_type === filterAppType;
        const matchesClientType = filterClientType === 'all' || client.client_type === filterClientType;

        return matchesSearch && matchesAppType && matchesClientType;
    });

    if (loading) {
        return (
            <div className="p-6">
                <div className="text-center py-12">
                    <Loader className="w-12 h-12 animate-spin text-primary mx-auto mb-4" />
                    <p className="text-secondary">Loading OAuth App Clients...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6">
            {/* Header */}
            <div className="mb-6">
                <div className="flex justify-between items-center mb-4">
                    <div>
                        <h1 className="text-2xl font-bold text-default mb-2">OAuth App Client Management</h1>
                        <p className="text-secondary">
                            Manage OAuth clients for different application types (web, mobile, desktop, etc.)
                        </p>
                    </div>
                    <div className="flex items-center space-x-2">
                        <button
                            onClick={asyncHandler(loadData, 'click')}
                            className="flex items-center space-x-2 px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        >
                            <RefreshCw className="w-4 h-4" />
                            <span>Refresh</span>
                        </button>
                        <button
                            onClick={() => {
                                setEditingClient(null);
                                setShowCreateModal(true);
                            }}
                            className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                        >
                            <Plus className="w-4 h-4" />
                            <span>Create OAuth App Client</span>
                        </button>
                    </div>
                </div>

                {/* Search and Filters */}
                <div className="flex items-center space-x-4 mb-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder="Search by name, client ID, or IdP..."
                            className="w-full pl-10 pr-4 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        />
                    </div>
                    <div className="flex items-center space-x-2">
                        <Filter className="w-4 h-4 text-muted" />
                        <select
                            value={filterAppType}
                            onChange={(e) => setFilterAppType(e.target.value)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All App Types</option>
                            <option value="webapp">Web App</option>
                            <option value="mobile">Mobile</option>
                            <option value="desktop">Desktop</option>
                            <option value="cli">CLI</option>
                            <option value="service">Service</option>
                        </select>
                        <select
                            value={filterClientType}
                            onChange={(e) => setFilterClientType(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Client Types</option>
                            <option value="confidential">Confidential</option>
                            <option value="public">Public</option>
                            <option value="public_with_secret">Public + Secret</option>
                        </select>
                    </div>
                </div>
            </div>

            {/* Error Display */}
            {error && (
                <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-md">
                    <div className="flex items-center space-x-2">
                        <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
                        <span className="text-red-700">{error}</span>
                        <button
                            onClick={() => setError('')}
                            className="ml-auto text-red-600 hover:text-red-800"
                        >
                            ×
                        </button>
                    </div>
                </div>
            )}

            {/* Info Banner */}
            <InfoBanner title="About OAuth App Clients">
                <p>
                    OAuth app clients represent different applications (web, mobile, desktop) that access MCP servers.
                    Create clients here, then link them to MCP servers via Server Configs.
                </p>
            </InfoBanner>

            {/* Clients Grid */}
            {filteredClients.length === 0 ? (
                <div className="text-center py-12 bg-surface rounded-lg border">
                    <Key className="w-12 h-12 text-muted mx-auto mb-4" />
                    <h3 className="text-lg font-medium text-default mb-2">
                        {searchTerm || filterAppType !== 'all' || filterClientType !== 'all'
                            ? 'No clients found'
                            : 'No OAuth app clients created'}
                    </h3>
                    <p className="text-secondary mb-4">
                        {searchTerm || filterAppType !== 'all' || filterClientType !== 'all'
                            ? 'Try adjusting your search or filters'
                            : 'Create your first OAuth app client to get started'}
                    </p>
                </div>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {filteredClients.map((client) => (
                        <OAuthAppClientRow
                            key={client.id}
                            client={client}
                            idpName={getIdPName(client.idp_id)}
                            onEdit={handleEdit}
                            onDelete={asyncHandler(handleDeleteClient, 'delete')}
                        />
                    ))}
                </div>
            )}

            {/* Create/Edit Modal */}
            {showCreateModal && (
                <OAuthAppClientForm
                    client={editingClient}
                    idps={idps}
                    onSave={handleCreateOrUpdateClient}
                    onCancel={() => {
                        setShowCreateModal(false);
                        setEditingClient(null);
                    }}
                />
            )}

            {/* Delete Confirmation Modal */}
            {deleteConfirmation && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => {
                        if (!isDeleting) {
                            setDeleteConfirmation(null);
                            setDeleteError(null);
                            setDeleteInUse(null);
                        }
                    }}
                    onConfirm={asyncHandler(confirmDeleteClient, 'confirm')}
                    itemType="client"
                    itemName={deleteConfirmation.clientName}
                    itemInfo={{
                        id: deleteConfirmation.clientInfo.id,
                        description: `${deleteConfirmation.clientInfo.app_type} (${deleteConfirmation.clientInfo.client_type})`,
                        enabled: true,
                    }}
                    customWarning="This will remove the client from all server configurations."
                    customNote={`Client ID: ${deleteConfirmation.clientInfo.client_id}`}
                    isDeleting={isDeleting}
                    errorMessage={deleteError}
                    inUse={deleteInUse}
                />
            )}
        </div>
    );
};

export default OAuthAppClientTab;