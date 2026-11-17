# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The edition marker (migration `a212_edition_marker`).

One schema serves both editions, so a Community Edition can be started on a
database the Enterprise Edition has used. The Enterprise Edition records that
it has run here: at every start its control plane writes the single row of
this table (`id = 1`), setting the "first" columns once and the "last"
columns each time. The Community Edition never writes the table. At its
start, in the control plane and in the core, it reads the row: a row means
the Enterprise Edition has been here, and the Community Edition refuses to
start (`app/services/edition/guard.py`).

The reset command (`app/services/edition/reset.py`) deletes the row, with the
Enterprise data. The core implements the same check.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String

from app.database import Base


class EditionMarker(Base):
    """The single row that says the Enterprise Edition has started here."""

    __tablename__ = "edition_marker"

    id = Column(Integer, primary_key=True, autoincrement=False)
    enterprise_first_started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When an Enterprise Edition control plane first started on this database (set once)",
    )
    enterprise_last_started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When an Enterprise Edition control plane last started on this database",
    )
    enterprise_first_version = Column(
        String(50), nullable=False, comment="Application version of that first start (set once)"
    )
    enterprise_last_version = Column(
        String(50), nullable=False, comment="Application version of the last start"
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_edition_marker_single_row"),
        {"comment": "Present when the Enterprise Edition has started on this database"},
    )
