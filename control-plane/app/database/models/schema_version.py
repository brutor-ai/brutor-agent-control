# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Schema Version Tracking

Single-row table that records which application version created or last
initialized the database schema. Updated on every startup so operators
can see at a glance whether the running code matches the DB.
"""

from sqlalchemy import Column, String, DateTime, Integer
from sqlalchemy.sql import func

from app.database import Base


class SchemaVersion(Base):
    """Tracks the application version that created the DB schema and the currently running version."""

    __tablename__ = "schema_version"

    id = Column(Integer, primary_key=True, default=1)
    schema_version = Column(String(50), nullable=False,
                            comment="App version that originally created the schema (set once, never changed)")
    app_version = Column(String(50), nullable=False,
                         comment="App version of the currently running container (updated on every startup)")
    schema_created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                               comment="When the schema was first created")
    app_started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                            comment="When the current app version last started")
