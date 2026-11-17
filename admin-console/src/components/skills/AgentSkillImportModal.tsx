/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useCallback } from 'react';
import {
    X,
    Upload,
    FileArchive,
    CheckCircle2,
    XCircle,
    Loader2,
    Trash2,
    FolderOpen,
    AlertTriangle,
    FileText,
    Code2,
    MinusCircle,
} from 'lucide-react';
import type {
    AgentSkillImportResponse,
    AgentSkillImportResult,
    AgentSkillImportStatus,
    AgentSkillLimits,
} from '../../lib/agentSkillTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler } from '../../lib/asyncSafe';

interface AgentSkillImportModalProps {
    onClose: () => void;
    onImport: (files: File[]) => Promise<AgentSkillImportResponse>;
    onImportComplete: () => void;
    /** The limits the list of skills states; null when it stated none. */
    limits?: AgentSkillLimits | null;
}

/** Why the Control Plane refused or skipped a skill, by its reason code. */
const REASONS: Record<string, string> = {
    not_a_zip: 'The file is not a .zip archive',
    invalid_archive: 'The zip could not be read',
    archive_too_large: 'The zip is larger than the archive limit',
    too_many_skills: 'The zip holds more skills than the archive limit',
    no_skill: 'No SKILL.md found',
    too_large: 'The skill is larger than the size limit',
    invalid_skill: 'The SKILL.md is not valid',
    quota_exceeded: 'The tenant\'s skill quota is reached',
    already_exists: 'A skill of this name already exists',
};

/** The words for a reason code; a code this console does not know is shown as it is. */
const reasonText = (reason: string): string => REASONS[reason] ?? reason;

/** Bytes in MB as the Control Plane states its limits (decimal, 30_000_000 → "30 MB"). */
const megabytes = (bytes: number): string => `${Number((bytes / 1_000_000).toFixed(1))} MB`;

const STATUS: Record<AgentSkillImportStatus, { label: string; row: string; badge: string; Icon: typeof CheckCircle2; icon: string }> = {
    imported: { label: 'Imported', row: 'bg-green-50 border-green-100', badge: 'bg-green-100 text-green-700', Icon: CheckCircle2, icon: 'text-green-500' },
    refused: { label: 'Refused', row: 'bg-red-50 border-red-100', badge: 'bg-red-100 text-red-700', Icon: XCircle, icon: 'text-red-500' },
    skipped: { label: 'Skipped', row: 'bg-yellow-50 border-yellow-100', badge: 'bg-yellow-100 text-yellow-700', Icon: MinusCircle, icon: 'text-yellow-500' },
};

/** Where a skill was in its zip: the zip, and the folder when the zip holds several. */
const placeOf = (result: AgentSkillImportResult): string =>
    result.folder ? `${result.zip_filename} / ${result.folder}` : result.zip_filename;

const AgentSkillImportModal: React.FC<AgentSkillImportModalProps> = ({
    onClose,
    onImport,
    onImportComplete,
    limits = null,
}) => {
    const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
    const [importing, setImporting] = useState(false);
    const [results, setResults] = useState<AgentSkillImportResponse | null>(null);
    const [dragOver, setDragOver] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const directoryInputRef = useRef<HTMLInputElement>(null);

    const addFiles = useCallback((newFiles: FileList | File[]) => {
        const zipFiles = Array.from(newFiles).filter(f => f.name.endsWith('.zip'));
        if (zipFiles.length === 0) return;

        setSelectedFiles(prev => {
            const existingNames = new Set(prev.map(f => f.name));
            const unique = zipFiles.filter(f => !existingNames.has(f.name));
            return [...prev, ...unique];
        });
    }, []);

    const removeFile = (index: number) => {
        setSelectedFiles(prev => prev.filter((_, i) => i !== index));
    };

    const handleDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(false);
        if (e.dataTransfer.files.length > 0) {
            addFiles(e.dataTransfer.files);
        }
    }, [addFiles]);

    const handleImport = async () => {
        if (selectedFiles.length === 0) return;
        setImporting(true);
        setResults(null);

        try {
            const response = await onImport(selectedFiles);
            setResults(response);
            if (response.success_count > 0) {
                onImportComplete();
            }
        } catch (error) {
            const message = errorMessage(error, 'Import failed');
            setResults({
                success: false,
                message,
                total_files: selectedFiles.length,
                success_count: 0,
                error_count: selectedFiles.length,
                refused_count: selectedFiles.length,
                skipped_count: 0,
                results: selectedFiles.map(f => ({
                    zip_filename: f.name,
                    folder: null,
                    status: 'refused',
                    success: false,
                    skill_name: f.name,
                    skill_id: null,
                    error: message,
                    reason: null,
                    resources_count: 0,
                    scripts_count: 0,
                    skipped_files: [],
                })),
                archives: [],
            });
        } finally {
            setImporting(false);
        }
    };

    const formatFileSize = (bytes: number): string => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    };

    const hasResults = results !== null;
    const canImport = selectedFiles.length > 0 && !importing && !hasResults;

    return (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <div className="p-2 bg-primary-50 rounded-lg">
                            <Upload className="w-5 h-5 text-primary-600" />
                        </div>
                        <div>
                            <h2 className="text-lg font-semibold text-default">Import Agent Skills</h2>
                            <p className="text-sm text-muted">
                                Upload skill zip archives to import
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region px-6 py-4 space-y-4">
                    {!hasResults ? (
                        <>
                            {/* Drop zone */}
                            <div
                                className={`relative border-2 border-dashed rounded-xl p-8 text-center transition-colors ${
                                    dragOver
                                        ? 'border-primary-400 bg-primary-50'
                                        : 'border-strong hover:border-gray-400 bg-surface-alt'
                                }`}
                                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                                onDragLeave={() => setDragOver(false)}
                                onDrop={handleDrop}
                            >
                                <FileArchive className={`w-12 h-12 mx-auto mb-3 ${
                                    dragOver ? 'text-primary-500' : 'text-muted'
                                }`} />
                                <p className="text-sm font-medium text-secondary mb-1">
                                    Drag & drop skill zip files here
                                </p>
                                <p className="text-xs text-muted mb-4">
                                    Each skill has a SKILL.md; a zip may hold several skills
                                </p>
                                <div className="flex items-center justify-center space-x-3">
                                    <button
                                        onClick={() => fileInputRef.current?.click()}
                                        className="px-4 py-2 bg-surface border border-strong rounded-lg text-sm font-medium text-secondary hover:bg-surface-alt transition-colors inline-flex items-center space-x-2"
                                    >
                                        <FileArchive className="w-4 h-4" />
                                        <span>Select Zip Files</span>
                                    </button>
                                    <button
                                        onClick={() => directoryInputRef.current?.click()}
                                        className="px-4 py-2 bg-surface border border-strong rounded-lg text-sm font-medium text-secondary hover:bg-surface-alt transition-colors inline-flex items-center space-x-2"
                                    >
                                        <FolderOpen className="w-4 h-4" />
                                        <span>Select Directory</span>
                                    </button>
                                </div>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    accept=".zip"
                                    multiple
                                    className="hidden"
                                    onChange={(e) => {
                                        if (e.target.files) addFiles(e.target.files);
                                        e.target.value = '';
                                    }}
                                />
                                <input
                                    ref={directoryInputRef}
                                    type="file"
                                    accept=".zip"
                                    multiple
                                    // @ts-expect-error webkitdirectory is not in the type defs
                                    webkitdirectory=""
                                    className="hidden"
                                    onChange={(e) => {
                                        if (e.target.files) addFiles(e.target.files);
                                        e.target.value = '';
                                    }}
                                />
                            </div>

                            {/* Selected files list */}
                            {selectedFiles.length > 0 && (
                                <div>
                                    <div className="flex items-center justify-between mb-2">
                                        <h3 className="text-sm font-medium text-secondary">
                                            {selectedFiles.length} file{selectedFiles.length !== 1 ? 's' : ''} selected
                                        </h3>
                                        <button
                                            onClick={() => setSelectedFiles([])}
                                            className="text-xs text-muted hover:text-red-600 transition-colors"
                                        >
                                            Clear all
                                        </button>
                                    </div>
                                    <div className="space-y-1.5 max-h-48 overflow-y-auto">
                                        {selectedFiles.map((file, index) => (
                                            <div
                                                key={`${file.name}-${index}`}
                                                className="flex items-center justify-between px-3 py-2 bg-surface-alt rounded-lg group"
                                            >
                                                <div className="flex items-center space-x-3 min-w-0">
                                                    <FileArchive className="w-4 h-4 text-primary-500 flex-shrink-0" />
                                                    <span className="text-sm text-default truncate">{file.name}</span>
                                                    <span className="text-xs text-muted flex-shrink-0">
                                                        {formatFileSize(file.size)}
                                                    </span>
                                                </div>
                                                <button
                                                    onClick={() => removeFile(index)}
                                                    className="p-1 text-muted hover:text-red-500 opacity-0 group-hover:opacity-100 transition-all"
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                </button>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {/* Info box */}
                            <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
                                <div className="flex items-start space-x-2">
                                    <AlertTriangle className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
                                    <div className="text-xs text-blue-700 space-y-1">
                                        <p>Skills will be imported as <strong>Draft</strong> status and assigned to the <strong>Default Resource Group</strong>.</p>
                                        <p>A zip with a <code className="bg-blue-100 px-1 rounded">SKILL.md</code> at its root is one skill; otherwise it holds one skill per top-level folder with a SKILL.md. Each skill&apos;s reference files, scripts and assets are imported with it; files outside every skill folder are listed and not imported.</p>
                                        {limits && (
                                            <p data-testid="skill-import-limits">
                                                A skill may hold up to {megabytes(limits.skill_max_bytes)} of files. A zip may hold up to {limits.archive_max_skills} skills and {megabytes(limits.archive_max_bytes)}.
                                            </p>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </>
                    ) : (
                        /* Results view */
                        <div className="space-y-4">
                            {/* Summary */}
                            <div className={`p-4 rounded-lg border ${
                                results.error_count === 0
                                    ? 'bg-green-50 border-green-200'
                                    : results.success_count === 0
                                        ? 'bg-red-50 border-red-200'
                                        : 'bg-yellow-50 border-yellow-200'
                            }`}>
                                <div className="flex items-center space-x-3">
                                    {results.error_count === 0 ? (
                                        <CheckCircle2 className="w-6 h-6 text-green-500" />
                                    ) : results.success_count === 0 ? (
                                        <XCircle className="w-6 h-6 text-red-500" />
                                    ) : (
                                        <AlertTriangle className="w-6 h-6 text-yellow-500" />
                                    )}
                                    <div>
                                        <p className="font-medium text-default">{results.message}</p>
                                        <p className="text-sm text-secondary mt-0.5">
                                            {results.total_files} file{results.total_files !== 1 ? 's' : ''} processed
                                        </p>
                                        <p data-testid="skill-import-counts" className="text-xs text-muted mt-0.5">
                                            {results.success_count} imported · {results.refused_count ?? results.error_count} refused · {results.skipped_count ?? 0} skipped
                                        </p>
                                    </div>
                                </div>
                            </div>

                            {/* One row per skill: a zip may hold several */}
                            <div className="space-y-2">
                                {results.results.map((result: AgentSkillImportResult, index: number) => {
                                    const status = STATUS[result.status] ?? STATUS.refused;
                                    return (
                                        <div
                                            key={index}
                                            data-testid="skill-import-result"
                                            className={`flex items-start space-x-3 px-4 py-3 rounded-lg border ${status.row}`}
                                        >
                                            <status.Icon className={`w-5 h-5 mt-0.5 flex-shrink-0 ${status.icon}`} />
                                            <div className="min-w-0 flex-1 min-h-0">
                                                <div className="flex items-center gap-2 flex-wrap">
                                                    <p className="text-sm font-medium text-default">{result.skill_name}</p>
                                                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${status.badge}`}>{status.label}</span>
                                                </div>
                                                {placeOf(result) !== result.skill_name && (
                                                    <p className="text-xs text-muted mt-0.5">{placeOf(result)}</p>
                                                )}
                                                {result.status === 'imported' ? (
                                                    <div className="flex items-center space-x-3 mt-1">
                                                        {result.resources_count > 0 && (
                                                            <span className="inline-flex items-center space-x-1 text-xs text-secondary">
                                                                <FileText className="w-3 h-3" />
                                                                <span>{result.resources_count} resource{result.resources_count !== 1 ? 's' : ''}</span>
                                                            </span>
                                                        )}
                                                        {result.scripts_count > 0 && (
                                                            <span className="inline-flex items-center space-x-1 text-xs text-secondary">
                                                                <Code2 className="w-3 h-3" />
                                                                <span>{result.scripts_count} script{result.scripts_count !== 1 ? 's' : ''}</span>
                                                            </span>
                                                        )}
                                                        {result.resources_count === 0 && result.scripts_count === 0 && (
                                                            <span className="text-xs text-muted">SKILL.md only</span>
                                                        )}
                                                    </div>
                                                ) : (
                                                    <>
                                                        {result.reason && (
                                                            <p className="text-xs font-medium text-secondary mt-0.5">{reasonText(result.reason)}</p>
                                                        )}
                                                        {result.error && (
                                                            <p className={`text-xs mt-0.5 ${result.status === 'skipped' ? 'text-yellow-700' : 'text-red-600'}`}>{result.error}</p>
                                                        )}
                                                    </>
                                                )}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>

                            {/* Files of a zip outside every skill folder: reported, never attached */}
                            {(results.archives ?? []).some((archive) => archive.unassigned_files.length > 0) && (
                                <div data-testid="skill-import-unassigned" className="space-y-2">
                                    {results.archives
                                        .filter((archive) => archive.unassigned_files.length > 0)
                                        .map((archive) => (
                                            <div key={archive.zip_filename} className="px-4 py-3 rounded-lg border border-default bg-surface-alt">
                                                <p className="text-xs font-medium text-secondary">
                                                    {archive.zip_filename}: {archive.unassigned_files.length} file{archive.unassigned_files.length !== 1 ? 's' : ''} belong{archive.unassigned_files.length === 1 ? 's' : ''} to no skill and {archive.unassigned_files.length !== 1 ? 'were' : 'was'} not imported
                                                </p>
                                                <ul className="mt-1 space-y-0.5 max-h-32 overflow-y-auto">
                                                    {archive.unassigned_files.map((file) => (
                                                        <li key={file} className="text-xs font-mono text-muted truncate">{file}</li>
                                                    ))}
                                                </ul>
                                            </div>
                                        ))}
                                </div>
                            )}
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-end space-x-3 px-6 py-4 border-t border-default bg-surface-alt rounded-b-xl">
                    {!hasResults ? (
                        <>
                            <button
                                onClick={onClose}
                                className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={asyncHandler(handleImport, 'click')}
                                disabled={!canImport}
                                className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center space-x-2"
                            >
                                {importing ? (
                                    <>
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                        <span>Importing...</span>
                                    </>
                                ) : (
                                    <>
                                        <Upload className="w-4 h-4" />
                                        <span>Import {selectedFiles.length} Skill{selectedFiles.length !== 1 ? 's' : ''}</span>
                                    </>
                                )}
                            </button>
                        </>
                    ) : (
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
                        >
                            Done
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
};

export default AgentSkillImportModal;
