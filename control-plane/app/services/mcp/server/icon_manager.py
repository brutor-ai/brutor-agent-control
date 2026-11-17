# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Database-Based Icon Manager Service for MCP Servers
Stores icons in database with validation, optimization, and deduplication
"""

import hashlib
import base64
from io import BytesIO
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

from fastapi import UploadFile
from PIL import Image
from sqlalchemy.orm import Session

from app.constants.endpoints import EndpointUrls
from app.database.models.mcp_server import MCPServer
from app.core.logging_system import BrutorLogger


class IconManager:
    """Service for managing server icons in database"""

    # Supported formats
    SUPPORTED_FORMATS = ['PNG', 'JPEG', 'GIF', 'WEBP', 'ICO']
    SUPPORTED_MIME_TYPES = {
        'image/png': 'PNG',
        'image/jpeg': 'JPEG',
        'image/jpg': 'JPEG',
        'image/gif': 'GIF',
        'image/webp': 'WEBP',
        'image/x-icon': 'ICO',
        'image/vnd.microsoft.icon': 'ICO'
    }

    # Size limits
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
    MAX_DIMENSION = 2048  # Max width/height
    STANDARD_SIZE = 512  # Standard size for optimization

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("icon-manager")

    async def save_icon_from_upload(
        self,
        server_id: str,
        file: UploadFile,
        db: Session,
        optimize: bool = True
    ) -> Dict[str, Any]:
        """
        Save icon from uploaded file

        Args:
            server_id: Server ID
            file: Uploaded file
            db: Database session
            optimize: Whether to optimize/resize the image

        Returns:
            Icon metadata dict

        Raises:
            ValueError: If validation fails
        """
        try:
            # Read file data
            icon_data = await file.read()

            # Validate size
            if len(icon_data) > self.MAX_FILE_SIZE:
                raise ValueError(
                    f"Icon file too large. Maximum size is {self.MAX_FILE_SIZE / 1024 / 1024:.1f}MB"
                )

            # Detect and validate MIME type
            mime_type = file.content_type or self._detect_mime_type(icon_data)
            if mime_type not in self.SUPPORTED_MIME_TYPES:
                raise ValueError(
                    f"Unsupported image format. Supported: {', '.join(self.SUPPORTED_MIME_TYPES.keys())}"
                )

            # Process image
            if optimize:
                icon_data, mime_type = self._optimize_image(icon_data, mime_type)
            else:
                # Still validate it's a valid image
                self._validate_image(icon_data)

            # Calculate hash for deduplication
            icon_hash = hashlib.sha256(icon_data).hexdigest()

            # Get or create server
            server = db.query(MCPServer).filter(MCPServer.id == server_id).first()
            if not server:
                raise ValueError(f"Server {server_id} not found")

            # Update server with icon
            server.icon_data = icon_data
            server.icon_mime_type = mime_type
            server.icon_size = len(icon_data)
            server.icon_hash = icon_hash
            server.icon_uploaded_at = datetime.now(timezone.utc)

            db.flush()

            self.logger.debug("Icon saved to database", context={
                "server_id": server_id,
                "mime_type": mime_type,
                "size": len(icon_data),
                "hash": icon_hash[:16],
                "optimized": optimize
            })

            # Return icon info with correct type
            return {
                "type": "file",
                "mime_type": mime_type,
                "size": len(icon_data),
                "hash": icon_hash,
                "uploaded_at": server.icon_uploaded_at.isoformat() + "Z",
                "url": f"{EndpointUrls.MCP_SERVER_ADMIN.value}/{server_id}/icon"
            }

        except Exception as e:
            self.logger.error("Failed to save icon", context={
                "server_id": server_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def save_icon_from_url(
        self,
        server_id: str,
        url: str,
        db: Session,
        optimize: bool = True
    ) -> Dict[str, Any]:
        """
        Download and save icon from URL

        Args:
            server_id: Server ID
            url: Icon URL
            db: Database session
            optimize: Whether to optimize the image

        Returns:
            Icon metadata dict
        """
        from app.services.outbound_http import outbound_async_client

        try:
            async with outbound_async_client(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()

                icon_data = response.content
                mime_type = response.headers.get('content-type', 'image/png')

                # Validate size
                if len(icon_data) > self.MAX_FILE_SIZE:
                    raise ValueError("Downloaded icon too large")

                # Process and save
                if optimize:
                    icon_data, mime_type = self._optimize_image(icon_data, mime_type)
                else:
                    self._validate_image(icon_data)

                # Calculate hash
                icon_hash = hashlib.sha256(icon_data).hexdigest()

                # Save to database
                server = db.query(MCPServer).filter(MCPServer.id == server_id).first()
                if not server:
                    raise ValueError(f"Server {server_id} not found")

                server.icon_data = icon_data
                server.icon_mime_type = mime_type
                server.icon_size = len(icon_data)
                server.icon_hash = icon_hash
                server.icon_uploaded_at = datetime.now(timezone.utc)

                db.flush()

                self.logger.debug("Icon downloaded and saved", context={
                    "server_id": server_id,
                    "source_url": url,
                    "size": len(icon_data)
                })

                return server.get_icon_info()

        except Exception as e:
            self.logger.error("Failed to download icon", context={
                "server_id": server_id,
                "url": url,
                "error": str(e)
            }, exc_info=True)
            raise

    async def save_icon_from_base64(
        self,
        server_id: str,
        base64_data: str,
        db: Session,
        optimize: bool = True
    ) -> Dict[str, Any]:
        """
        Save icon from base64 encoded data

        Args:
            server_id: Server ID
            base64_data: Base64 encoded image (with or without data URI prefix)
            db: Database session
            optimize: Whether to optimize the image

        Returns:
            Icon metadata dict
        """
        try:
            # Remove data URI prefix if present
            if ',' in base64_data and base64_data.startswith('data:'):
                base64_data = base64_data.split(',', 1)[1]

            # Decode base64
            icon_data = base64.b64decode(base64_data)

            # Detect MIME type
            mime_type = self._detect_mime_type(icon_data)

            # Validate and process
            if optimize:
                icon_data, mime_type = self._optimize_image(icon_data, mime_type)
            else:
                self._validate_image(icon_data)

            # Calculate hash
            icon_hash = hashlib.sha256(icon_data).hexdigest()

            # Save to database
            server = db.query(MCPServer).filter(MCPServer.id == server_id).first()
            if not server:
                raise ValueError(f"Server {server_id} not found")

            server.icon_data = icon_data
            server.icon_mime_type = mime_type
            server.icon_size = len(icon_data)
            server.icon_hash = icon_hash
            server.icon_uploaded_at = datetime.now(timezone.utc)

            db.flush()

            self.logger.debug("Icon saved from base64", context={
                "server_id": server_id,
                "size": len(icon_data)
            })

            return server.get_icon_info()

        except Exception as e:
            self.logger.error("Failed to save base64 icon", context={
                "server_id": server_id,
                "error": str(e)
            }, exc_info=True)
            raise

    def get_icon(
        self,
        server_id: str,
        tenant_id: str,
        db: Session
    ) -> Optional[Tuple[bytes, str]]:
        """
        Get icon data for a server with tenant isolation

        Args:
            server_id: Server ID
            tenant_id: Tenant ID (for multi-tenant isolation)
            db: Database session

        Returns:
            Tuple of (icon_data, mime_type) or None if no icon
        """
        server = db.query(MCPServer).filter(
            MCPServer.id == server_id,
            MCPServer.tenant_id == tenant_id
        ).first()

        if not server or not server.has_icon():
            return None

        return (server.icon_data, server.icon_mime_type)

    def delete_icon(
        self,
        server_id: str,
        tenant_id: str,
        db: Session
    ) -> bool:
        """
        Delete icon for a server with tenant isolation
        (without deleting the server itself)

        This is used by the DELETE /servers/{server_id}/icon endpoint
        to remove just the icon while keeping the server configuration.

        Args:
            server_id: Server ID
            tenant_id: Tenant ID (for multi-tenant isolation)
            db: Database session

        Returns:
            True if deleted, False if no icon existed
        """
        server = db.query(MCPServer).filter(
            MCPServer.id == server_id,
            MCPServer.tenant_id == tenant_id
        ).first()

        if not server or not server.has_icon():
            return False

        server.icon_data = None
        server.icon_mime_type = None
        server.icon_size = None
        server.icon_hash = None
        server.icon_uploaded_at = None

        db.flush()

        self.logger.debug("Icon deleted", context={
            "server_id": server_id,
            "tenant_id": tenant_id
        })

        return True

    def find_duplicate_icons(
        self,
        tenant_id: str,
        db: Session
    ) -> Dict[str, list]:
        """
        Find servers with duplicate icons (same hash) within a tenant

        Args:
            tenant_id: Tenant ID (for multi-tenant isolation)
            db: Database session

        Returns:
            Dict mapping icon hash to list of server IDs
        """
        from sqlalchemy import func

        # Find hashes that appear more than once within this tenant
        duplicates = db.query(
            MCPServer.icon_hash,
            func.count(MCPServer.id).label('count')
        ).filter(
            MCPServer.tenant_id == tenant_id,
            MCPServer.icon_hash.isnot(None)
        ).group_by(
            MCPServer.icon_hash
        ).having(
            func.count(MCPServer.id) > 1
        ).all()

        result = {}
        for icon_hash, count in duplicates:
            servers = db.query(MCPServer).filter(
                MCPServer.tenant_id == tenant_id,
                MCPServer.icon_hash == icon_hash
            ).all()
            result[icon_hash] = [s.id for s in servers]

        return result

    # Private helper methods

    def _detect_mime_type(self, data: bytes) -> str:
        """Detect MIME type from image data"""
        if data.startswith(b'\x89PNG'):
            return 'image/png'
        elif data.startswith(b'\xff\xd8\xff'):
            return 'image/jpeg'
        elif data.startswith(b'GIF8'):
            return 'image/gif'
        elif data.startswith(b'RIFF') and b'WEBP' in data[:12]:
            return 'image/webp'
        elif data.startswith(b'\x00\x00\x01\x00'):
            return 'image/x-icon'
        else:
            raise ValueError("Unable to detect image format")

    def _validate_image(self, data: bytes) -> None:
        """Validate image can be opened and is reasonable size"""
        try:
            img = Image.open(BytesIO(data))

            # Check dimensions
            if img.width > self.MAX_DIMENSION or img.height > self.MAX_DIMENSION:
                raise ValueError(
                    f"Image dimensions too large. Maximum is {self.MAX_DIMENSION}x{self.MAX_DIMENSION}"
                )

            # Verify format
            if img.format not in self.SUPPORTED_FORMATS:
                raise ValueError(f"Unsupported format: {img.format}")

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"Invalid or corrupted image: {str(e)}")

    def _optimize_image(
        self,
        data: bytes,
        mime_type: str
    ) -> Tuple[bytes, str]:
        """
        Optimize image: resize if too large, convert to efficient format

        Args:
            data: Original image data
            mime_type: Original MIME type

        Returns:
            Tuple of (optimized_data, mime_type)
        """
        try:
            img = Image.open(BytesIO(data))

            # Convert RGBA to RGB if saving as JPEG
            if img.mode == 'RGBA' and mime_type == 'image/jpeg':
                # Create white background
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])  # Use alpha channel as mask
                img = background

            # Resize if too large
            if img.width > self.STANDARD_SIZE or img.height > self.STANDARD_SIZE:
                img.thumbnail((self.STANDARD_SIZE, self.STANDARD_SIZE), Image.Resampling.LANCZOS)
                self.logger.debug("Image resized", context={
                    "original_size": f"{img.width}x{img.height}",
                    "new_size": f"{img.width}x{img.height}"
                })

            # Save optimized
            output = BytesIO()

            # Prefer PNG for transparency, JPEG for photos
            if img.mode in ('RGBA', 'LA', 'P'):
                img.save(output, format='PNG', optimize=True)
                mime_type = 'image/png'
            else:
                img.save(output, format='JPEG', quality=85, optimize=True)
                mime_type = 'image/jpeg'

            optimized_data = output.getvalue()

            self.logger.debug("Image optimized", context={
                "original_size": len(data),
                "optimized_size": len(optimized_data),
                "savings": f"{(1 - len(optimized_data)/len(data)) * 100:.1f}%"
            })

            return optimized_data, mime_type

        except Exception as e:
            # Downstream-class failure: the upstream icon URL handed us
            # bytes Pillow can't decode (often an HTML error page from a
            # broken icon link). We already have a working fallback —
            # ship the original bytes — so log a clean one-liner with
            # `exc_info=False` to suppress BrutorLogger's auto-traceback
            # capture. The traceback was burying real errors in the
            # control-plane log on every catalog import.
            self.logger.warning(
                "Image optimization failed, using original",
                context={"error": str(e), "mime_type": mime_type, "bytes": len(data)},
                exc_info=False,
            )
            # Return original if optimization fails
            return data, mime_type