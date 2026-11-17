/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect } from 'react';
import { Upload, X, Image, AlertTriangle } from 'lucide-react';
import { useGateway } from '../../../../hooks/useGateway.ts';
import { useToast } from '../../../common/Toast.tsx';
import { asyncHandler, runBackground } from '../../../../lib/asyncSafe';
import { errorKind } from '../../../../lib/narrow';

interface ServerIconProps {
    serverId: string;
    serverName: string;
    iconInfo?: {
        type: string;
        url: string;
        mime_type: string;
        size: number;
        uploaded_at: string;
        available_sizes?: string[];
    };
    size?: 'small' | 'medium' | 'large';
    editable?: boolean;
    onIconUpdate?: (iconInfo: any) => void;
    onIconDelete?: () => void;
}

const ServerIcon: React.FC<ServerIconProps> = ({
                                                   serverId,
                                                   serverName,
                                                   iconInfo,
                                                   size = 'medium',
                                                   editable = false,
                                                   onIconUpdate,
                                                   onIconDelete
                                               }) => {
    const toast = useToast();
    const [isUploading, setIsUploading] = useState(false);
    const [showUploadModal, setShowUploadModal] = useState(false);
    const [uploadMethod, setUploadMethod] = useState<'file' | 'url'>('file');
    const [inputIconUrl, setInputIconUrl] = useState('');
    const [dragActive, setDragActive] = useState(false);
    const [imageError, setImageError] = useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // Use the gateway hook for all icon operations
    const {
        uploadServerIcon,
        deleteServerIcon,
        getServerIconUrl
    } = useGateway();

    const sizeClasses = {
        small: 'w-8 h-8',
        medium: 'w-12 h-12',
        large: 'w-16 h-16'
    };

    // Reset image error state whenever iconInfo changes
    useEffect(() => {
        if (iconInfo?.uploaded_at) {
            setImageError(false);
        }
    }, [iconInfo?.uploaded_at]);

    const getIconUrl = () => {
        if (!iconInfo || imageError) return null;

        // Handle preview modes (when creating new servers)
        if (iconInfo.type === 'preview' || iconInfo.type === 'preview_url') {
            return iconInfo.url;
        }

        // Handle file type icons (with fallback for missing type)
        if (iconInfo.type === 'file' ||
            (!iconInfo.type && iconInfo.url && iconInfo.uploaded_at)) {

            const baseUrl = getServerIconUrl(serverId);
            const timestamp = iconInfo.uploaded_at
                ? new Date(iconInfo.uploaded_at).getTime()
                : Date.now();
            const cacheKey = `${timestamp}_${Math.random().toString(36).substr(2, 9)}`;

            return `${baseUrl}?t=${cacheKey}`;
        } else if (iconInfo.type === 'url') {
            return iconInfo.url;
        }

        return null;
    };

    const getDefaultIcon = () => {
        // Create a simple SVG icon with server initials
        const initials = serverName
            .split(' ')
            .map(word => word[0])
            .join('')
            .substring(0, 2)
            .toUpperCase();

        const colors = [
            'from-blue-500 to-blue-600',
            'from-green-500 to-green-600',
            'from-purple-500 to-purple-600',
            'from-orange-500 to-orange-600',
            'from-red-500 to-red-600',
            'from-indigo-500 to-indigo-600'
        ];

        // Use server ID to consistently pick a color
        const colorIndex = serverId.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % colors.length;
        const gradientClass = colors[colorIndex];

        return (
            <div className={`${sizeClasses[size]} bg-gradient-to-br ${gradientClass} rounded-lg flex items-center justify-center text-white font-semibold text-sm shadow-sm`}>
                {initials}
            </div>
        );
    };

    const handleFileUpload = async (file: File) => {
        if (!onIconUpdate) return;

        // Validate file type
        if (!file.type.startsWith('image/')) {
            toast.error('Invalid File Type', 'Please upload an image file (PNG, JPG, SVG).');
            return;
        }

        // Validate file size (2MB max)
        if (file.size > 2 * 1024 * 1024) {
            toast.error('File Too Large', 'Please upload an image smaller than 2MB.');
            return;
        }

        // Don't try to upload for preview servers (in create form)
        if (serverId === 'preview' || !serverId.trim()) {
            // For preview mode, just store the file data locally
            const reader = new FileReader();
            reader.onload = (e) => {
                const result = e.target?.result as string;
                if (result) {
                    const mockIconInfo = {
                        type: 'preview',
                        url: result,
                        mime_type: file.type,
                        size: file.size,
                        uploaded_at: new Date().toISOString(),
                        file_data: result
                    };
                    onIconUpdate(mockIconInfo);
                    setShowUploadModal(false);
                    setImageError(false);
                    toast.success('Icon Preview Updated', 'Icon will be uploaded when you save the server.');
                }
            };
            reader.readAsDataURL(file);
            return;
        }

        setIsUploading(true);
        try {
            const result = await uploadServerIcon(serverId, file);

            if (result?.icon_info) {
                setImageError(false);
                await onIconUpdate(result.icon_info);
                toast.success('Icon Uploaded', 'Server icon has been updated successfully.');
            }

            setShowUploadModal(false);
        } catch (error) {
            console.error('Icon upload failed:', errorKind(error));
            toast.error(
                'Upload Failed',
                error instanceof Error ? error.message : 'Failed to upload icon.'
            );
        } finally {
            setIsUploading(false);
        }
    };

    const handleUrlUpload = async () => {
        if (!onIconUpdate || !inputIconUrl.trim()) return;

        // Basic URL validation
        try {
            new URL(inputIconUrl);
        } catch {
            toast.error('Invalid URL', 'Please enter a valid image URL.');
            return;
        }

        // Don't try to upload for preview servers (in create form)
        if (serverId === 'preview' || !serverId.trim()) {
            // For preview mode, just store the URL
            const mockIconInfo = {
                type: 'preview_url',
                url: inputIconUrl,
                mime_type: 'image/unknown',
                size: 0,
                uploaded_at: new Date().toISOString()
            };
            onIconUpdate(mockIconInfo);
            setShowUploadModal(false);
            setImageError(false);
            setInputIconUrl('');
            toast.success('Icon Preview Updated', 'Icon URL will be saved when you save the server.');
            return;
        }

        setIsUploading(true);
        try {
            toast.warning('Icon Upload', 'Server icon upload by URL is not supported.');

            setShowUploadModal(false);
            setInputIconUrl('');
        } catch (error) {
            console.error('Icon URL upload failed:', errorKind(error));
            toast.error(
                'Upload Failed',
                error instanceof Error ? error.message : 'Failed to upload icon from URL.'
            );
        } finally {
            setIsUploading(false);
        }
    };

    const handleDelete = () => {
        setShowDeleteConfirm(true);
    };

    const confirmDelete = async () => {
        if (!onIconDelete) return;

        setShowDeleteConfirm(false);

        try {
            await deleteServerIcon(serverId);
            await onIconDelete();
            setShowUploadModal(false);
            toast.success('Icon Deleted', 'Server icon has been removed successfully.');
        } catch (error) {
            console.error('Icon delete failed:', errorKind(error));
            toast.error(
                'Delete Failed',
                error instanceof Error ? error.message : 'Failed to delete icon.'
            );
        }
    };

    const handleDrag = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.type === 'dragenter' || e.type === 'dragover') {
            setDragActive(true);
        } else if (e.type === 'dragleave') {
            setDragActive(false);
        }
    };

    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        setDragActive(false);

        const files = e.dataTransfer.files;
        if (files && files[0]) {
            if (files[0].type.startsWith('image/')) {
                runBackground(handleFileUpload(files[0]), 'file upload');
            } else {
                toast.error('Invalid File Type', 'Please drop an image file (PNG, JPG, SVG).');
            }
        }
    };

    const iconUrl = getIconUrl();

    return (
        <div>
            <div className={`relative group ${editable ? 'cursor-pointer' : ''}`}>
                {iconUrl && !imageError ? (
                    // `object-contain` (not `object-cover`) so non-square
                    // server icons letterbox inside the frame instead of
                    // getting cropped. E.g. the built-in Web Search icon
                    // is 192×148; at object-cover it lost roughly half of
                    // the glyph. `bg-surface-alt` fills the letterboxed
                    // gap so the rounded frame still reads as a tile.
                    <img
                        src={iconUrl}
                        alt={`${serverName} icon`}
                        className={`${sizeClasses[size]} object-contain rounded-lg shadow-sm bg-surface-alt`}
                        onError={() => {
                            console.error('Server icon failed to load');
                            setImageError(true);
                        }}
                        onLoad={() => {
                            setImageError(false);
                        }}
                    />
                ) : (
                    getDefaultIcon()
                )}

                {/* Edit overlay */}
                {editable && (
                    <div className="absolute inset-0 bg-black bg-opacity-0 group-hover:bg-opacity-30 transition-all duration-200 rounded-lg flex items-center justify-center">
                        <div className="opacity-0 group-hover:opacity-100 transition-opacity duration-200">
                            <button
                                onClick={(e) => {
                                    e.stopPropagation();
                                    e.preventDefault();
                                    setShowUploadModal(true);
                                }}
                                className="p-1 bg-surface rounded-full shadow-sm hover:shadow-md transition-shadow"
                                title="Change icon"
                                type="button"
                            >
                                <Upload className="w-3 h-3 text-secondary" />
                            </button>
                        </div>
                    </div>
                )}
            </div>

            {/* Upload Modal */}
            {showUploadModal && (
                <div
                    className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
                    onClick={(e) => {
                        if (e.target === e.currentTarget) {
                            setShowUploadModal(false);
                        }
                    }}
                >
                    <div className="bg-surface rounded-lg shadow-xl max-w-md w-full mx-4">
                        {/* Header */}
                        <div className="flex items-center justify-between px-6 py-4 border-b">
                            <h3 className="text-lg font-semibold text-default">Update Server Icon</h3>
                            <button
                                onClick={() => setShowUploadModal(false)}
                                className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                                disabled={isUploading}
                                type="button"
                            >
                                <X className="w-5 h-5 text-muted" />
                            </button>
                        </div>

                        <div className="px-6 py-4 space-y-4">
                            {/* Upload method selection */}
                            <div className="flex space-x-4">
                                <label className="flex items-center space-x-2 cursor-pointer">
                                    <input
                                        type="radio"
                                        checked={uploadMethod === 'file'}
                                        onChange={() => setUploadMethod('file')}
                                        className="text-primary focus:ring-primary"
                                    />
                                    <span className="text-sm font-medium text-secondary">Upload File</span>
                                </label>
                                <label className="flex items-center space-x-2 cursor-pointer">
                                    <input
                                        type="radio"
                                        checked={uploadMethod === 'url'}
                                        onChange={() => setUploadMethod('url')}
                                        className="text-primary focus:ring-primary"
                                    />
                                    <span className="text-sm font-medium text-secondary">From URL</span>
                                </label>
                            </div>

                            {uploadMethod === 'file' ? (
                                <div
                                    className={`border-2 border-dashed rounded-lg p-6 text-center transition-colors ${
                                        dragActive ? 'border-primary bg-primary-50' : 'border-strong hover:border-gray-400'
                                    }`}
                                    onDragEnter={handleDrag}
                                    onDragLeave={handleDrag}
                                    onDragOver={handleDrag}
                                    onDrop={handleDrop}
                                >
                                    <input
                                        ref={fileInputRef}
                                        type="file"
                                        accept="image/*"
                                        onChange={(e) => {
                                            if (e.target.files?.[0]) {
                                                runBackground(handleFileUpload(e.target.files[0]), 'file upload');
                                            }
                                        }}
                                        className="hidden"
                                    />

                                    <Image className="w-12 h-12 text-muted mx-auto mb-4" />
                                    <p className="text-secondary mb-3">
                                        Drop an image here or click to browse
                                    </p>
                                    <button
                                        onClick={() => fileInputRef.current?.click()}
                                        className="px-4 py-2 bg-primary text-white rounded-md hover:bg-primary-600 disabled:opacity-50 transition-colors"
                                        disabled={isUploading}
                                        type="button"
                                    >
                                        {isUploading ? 'Uploading...' : 'Choose File'}
                                    </button>
                                    <p className="text-xs text-muted mt-3">
                                        PNG, JPG, SVG up to 2MB. Recommended: 128x128px
                                    </p>
                                </div>
                            ) : (
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-2">
                                            Image URL
                                        </label>
                                        <input
                                            type="url"
                                            value={inputIconUrl}
                                            onChange={(e) => setInputIconUrl(e.target.value)}
                                            placeholder="https://example.com/icon.png"
                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                                        />
                                    </div>
                                    <button
                                        onClick={asyncHandler(handleUrlUpload, 'click')}
                                        disabled={isUploading || !inputIconUrl.trim()}
                                        className="w-full px-4 py-2 bg-primary text-white rounded-md hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                        type="button"
                                    >
                                        {isUploading ? 'Uploading...' : 'Upload from URL'}
                                    </button>
                                </div>
                            )}

                            {/* Current icon info */}
                            {iconInfo && iconInfo.type !== 'preview' && iconInfo.type !== 'preview_url' && (
                                <div className="pt-4 border-t space-y-2">
                                    <div className="flex items-center justify-between text-sm">
                                        <span className="text-secondary">Current icon:</span>
                                        <div className="flex items-center space-x-3">
                                          <span className="text-default font-medium">
                                            {Math.round(iconInfo.size / 1024)}KB
                                          </span>
                                            <button
                                                onClick={handleDelete}
                                                className="text-red-600 hover:text-red-800 text-sm font-medium transition-colors"
                                                type="button"
                                            >
                                                Delete
                                            </button>
                                        </div>
                                    </div>
                                    {iconInfo.available_sizes && (
                                        <div className="text-xs text-muted">
                                            Available sizes: {iconInfo.available_sizes.join(', ')}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Delete Confirmation Modal */}
            {showDeleteConfirm && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
                    <div className="bg-surface rounded-lg shadow-xl max-w-md w-full mx-4">
                        <div className="flex items-center justify-between p-6 border-b border-red-100 bg-red-50">
                            <div className="flex items-center space-x-3">
                                <div className="w-12 h-12 bg-red-100 rounded-full flex items-center justify-center">
                                    <AlertTriangle className="w-6 h-6 text-red-600" />
                                </div>
                                <div>
                                    <h2 className="text-xl font-semibold text-red-900">Delete Icon</h2>
                                    <p className="text-sm text-red-700 mt-0.5">This action cannot be undone</p>
                                </div>
                            </div>
                            <button
                                onClick={() => setShowDeleteConfirm(false)}
                                className="p-2 hover:bg-red-100 rounded-md transition-colors"
                            >
                                <X className="w-5 h-5 text-red-600" />
                            </button>
                        </div>

                        <div className="p-6">
                            <p className="text-secondary mb-4">
                                Are you sure you want to delete this server icon? The server will revert to its default generated icon.
                            </p>
                        </div>

                        <div className="flex justify-end space-x-3 p-6 border-t bg-surface-alt">
                            <button
                                onClick={() => setShowDeleteConfirm(false)}
                                className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={asyncHandler(confirmDelete, 'click')}
                                className="px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 transition-colors"
                            >
                                Delete Icon
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default ServerIcon;