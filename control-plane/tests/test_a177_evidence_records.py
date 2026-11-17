# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a177_evidence_records migration drift suite — evidence records

Fresh installs build every table from the ORM (create_all + stamp) and
never run this migration; upgraded installs get the tables only from it.
The two must agree on every table, on `proxy_logs.record_id`, and on the
framework registry seed — the migration inserts it for upgrades, the boot
seed inserts it for fresh installs, and a row present in one and absent
in the other is a framework a tenant can enable on one kind of install
and not the other.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app.database.models  # noqa: F401 — registers every model on Base
from app.database import Base
from app.database.models.compliance_framework import FRAMEWORK_SEED
from app.database.models.proxy_log import ProxyLog


TABLES = [
    "action_records",
    "evidence_logs",
    "evidence_log_nodes",
    "evidence_log_entries",
    "evidence_tree_heads",
    "evidence_receipts",
    "witnesses",
    "disclosure_records",
    "compliance_frameworks",
    "ai_system_compliance_profiles",
]


SEED_IDS = ["eu-ai-act", "iso-42001", "soc2", "hipaa", "gdpr", "dora", "nis2", "nist-ai-rmf"]


class TestSeedParity:
    """The model's FRAMEWORK_SEED: the seeded frameworks, in their order."""

    def test_seed_ids_are_the_expected_eight(self):
        assert [r["id"] for r in FRAMEWORK_SEED][: len(SEED_IDS)] == SEED_IDS

    def test_tagging_frameworks_carry_their_a070_key(self):
        from app.api.v1.models.admin.compliance import FRAMEWORK_KEYS

        keyed = {r["tag_key"] for r in FRAMEWORK_SEED if r["tag_key"]}
        assert keyed == FRAMEWORK_KEYS, "registry tag keys drifted from the tagging engine's"


class TestModelParity:
    @pytest.mark.parametrize("table", TABLES)
    def test_each_table_is_on_base_metadata(self, table):
        assert table in Base.metadata.tables, "fresh installs build tables from the model"

    def test_proxy_log_declares_record_id(self):
        col = ProxyLog.__table__.c["record_id"]
        assert col.type.length == 64
        assert col.nullable
        names = {i.name for i in ProxyLog.__table__.indexes}
        assert "ix_proxy_logs_record_id" in names

    def test_proxy_log_to_dict_exposes_record_id(self):
        row = MagicMock(spec=ProxyLog)
        row.record_id = "abc123"
        assert ProxyLog.to_dict(row)["record_id"] == "abc123"

    def test_index_column_name_is_the_orm_attribute(self):
        nodes = Base.metadata.tables["evidence_log_nodes"]
        assert {c.name for c in nodes.primary_key.columns} == {"log_id", "level", "index"}
        assert nodes.primary_key.name == "pk_evidence_log_nodes"

    def test_bytea_columns_never_dump_raw_bytes(self):
        """to_dict() on every model with a LargeBinary column returns base64."""
        from sqlalchemy import LargeBinary

        from app.database.models.action_record import ActionRecord
        from app.database.models.disclosure_record import DisclosureRecord
        from app.database.models.evidence_log import EvidenceReceipt, EvidenceTreeHead

        for model in (ActionRecord, EvidenceTreeHead, EvidenceReceipt, DisclosureRecord):
            binary = [c.name for c in model.__table__.columns if isinstance(c.type, LargeBinary)]
            assert binary, f"{model.__name__} has no BYTEA column?"
            # A transient instance — no session, no flush, no validators
            # fired on the vocabulary columns we leave unset.
            row = model(**{name: b"\x00\x01" for name in binary})
            d = row.to_dict()
            for value in d.values():
                assert not isinstance(value, (bytes, bytearray)), f"{model.__name__}.to_dict leaks bytes"
            for name in binary:
                assert d[f"{name}_b64"] == "AAE="
