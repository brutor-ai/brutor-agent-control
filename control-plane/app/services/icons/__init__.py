# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Generic icon manager for any DB entity that carries the standard
icon storage columns (`icon_data`, `icon_mime_type`, `icon_size`,
`icon_hash`, `icon_uploaded_at`).

Extracted so the same code paths serve icons for:

  * `mcp_servers` (existing — see
    `app/services/mcp/server/icon_manager.py`; that one stays as
    a wrapper because it needs PIL re-encoding for raster
    optimisation, which we don't want to force on SVG paths)
  * `llm_providers` (`LlmProviderIconManager`)
  * `agent_skills` (planned)
  * `idp_servers` (planned)
  * any future entity that adopts the five-column shape

Design constraints
------------------
The manager is *generic* over the SQLAlchemy mapped class. Each
entity that wants icon support:

  1. Adds the five icon columns to its table (mirror the migration
     pattern from `a028_llm_provider_icon_columns.py`).
  2. Instantiates `EntityIconManager(model_class=…)`.
  3. Wires the resulting GET / POST / DELETE endpoints into its
     admin router.

The Rust counterpart (`routes/icons.rs::serve_entity_icon`)
mirrors this — a single helper that takes the table name and an
id, with a cache-prefix to keep IDs from different entities from
colliding in the in-memory ETag cache.

What this base does NOT do
--------------------------
*Optimisation* — re-encoding raster icons is the MCP IconManager's
specialty (it pulls them down from arbitrary URLs and wants a
size-bounded blob). For provider / skill / IdP logos the operator
uploads exactly what they want; we honour the bytes as-is.
Subclasses can add an `_optimize` step in their `_save` override
if they need it.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Iterable, Optional, Type

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger


class EntityIconNotFound(LookupError):
    """Raised when the entity row referenced by the manager doesn't exist."""


class EntityIconValidationError(ValueError):
    """Raised on size / mime validation failures.

    The endpoint layer should map this to HTTP 400.
    """


# Default mime allowlist — covers the formats most operators want
# to upload (SVG for vector logos, PNG/JPEG for photos, GIF/WEBP/
# ICO for legacy assets). Subclasses override via the constructor.
DEFAULT_SUPPORTED_MIME_TYPES = frozenset({
    "image/svg+xml",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/x-icon",
    "image/vnd.microsoft.icon",
})

DEFAULT_MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MiB


class EntityIconManager:
    """Save / fetch / delete icons on any model with the icon columns.

    Usage (subclass-free path)::

        from app.database.models.llm_provider import LlmProvider
        mgr = EntityIconManager(model_class=LlmProvider)
        await mgr.save_icon_from_upload(provider_id, file, db)

    Subclassing is only useful when an entity needs its own
    validation / optimisation; see `LlmProviderIconManager` for
    a near-empty subclass kept for type-checked discoverability.
    """

    def __init__(
        self,
        *,
        model_class: Type,
        id_field: str = "id",
        logger_name: Optional[str] = None,
        supported_mime_types: Iterable[str] = DEFAULT_SUPPORTED_MIME_TYPES,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        logger: Optional[BrutorLogger] = None,
    ) -> None:
        self.model_class = model_class
        self.id_field = id_field
        self.supported_mime_types = frozenset(supported_mime_types)
        self.max_file_size = max_file_size
        self.logger = logger or BrutorLogger(
            logger_name or f"icon-manager.{model_class.__tablename__}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def save_icon_from_upload(
        self,
        entity_id: str,
        file: UploadFile,
        db: Session,
    ) -> Dict[str, Any]:
        data = await file.read()
        mime_type = file.content_type or self._sniff_mime(data)
        return self._save(entity_id, data, mime_type, db)

    async def save_icon_from_bytes(
        self,
        entity_id: str,
        data: bytes,
        mime_type: str,
        db: Session,
    ) -> Dict[str, Any]:
        return self._save(entity_id, data, mime_type, db)

    def delete_icon(self, entity_id: str, db: Session) -> bool:
        row = self._fetch_row(entity_id, db)
        if row is None or row.icon_data is None:
            return False
        row.icon_data = None
        row.icon_mime_type = None
        row.icon_size = None
        row.icon_hash = None
        row.icon_uploaded_at = None
        db.flush()
        self.logger.info(
            f"Icon cleared for {self.model_class.__tablename__}",
            context={"entity_id": entity_id},
        )
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _save(
        self,
        entity_id: str,
        data: bytes,
        mime_type: str,
        db: Session,
    ) -> Dict[str, Any]:
        if not data:
            raise EntityIconValidationError("icon body is empty")
        if len(data) > self.max_file_size:
            raise EntityIconValidationError(
                f"icon size {len(data)} bytes exceeds "
                f"{self.max_file_size} byte cap"
            )
        normalized_mime = self._normalize_mime(mime_type)
        if normalized_mime not in self.supported_mime_types:
            raise EntityIconValidationError(
                f"unsupported mime type {mime_type!r}; supported: "
                f"{sorted(self.supported_mime_types)}"
            )

        if normalized_mime == "image/svg+xml":
            self._validate_svg(data)
        else:
            self._validate_raster(data)

        row = self._fetch_row(entity_id, db)
        if row is None:
            raise EntityIconNotFound(
                f"{self.model_class.__tablename__} row "
                f"{entity_id!r} not found"
            )

        digest = hashlib.sha256(data).hexdigest()
        row.icon_data = data
        row.icon_mime_type = normalized_mime
        row.icon_size = len(data)
        row.icon_hash = digest
        row.icon_uploaded_at = datetime.now(timezone.utc)
        db.flush()

        self.logger.info(
            f"Icon saved for {self.model_class.__tablename__}",
            context={
                "entity_id": entity_id,
                "mime_type": normalized_mime,
                "size": len(data),
                "hash": digest[:16],
            },
        )
        return {
            "mime_type": normalized_mime,
            "size": len(data),
            "hash": digest,
            "uploaded_at": row.icon_uploaded_at.isoformat(),
        }

    def _fetch_row(self, entity_id: str, db: Session):
        return (
            db.query(self.model_class)
            .filter(getattr(self.model_class, self.id_field) == entity_id)
            .first()
        )

    @staticmethod
    def _normalize_mime(mime: str) -> str:
        return mime.split(";", 1)[0].strip().lower()

    @staticmethod
    def _sniff_mime(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"\x00\x00\x01\x00"):
            return "image/x-icon"
        head = data[:512].lstrip().lower()
        if head.startswith(b"<?xml") or head.startswith(b"<svg"):
            return "image/svg+xml"
        raise EntityIconValidationError(
            "could not detect icon MIME type from bytes"
        )

    @staticmethod
    def _validate_svg(data: bytes) -> None:
        head = data[:512].lstrip().lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<svg")):
            raise EntityIconValidationError(
                "SVG payload doesn't look like SVG (expected '<svg' "
                "or '<?xml' near the start)"
            )

    @staticmethod
    def _validate_raster(data: bytes) -> None:
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            return
        try:
            Image.open(BytesIO(data)).verify()
        except Exception as exc:  # pragma: no cover
            raise EntityIconValidationError(
                f"corrupt or unreadable image: {exc}"
            ) from exc


__all__ = [
    "EntityIconManager",
    "EntityIconNotFound",
    "EntityIconValidationError",
    "DEFAULT_SUPPORTED_MIME_TYPES",
    "DEFAULT_MAX_FILE_SIZE",
]
