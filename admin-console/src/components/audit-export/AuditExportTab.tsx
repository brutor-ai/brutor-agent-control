/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AuditExportTab — list view for scheduled proxy_log export
 * destinations.
 *
 * Each row carries enough status info that an admin doesn't have
 * to dig further to answer "is my SIEM ingestion working?":
 *   - destination type (s3 / webhook)
 *   - last_run_status badge (success / no_data / failure / running)
 *   - last_run_rows count and how long ago
 *   - last_run_error tooltip on failure
 *
 * Actions:
 *   - Run Now — manual trigger that bypasses the schedule; useful
 *     after editing credentials to verify they work.
 *   - Edit / Delete — standard CRUD.
 *
 * Required permission: PROXY_LOGS_READ (list/read) or
 * PROXY_MANAGEMENT (mutate). The backend enforces; this UI just
 * surfaces the actions optimistically — a 403 on save shows up
 * in the form's error banner.
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    Download,
    Plus,
    Trash2,
    RefreshCw,
    Play,
    AlertCircle,
    Loader,
    Edit,
    CheckCircle,
    XCircle,
    Clock,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import { TABLE_TH, TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import AuditExportForm from './AuditExportForm';
import { AuditExportDestination } from '../../lib/types';
import { asyncHandler } from '../../lib/asyncSafe';


const AuditExportTab: React.FC = () => {
    const {
        fetchAuditExportDestinations,
        fetchAuditExportDestination,
        deleteAuditExportDestination,
        runAuditExportDestinationNow,
    } = useGateway();
    const toast = useToast();

    const [destinations, setDestinations] = useState<AuditExportDestination[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [editingDestination, setEditingDestination] = useState<AuditExportDestination | null>(null);
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [showEditModal, setShowEditModal] = useState(false);
    const [runningId, setRunningId] = useState<string | null>(null);
    const [deleteConfirmation, setDeleteConfirmation] = useState<AuditExportDestination | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);

    const loadDestinations = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const response = await fetchAuditExportDestinations();
            setDestinations(response.destinations ?? []);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load destinations');
        } finally {
            setLoading(false);
        }
    }, [fetchAuditExportDestinations]);

    useEffect(() => { void loadDestinations(); }, [loadDestinations]);

    const handleEdit = async (destination: AuditExportDestination) => {
        // The row is read again, so the form opens on the configuration as
        // it is stored now, with what it says of each secret.
        try {
            const full = await fetchAuditExportDestination(destination.id);
            setEditingDestination(full);
            setShowEditModal(true);
        } catch {
            // useGateway already pushed an error toast; nothing more here.
        }
    };

    const handleRunNow = async (destination: AuditExportDestination) => {
        setRunningId(destination.id);
        try {
            const result = await runAuditExportDestinationNow(destination.id);
            if (result.outcome === 'success') {
                toast.success(`Exported ${result.rows_exported} rows`);
            } else if (result.outcome === 'no_data') {
                toast.success('No new rows since last run — high-water mark advanced');
            } else {
                toast.error(`Export failed: ${result.error ?? 'unknown'}`);
            }
            await loadDestinations();
        } catch (err) {
            toast.error(err instanceof Error ? err.message : 'Run-now failed');
        } finally {
            setRunningId(null);
        }
    };

    const handleDelete = async () => {
        if (!deleteConfirmation) return;
        setIsDeleting(true);
        try {
            await deleteAuditExportDestination(deleteConfirmation.id);
            toast.success('Destination deleted');
            setDeleteConfirmation(null);
            await loadDestinations();
        } catch (err) {
            toast.error(err instanceof Error ? err.message : 'Delete failed');
        } finally {
            setIsDeleting(false);
        }
    };

    return (
        <div className="p-6 h-full overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
                <div>
                    <h1 className="text-2xl font-semibold text-default flex items-center">
                        <Download className="w-6 h-6 mr-2 text-primary-600" />
                        Audit Export Destinations
                    </h1>
                    <p className="text-sm text-secondary mt-1">
                        Scheduled export of proxy logs to S3 / SIEM / webhook collectors.
                        See <span className="font-mono">export_proxy_log_s3.md</span> for the payload format.
                    </p>
                </div>
                <div className="flex space-x-2">
                    <button
                        onClick={() => void loadDestinations()}
                        disabled={loading}
                        className="px-3 py-2 border border-strong rounded-md text-secondary hover:bg-surface-alt flex items-center"
                    >
                        <RefreshCw className={`w-4 h-4 mr-1 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                    <button
                        onClick={() => setShowCreateModal(true)}
                        className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700 flex items-center"
                    >
                        <Plus className="w-4 h-4 mr-1" />
                        New Destination
                    </button>
                </div>
            </div>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md flex items-start">
                    <AlertCircle className="w-5 h-5 text-red-500 mr-2 flex-shrink-0 mt-0.5" />
                    <div>
                        <p className="text-sm font-medium text-red-700">Failed to load destinations</p>
                        <p className="text-sm text-red-600 mt-1">{error}</p>
                    </div>
                </div>
            )}

            {loading ? (
                <div className="flex items-center justify-center py-12">
                    <Loader className="w-6 h-6 animate-spin text-primary-600" />
                </div>
            ) : destinations.length === 0 ? (
                <div className="text-center py-12 bg-surface border border-default rounded-lg">
                    <Download className="w-12 h-12 mx-auto text-muted mb-3" />
                    <h3 className="text-lg font-medium text-default mb-1">No destinations configured</h3>
                    <p className="text-sm text-secondary mb-4">
                        Configure your first S3 bucket or SIEM webhook to start exporting proxy logs.
                    </p>
                    <button
                        onClick={() => setShowCreateModal(true)}
                        className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700 inline-flex items-center"
                    >
                        <Plus className="w-4 h-4 mr-1" />
                        New Destination
                    </button>
                </div>
            ) : (
                <div className="bg-surface border border-default rounded-lg overflow-hidden">
                    <table className="w-full">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={TABLE_TH}>Name</th>
                                <th className={TABLE_TH}>Type</th>
                                <th className={TABLE_TH}>Interval</th>
                                <th className={TABLE_TH}>Status</th>
                                <th className={TABLE_TH}>Last Run</th>
                                <th className={`px-4 py-3 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {destinations.map(dest => (
                                <DestinationRow
                                    key={dest.id}
                                    destination={dest}
                                    isRunning={runningId === dest.id}
                                    onRunNow={() => void handleRunNow(dest)}
                                    onEdit={() => void handleEdit(dest)}
                                    onDelete={() => setDeleteConfirmation(dest)}
                                />
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {showCreateModal && (
                <AuditExportForm
                    onCancel={() => setShowCreateModal(false)}
                    onSuccess={() => {
                        setShowCreateModal(false);
                        toast.success('Destination created');
                        void loadDestinations();
                    }}
                />
            )}

            {showEditModal && editingDestination && (
                <AuditExportForm
                    destination={editingDestination}
                    isEdit
                    onCancel={() => { setShowEditModal(false); setEditingDestination(null); }}
                    onSuccess={() => {
                        setShowEditModal(false);
                        setEditingDestination(null);
                        toast.success('Destination updated');
                        void loadDestinations();
                    }}
                />
            )}

            <GenericDeleteConfirmationModal
                isOpen={!!deleteConfirmation}
                onClose={() => { if (!isDeleting) setDeleteConfirmation(null); }}
                onConfirm={asyncHandler(handleDelete, 'confirm')}
                itemType="audit-export-destination"
                itemName={deleteConfirmation?.name ?? ''}
                isDeleting={isDeleting}
            />
        </div>
    );
};


// ── Single-row sub-component — keeps the table render tidy ────────────────

interface DestinationRowProps {
    destination: AuditExportDestination;
    isRunning: boolean;
    onRunNow: () => void;
    onEdit: () => void;
    onDelete: () => void;
}

const DestinationRow: React.FC<DestinationRowProps> = ({
    destination, isRunning, onRunNow, onEdit, onDelete,
}) => {
    const status = destination.last_run_status;
    return (
        <tr className="border-b border-default last:border-b-0 hover:bg-surface-alt">
            <td className="px-4 py-3">
                <div className="font-medium text-default">{destination.name}</div>
                {!destination.enabled && (
                    <span className="inline-block mt-1 px-2 py-0.5 text-xs bg-gray-100 text-gray-600 rounded">
                        Disabled
                    </span>
                )}
            </td>
            <td className="px-4 py-3 text-sm text-secondary">
                <span className="font-mono">{destination.destination_type}</span>
            </td>
            <td className="px-4 py-3 text-sm text-secondary">
                {formatInterval(destination.interval_seconds)}
            </td>
            <td className="px-4 py-3">
                <StatusBadge status={status} error={destination.last_run_error} />
            </td>
            <td className="px-4 py-3 text-sm text-secondary">
                {destination.last_run_at ? (
                    <>
                        <div>{formatRelative(destination.last_run_at)}</div>
                        {destination.last_run_rows !== null && destination.last_run_rows !== undefined && (
                            <div className="text-xs text-muted">{destination.last_run_rows} rows</div>
                        )}
                    </>
                ) : (
                    <span className="text-muted">Never run</span>
                )}
            </td>
            <td className="px-4 py-3 text-right">
                <div className="flex justify-end space-x-1">
                    <button
                        onClick={onRunNow}
                        disabled={isRunning}
                        title="Run now — manually trigger export"
                        className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded disabled:opacity-50"
                    >
                        {isRunning ? <Loader size={14} className="animate-spin" /> : <Play size={14} />}
                    </button>
                    <button
                        onClick={onEdit}
                        title="Edit"
                        className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded"
                    >
                        <Edit size={14} />
                    </button>
                    <button
                        onClick={onDelete}
                        title="Delete"
                        className="p-2 text-red-600 hover:bg-red-50 rounded"
                    >
                        <Trash2 size={14} />
                    </button>
                </div>
            </td>
        </tr>
    );
};


// ── Status badge: matches backend STATUS_* constants ──────────────────────

const StatusBadge: React.FC<{ status?: string | null; error?: string | null }> = ({ status, error }) => {
    if (!status) {
        return <span className="text-xs text-muted">—</span>;
    }
    if (status === 'success') {
        return (
            <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-green-100 text-green-800 rounded">
                <CheckCircle size={12} className="mr-1" />Success
            </span>
        );
    }
    if (status === 'no_data') {
        return (
            <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-blue-100 text-blue-800 rounded">
                <CheckCircle size={12} className="mr-1" />No data
            </span>
        );
    }
    if (status === 'running') {
        return (
            <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-yellow-100 text-yellow-800 rounded">
                <Clock size={12} className="mr-1" />Running
            </span>
        );
    }
    // failure
    return (
        <span
            className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-red-100 text-red-800 rounded cursor-help"
            title={error || 'See gateway logs for details'}
        >
            <XCircle size={12} className="mr-1" />Failed
        </span>
    );
};


function formatInterval(seconds: number): string {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
}

function formatRelative(iso: string): string {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return iso;
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}


export default AuditExportTab;
