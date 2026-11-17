/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    Shield,
    Plus,
    Trash2,
    RefreshCw,
    Eye,
    TestTube,
    AlertCircle,
    Loader,
    Search,
    Filter,
    Edit,
    X
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import IdPServerForm from './IdPServerForm';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import { IdPConfig } from '../../lib/types';
import { errorKind, errorMessage } from '../../lib/narrow';
import { inUseRefusalOf, type InUseRefusal } from '../../lib/inUseRefusal';
import { refusalMessage } from '../../lib/serverRefusal';
import { unavailableProtocol, unavailableProtocolReason } from './protocolAvailability';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

const IdPServersTab: React.FC = () => {
    const {
        fetchIdPConfigs,
        fetchIdPConfig,
        deleteIdPConfig,
        discoverIdPEndpoints,
        testExistingIdP
    } = useGateway();

    const toast = useToast();

    // A stored provider of a protocol this console does not have (any but
    // OIDC) is listed and can be looked at. The control plane refuses to
    // test, discover, change or delete it, so those are not offered: each
    // says why instead.
    const whyUnavailable = (idp: IdPConfig | null): string | null => {
        const protocol = unavailableProtocol(idp);
        return protocol ? unavailableProtocolReason(protocol) : null;
    };

    const [idps, setIdps] = useState<IdPConfig[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [filterEnabled, setFilterEnabled] = useState<'all' | 'enabled' | 'disabled'>('all');
    const [selectedIdP, setSelectedIdP] = useState<IdPConfig | null>(null);
    const [showIdPDetails, setShowIdPDetails] = useState(false);
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [showEditModal, setShowEditModal] = useState(false);
    const [loadingDetails, setLoadingDetails] = useState(false);
    const [testingConnection, setTestingConnection] = useState(false);
    const [discovering, setDiscovering] = useState(false);

    // Delete confirmation modal state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        idpId: string;
        idpName: string;
        idpInfo: any;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [deleteInUse, setDeleteInUse] = useState<InUseRefusal | null>(null);

    useEffect(() => {
        runBackground(loadIdPs(), 'id ps');
    }, []);

    const loadIdPs = async () => {
        setLoading(true);
        setError('');
        try {
            const response = await fetchIdPConfigs();
            const idpList = Object.values(response.idps) as IdPConfig[];
            setIdps(idpList);
        } catch (err) {
            console.error('Failed to fetch IdPs:', errorKind(err));
            setError(errorMessage(err, 'Failed to load Identity Providers'));
            toast.error('Failed to Load IdPs', errorMessage(err, 'Unable to load Identity Providers'));
        } finally {
            setLoading(false);
        }
    };

    const loadIdPDetails = async (idpId: string) => {
        setLoadingDetails(true);
        try {
            const response = await fetchIdPConfig(idpId);
            // The Control Plane declares this payload `Dict[str, Any]`, so the
            // generated schema carries no fields. Narrowed at the boundary.
            setSelectedIdP(response.idp as unknown as IdPConfig);
            setShowIdPDetails(true);
        } catch (err) {
            console.error('Failed to fetch IdP details:', errorKind(err));
            setError(errorMessage(err, 'Failed to load IdP details'));
            toast.error('Failed to Load IdP Details', errorMessage(err, 'Unable to load Identity Provider details'));
        } finally {
            setLoadingDetails(false);
        }
    };

    const handleDeleteIdP = async (idpId: string) => {
        const idp = idps.find(i => i.id === idpId);
        if (!idp) {
            toast.error('IdP Not Found', 'Unable to locate the Identity Provider to delete.');
            return;
        }

        // Show delete confirmation modal
        setDeleteError(null);
        setDeleteInUse(null);
        setDeleteConfirmation({
            idpId: idp.id,
            idpName: idp.name,
            idpInfo: {
                id: idp.id,
                issuer: idp.issuer,
                enabled: idp.enabled
            }
        });
    };

    const confirmDeleteIdP = async () => {
        if (!deleteConfirmation) return;

        const { idpId, idpName } = deleteConfirmation;

        setIsDeleting(true);

        try {
            await deleteIdPConfig(idpId);
            await loadIdPs();

            if (selectedIdP?.id === idpId) {
                setShowIdPDetails(false);
                setSelectedIdP(null);
            }

            toast.success('Identity Provider Deleted', `${idpName} has been deleted successfully.`);
            setDeleteConfirmation(null);
        } catch (err) {
            console.error('Failed to delete IdP:', errorKind(err));
            setDeleteError(refusalMessage(err, `Unable to delete ${idpName}.`));
            setDeleteInUse(inUseRefusalOf(err));
        } finally {
            setIsDeleting(false);
        }
    };

    const handleTestConnection = async (idpId: string) => {
        setTestingConnection(true);
        try {
            const result = await testExistingIdP(idpId);
            if (result.connected) {
                toast.success('Connection Test Successful', 'Identity Provider is reachable and responding.');
            } else {
                toast.error('Connection Test Failed', result.error || 'Unable to connect to Identity Provider.');
            }
        } catch (err) {
            toast.error('Connection Test Failed', refusalMessage(err, 'An error occurred while testing the connection.'));
        } finally {
            setTestingConnection(false);
        }
    };

    const handleDiscover = async (idpId: string) => {
        setDiscovering(true);
        try {
            const result = await discoverIdPEndpoints(idpId, true);
            if (result.discovery_successful) {
                toast.success('Discovery Successful', 'OAuth endpoints have been discovered and updated.');
                await loadIdPs();
                if (selectedIdP?.id === idpId) {
                    await loadIdPDetails(idpId);
                }
            } else {
                toast.error('Discovery Failed', result.error || 'Unable to discover OAuth endpoints.');
            }
        } catch (err) {
            toast.error('Discovery Failed', refusalMessage(err, 'An error occurred during endpoint discovery.'));
        } finally {
            setDiscovering(false);
        }
    };

    const handleEditIdP = async (idp: IdPConfig) => {
        // Load full details before editing
        setLoadingDetails(true);
        try {
            const response = await fetchIdPConfig(idp.id);
            // The Control Plane declares this payload `Dict[str, Any]`, so the
            // generated schema carries no fields. Narrowed at the boundary.
            setSelectedIdP(response.idp as unknown as IdPConfig);
            setShowEditModal(true);
        } catch (err) {
            console.error('Failed to fetch IdP details for editing:', errorKind(err));
            toast.error('Failed to Load IdP', errorMessage(err, 'Unable to load Identity Provider for editing.'));
        } finally {
            setLoadingDetails(false);
        }
    };

    const handleIdPSaved = async () => {
        setShowCreateModal(false);
        setShowEditModal(false);
        setSelectedIdP(null);
        await loadIdPs();
        toast.success(
            'Identity Provider Saved',
            'Identity Provider configuration has been saved successfully.'
        );
    };

    const handleIdPCancelled = () => {
        setShowCreateModal(false);
        setShowEditModal(false);
        setSelectedIdP(null);
    };

    const filteredIdPs = idps.filter(idp => {
        const matchesSearch = !searchTerm ||
            idp.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            idp.id.toLowerCase().includes(searchTerm.toLowerCase()) ||
            (idp.issuer && idp.issuer.toLowerCase().includes(searchTerm.toLowerCase()));

        const matchesEnabled = filterEnabled === 'all' ||
            (filterEnabled === 'enabled' && idp.enabled) ||
            (filterEnabled === 'disabled' && !idp.enabled);

        return matchesSearch && matchesEnabled;
    });

    // Helper function to remove issuer prefix from endpoint URLs
    const shortenEndpoint = (endpoint: string | undefined, issuer: string | undefined): string => {
        if (!endpoint || !issuer) return endpoint || '';

        // Remove trailing slash from issuer for comparison
        const issuerBase = issuer.replace(/\/$/, '');

        // If endpoint starts with issuer, return just the path
        if (endpoint.startsWith(issuerBase)) {
            return endpoint.substring(issuerBase.length);
        }

        return endpoint;
    };

    return (
        <div className="h-full app-column bg-surface-alt">
            {/* Header */}
            <div className="bg-surface border-b border-default px-6 py-4">
                <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center space-x-3">
                        <Shield className="w-6 h-6 text-primary-600" />
                        <div>
                            <h2 className="text-xl font-semibold text-default">Identity Providers</h2>
                            <p className="text-sm text-secondary mt-0.5">
                                Configure OAuth 2.0 / OIDC identity providers for MCP server authentication and agent workload identity
                            </p>
                        </div>
                    </div>
                    <div className="flex items-center space-x-2">
                        <button
                            onClick={asyncHandler(loadIdPs, 'click')}
                            disabled={loading}
                            className="px-3 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                            <span>Refresh</span>
                        </button>
                        <button
                            onClick={() => setShowCreateModal(true)}
                            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 border border-transparent rounded-md hover:bg-primary-700 flex items-center space-x-2"
                        >
                            <Plus className="w-4 h-4" />
                            <span>Add Identity Provider</span>
                        </button>
                    </div>
                </div>

                {/* Filters */}
                <div className="flex items-center space-x-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                        <input
                            type="text"
                            placeholder="Search by name, ID, or issuer..."
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            className="w-full pl-10 pr-4 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                        />
                    </div>
                    <div className="flex items-center space-x-2">
                        <Filter className="w-4 h-4 text-muted" />
                        <select
                            value={filterEnabled}
                            onChange={(e) => setFilterEnabled(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-500 text-sm"
                        >
                            <option value="all">All Status</option>
                            <option value="enabled">Enabled</option>
                            <option value="disabled">Disabled</option>
                        </select>
                    </div>
                </div>
            </div>

            {/* Error Display */}
            {error && (
                <div className="mx-6 mt-4">
                    <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start space-x-3">
                        <AlertCircle className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                        <div className="flex-1 min-h-0 min-w-0">
                            <h3 className="font-medium text-red-900 mb-1">Error</h3>
                            <p className="text-sm text-red-800">{error}</p>
                        </div>
                        <button
                            onClick={() => setError('')}
                            className="text-red-600 hover:text-red-800"
                        >
                            <X className="w-5 h-5" />
                        </button>
                    </div>
                </div>
            )}

            {/* Content */}
            <div className="scroll-region p-6">
                {loading ? (
                    <div className="flex items-center justify-center h-64">
                        <div className="text-center">
                            <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-4" />
                            <p className="text-secondary">Loading Identity Providers...</p>
                        </div>
                    </div>
                ) : filteredIdPs.length === 0 ? (
                    <div className="text-center py-12">
                        <Shield className="w-12 h-12 text-muted mx-auto mb-4" />
                        <h3 className="text-lg font-medium text-default mb-2">
                            {idps.length === 0 ? 'No Identity Providers configured' : 'No Identity Providers match your filters'}
                        </h3>
                        <p className="text-secondary mb-4">
                            {idps.length === 0
                                ? 'Get started by adding your first Identity Provider.'
                                : 'Try adjusting your search or filter criteria.'}
                        </p>
                        {idps.length === 0 && (
                            <button
                                onClick={() => setShowCreateModal(true)}
                                className="px-4 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded-md transition-colors flex items-center space-x-2 mx-auto"
                            >
                                <Plus className="w-4 h-4" />
                                <span>Add First Identity Provider</span>
                            </button>
                        )}
                    </div>
                ) : (
                    <div className="bg-surface rounded-lg border border-default overflow-hidden">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                            <tr>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Server
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Issuer
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                                    End-user Login
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                                    Get Access Token
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                                    Get End-user Info
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                                    Protocol
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Type
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Status
                                </th>
                                <th scope="col" className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                                    Actions
                                </th>
                            </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                            {filteredIdPs.map((idp) => {
                                const unavailable = whyUnavailable(idp);
                                return (
                                <tr key={idp.id} className="hover:bg-surface-alt">
                                    {/* Server Column */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-start space-x-3">
                                            <Shield className="w-5 h-5 text-primary-600 mt-0.5 flex-shrink-0" />
                                            <div>
                                                <div className="font-medium text-default">{idp.name}</div>
                                                <div className="text-xs text-muted mt-0.5">
                                                    <code className="bg-surface-raised px-1 py-0.5 rounded">{idp.id}</code>
                                                </div>
                                                {idp.supports_pkce && (
                                                    <div className="mt-1">
                                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-800">
                                                                PKCE Support
                                                            </span>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* Issuer Column */}
                                    <td className="px-6 py-4">
                                        {idp.issuer ? (
                                            <div className="text-sm text-default break-all max-w-xs">{idp.issuer}</div>
                                        ) : (
                                            <span className="text-sm text-muted">—</span>
                                        )}
                                    </td>

                                    {/* End-user Login (Authorization Endpoint) */}
                                    <td className="px-4 py-4">
                                        {idp.authorization_endpoint ? (
                                            <div className="text-xs text-muted break-all font-mono">
                                                {shortenEndpoint(idp.authorization_endpoint, idp.issuer)}
                                            </div>
                                        ) : (
                                            <span className="text-sm text-muted">—</span>
                                        )}
                                    </td>

                                    {/* Get Access Token (Token Endpoint) */}
                                    <td className="px-4 py-4">
                                        {idp.token_endpoint ? (
                                            <div className="text-xs text-muted break-all font-mono">
                                                {shortenEndpoint(idp.token_endpoint, idp.issuer)}
                                            </div>
                                        ) : (
                                            <span className="text-sm text-muted">—</span>
                                        )}
                                    </td>

                                    {/* Get End-user Info (UserInfo Endpoint) */}
                                    <td className="px-4 py-4">
                                        {idp.userinfo_endpoint ? (
                                            <div className="text-xs text-muted break-all font-mono">
                                                {shortenEndpoint(idp.userinfo_endpoint, idp.issuer)}
                                            </div>
                                        ) : (
                                            <span className="text-sm text-muted">—</span>
                                        )}
                                    </td>

                                    {/* Protocol Column */}
                                    <td className="px-4 py-4 whitespace-nowrap">
                                        {unavailable ? (
                                            // A protocol the console does not have: its own name, not "OIDC".
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-default">
                                                {idp.protocol}
                                            </span>
                                        ) : (
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-800">
                                                OIDC
                                            </span>
                                        )}
                                        {unavailable && (
                                            <div
                                                className="text-xs text-muted mt-1"
                                                title={unavailable}
                                                data-testid="idp-protocol-unavailable"
                                            >
                                                Not available in this edition
                                            </div>
                                        )}
                                    </td>

                                    {/* Type Column */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        {idp.type ? (
                                            <span className="text-sm text-default capitalize">{idp.type.replace(/_/g, ' ')}</span>
                                        ) : (
                                            <span className="text-sm text-muted">—</span>
                                        )}
                                    </td>

                                    {/* Status Column */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                            <span
                                                className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                                                    idp.enabled
                                                        ? 'bg-green-100 text-green-800'
                                                        : 'bg-surface-raised text-default'
                                                }`}
                                            >
                                                {idp.enabled ? 'Enabled' : 'Disabled'}
                                            </span>
                                    </td>

                                    {/* Actions Column */}
                                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                                        <div className="flex items-center justify-end space-x-2">
                                            <button
                                                onClick={asyncHandler(() => handleTestConnection(idp.id), 'click')}
                                                disabled={testingConnection || !!unavailable}
                                                className={`p-2 text-secondary hover:text-primary-600 hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50${unavailable ? ' disabled:cursor-not-allowed' : ''}`}
                                                title={unavailable ? `Test Connection is not available. ${unavailable}` : 'Test Connection'}
                                            >
                                                <TestTube className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={asyncHandler(() => loadIdPDetails(idp.id), 'click')}
                                                disabled={loadingDetails}
                                                className="p-2 text-secondary hover:text-primary-600 hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50"
                                                title="View Details"
                                            >
                                                <Eye className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={asyncHandler(() => handleEditIdP(idp), 'click')}
                                                disabled={loadingDetails}
                                                className="p-2 text-secondary hover:text-primary-600 hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50"
                                                title="Edit"
                                            >
                                                <Edit className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={asyncHandler(() => handleDeleteIdP(idp.id), 'click')}
                                                disabled={!!unavailable}
                                                className={`p-2 text-red-600 hover:bg-red-50 rounded-md transition-colors${unavailable ? ' disabled:opacity-50 disabled:cursor-not-allowed' : ''}`}
                                                title={unavailable ? `Delete is not available. ${unavailable}` : 'Delete'}
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                                );
                            })}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Create/Edit Modal */}
            {(showCreateModal || showEditModal) && (
                <IdPServerForm
                    idp={showEditModal ? selectedIdP : null}
                    isEdit={showEditModal}
                    onSave={asyncHandler(handleIdPSaved, 'save')}
                    onCancel={handleIdPCancelled}
                />
            )}

            {/* Delete Confirmation Modal */}
            <GenericDeleteConfirmationModal
                isOpen={!!deleteConfirmation}
                onClose={() => {
                    if (!isDeleting) {
                        setDeleteConfirmation(null);
                        setDeleteError(null);
                        setDeleteInUse(null);
                    }
                }}
                onConfirm={asyncHandler(confirmDeleteIdP, 'confirm')}
                itemType="server"
                itemName={deleteConfirmation?.idpName || ''}
                itemInfo={deleteConfirmation?.idpInfo}
                isDeleting={isDeleting}
                errorMessage={deleteError}
                inUse={deleteInUse}
            />

            {/* Details Modal */}
            {showIdPDetails && selectedIdP && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] overflow-y-auto">
                        <div className="flex items-center justify-between px-6 py-4 border-b">
                            <div className="flex items-center space-x-3">
                                <Shield className="w-6 h-6 text-primary-600" />
                                <div>
                                    <h3 className="text-lg font-semibold text-default">{selectedIdP.name}</h3>
                                    <p className="text-sm text-secondary">Identity Provider Details</p>
                                </div>
                            </div>
                            <button
                                onClick={() => {
                                    setShowIdPDetails(false);
                                    setSelectedIdP(null);
                                }}
                                className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                            >
                                <X className="w-5 h-5 text-muted" />
                            </button>
                        </div>
                        <div className="p-6 space-y-4">
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">ID</label>
                                    <code className="text-xs bg-surface-raised px-2 py-1 rounded">{selectedIdP.id}</code>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">Type</label>
                                    <span className="text-sm text-default capitalize">{selectedIdP.type}</span>
                                </div>
                                <div className="col-span-2">
                                    <label className="block text-sm font-medium text-secondary mb-1">Issuer</label>
                                    <code className="text-xs bg-surface-raised px-2 py-1 rounded break-all">{selectedIdP.issuer}</code>
                                </div>
                                {selectedIdP.authorization_endpoint && (
                                    <div className="col-span-2">
                                        <label className="block text-sm font-medium text-secondary mb-1">Authorization Endpoint</label>
                                        <code className="text-xs bg-surface-raised px-2 py-1 rounded break-all">{selectedIdP.authorization_endpoint}</code>
                                    </div>
                                )}
                                {selectedIdP.token_endpoint && (
                                    <div className="col-span-2">
                                        <label className="block text-sm font-medium text-secondary mb-1">Token Endpoint</label>
                                        <code className="text-xs bg-surface-raised px-2 py-1 rounded break-all">{selectedIdP.token_endpoint}</code>
                                    </div>
                                )}
                                {selectedIdP.jwks_uri && (
                                    <div className="col-span-2">
                                        <label className="block text-sm font-medium text-secondary mb-1">JWKS URI</label>
                                        <code className="text-xs bg-surface-raised px-2 py-1 rounded break-all">{selectedIdP.jwks_uri}</code>
                                    </div>
                                )}
                            </div>
                            {whyUnavailable(selectedIdP) && (
                                <p className="text-sm text-secondary" data-testid="idp-discover-unavailable">
                                    {whyUnavailable(selectedIdP)} Its endpoints cannot be discovered here.
                                </p>
                            )}
                            <div className="flex items-center justify-between pt-4 border-t">
                                <button
                                    onClick={asyncHandler(() => handleDiscover(selectedIdP.id), 'click')}
                                    disabled={discovering || !!whyUnavailable(selectedIdP)}
                                    className="px-4 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-md hover:bg-primary-100 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    {discovering ? (
                                        <>
                                            <Loader className="w-4 h-4 animate-spin" />
                                            <span>Discovering...</span>
                                        </>
                                    ) : (
                                        <>
                                            <RefreshCw className="w-4 h-4" />
                                            <span>Discover Endpoints</span>
                                        </>
                                    )}
                                </button>
                                <button
                                    onClick={() => {
                                        setShowIdPDetails(false);
                                        setSelectedIdP(null);
                                    }}
                                    className="px-4 py-2 text-sm font-medium text-white bg-primary-600 border border-transparent rounded-md hover:bg-primary-700"
                                >
                                    Close
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default IdPServersTab;