# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The audit chain's one row format: field lists, dispatch, column guard

The Rust core seals `proxy_logs` rows; the control plane carries the
reference verifier for evidence that leaves the database
(`app/services/audit_chain_canonical.py`). Two implementations of one byte
format are only safe if they cannot drift quietly. The verifier computes the
one format this release writes (`v5`, whose scalar part is the field order
a188_audit_chain_v3 introduced as `V3_FIELDS`); a row that names any other format, or none,
does not verify. This suite pins:

* **The lists** — the scalar field order, the exclusion map and the version
  constants are parsed out of the core's `audit_chain.rs` and compared; the
  core and this module declare the same single known format and no other.
* **Dispatch** — a row labelled v1, v2, v3, anything else or nothing does not
  verify, whatever its hash; the agent-implementation fields a188_audit_chain_v3 brought into the chain
  break a row when edited. (The fixed vectors are in
  `tests/test_audit_chain_v5.py`.)
* **The class guard** — every `proxy_logs` ORM column is either covered by
  the format or on the exclusion list with a reason, so the next new column
  cannot land outside the chain without a recorded decision. And no code path that rewrites stored rows (retention,
  erasure, evidence backfill) may touch a covered column, or it would break
  the chain it runs over.
* **Exported rows verify** on their own.
"""

from __future__ import annotations

import ast
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database.models.proxy_log import ProxyLog
from app.services import audit_chain_canonical as canon
from app.services.audit_chain_canonical import (
    CANONICAL_VERSION,
    CHAIN_EXCLUDED_COLUMNS,
    GENESIS_HASH,
    V3_FIELDS,
    V5_FIELDS,
    hash_row,
    row_matches,
    verify_rows,
)

from tests.core_repo import core_repo_path


@pytest.fixture(autouse=True)
def _rows_are_exported_as_soon_as_they_are_written(monkeypatch):
    """A run leaves the newest moments for the next one
    (`tests/test_audit_export_service.py`); these tests export what they
    have just written."""
    from app.services.audit_export import service as export_service

    monkeypatch.setattr(export_service, "COMMIT_LAG_SECONDS", 0)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
# The core is where BRUTOR_CORE_REPO says (`tests/core_repo.py`): not set,
# the tests that read it are skipped; set and absent, they fail.

VECTOR_PREV = "1" * 64


def _ts(micros: int) -> datetime:
    return datetime(2026, 9, 27, 14, 3, 7, micros, tzinfo=timezone.utc)


def vector_full() -> dict:
    """Mirror of `audit_chain::tests::vector_full` — every v3 field present,
    `error_type` the EMPTY string, `mcp_capability_name` non-ASCII."""
    return dict(
        id="01K6V3VECTOR00000000000FULL",
        tenant_id="tenant-vec",
        timestamp=_ts(123456),
        proxy_type="mcp",
        proxy_subtype="mcp_jsonrpc",
        actor_agent_id="agent-support",
        subject_id="u-4471",
        subject_kind="user",
        api_key_id="key-1",
        resource_group_id="sys-support",
        http_method="POST",
        endpoint="/mcp/srv-crm",
        mcp_capability_name="issue_refund_ü",
        llm_model_used="gpt-5.2",
        agent_authz_decision="approval_required",
        agent_grant_id="grant-refunds",
        semantic_policy_id="sp-1",
        argument_policy_id="ap-1",
        guardrail_config_id="gr-1",
        guardrail_check="pii",
        guardrail_action="redact",
        autonomy_level="approval_required",
        autonomy_provenance="live",
        consumed_approval_request_id="appr-1",
        guardrail_blocked=False,
        argument_policy_denied=False,
        http_status_code=200,
        success=True,
        root_task_id="run-1",
        turn_id="turn-3",
        request_content_hash="9f" * 32,
        response_content_hash="60" * 32,
        server_version="2.4.1",
        tool_definition_hash="46" * 32,
        source="gateway",
        target_url="https://crm.example.com/mcp?tenant=acme",
        server_id="srv-crm",
        idp_server_id="idp-1",
        mcp_method="tools/call",
        skill_id="skill-1",
        skill_execution_id="sx-1",
        llm_provider="openai",
        llm_model_requested="gpt-5",
        oauth_flow_type="client_credentials",
        oauth_scope="crm.read crm.write",
        rag_collection_ids="kb-1,kb-2",
        prompt_fingerprint="ab" * 32,
        gateway_user_id="gu-1",
        idp_user_id="idp-u-1",
        is_auth_event=False,
        agent_impl_name="support-copilot",
        agent_impl_version="1.4.2",
        agent_impl_build="sha256:" + "cd" * 32,
        agent_impl_source="header",
        client_label="python-httpx",
        pii_detected=True,
        region_routed_to="eu-west-1",
        residency_class="allowed",
        error_type="",
        upstream_request_id="req-up-1",
        a2a_task_state="completed",
        current_task_id="task-2",
        parent_task_id="task-1",
        delegation_depth=1,
        trace_root_kind="agent_invocation",
        trace_continuity="client_asserted",
        run_end_state="completed",
        deciding_model="gpt-5.2",
    )


def vector_sparse(micros: int) -> dict:
    """Mirror of `audit_chain::tests::vector_sparse` — every optional field
    NULL (absent), every flag false, no status code."""
    return dict(
        id="01K6V3VECTOR0000000000SPARSE",
        tenant_id="tenant-vec",
        timestamp=_ts(micros),
        proxy_type="llm",
        http_method="POST",
        endpoint="/v1/chat/completions",
        source="otel",
        guardrail_blocked=False,
        argument_policy_denied=False,
        success=False,
        is_auth_event=False,
        pii_detected=False,
    )


# ---------------------------------------------------------------------------
# Tamper behaviour and version dispatch
# ---------------------------------------------------------------------------

A188_NAMED = (
    "agent_impl_name",
    "agent_impl_version",
    "agent_impl_build",
    "agent_impl_source",
    "client_label",
    "deciding_model",
)

#: Labels a row may carry that are not the format this release writes: the
#: formats earlier releases sealed, an unknown one, an empty one, none.
OTHER_LABELS = ["v1", "v2", "v3", "v9", "", None]


class TestTamper:
    @pytest.mark.parametrize("name", A188_NAMED)
    @pytest.mark.parametrize("edit", ["changed", None])
    def test_the_agent_implementation_fields_break_a_row(self, name, edit):
        row = vector_full()
        sealed = hash_row(row, GENESIS_HASH)
        assert not row_matches(dict(row, **{name: edit}), CANONICAL_VERSION, GENESIS_HASH, sealed), name

    @pytest.mark.parametrize("name", V3_FIELDS)
    def test_every_scalar_field_is_bound(self, name):
        row = vector_full()
        sealed = hash_row(row, GENESIS_HASH)
        value = row[name]
        if isinstance(value, bool):
            changed = not value
        elif isinstance(value, int):
            changed = value + 1
        elif isinstance(value, datetime):
            changed = value.replace(microsecond=value.microsecond + 1)
        else:
            changed = value + "~"
        assert hash_row(dict(row, **{name: changed}), GENESIS_HASH) != sealed

    def test_null_is_told_from_the_empty_string(self):
        row = vector_full()  # error_type == ""
        assert hash_row(row, GENESIS_HASH) != hash_row(dict(row, error_type=None), GENESIS_HASH)

    def test_an_iso_string_timestamp_hashes_like_the_datetime(self):
        # Exported rows carry the timestamp as ISO-8601 text.
        row = vector_full()
        sealed = hash_row(row, GENESIS_HASH)
        assert hash_row(dict(row, timestamp="2026-09-27T14:03:07.123456+00:00"), GENESIS_HASH) == sealed
        assert hash_row(dict(row, timestamp="2026-09-27T14:03:07.123456Z"), GENESIS_HASH) == sealed

    @pytest.mark.parametrize("label", OTHER_LABELS)
    def test_the_verifier_computes_one_format_and_no_other(self, label):
        # Same as the core's `the_verifier_computes_one_format_and_no_other`:
        # a row that names another format, or none, does not verify, even
        # with the hash this format gives it. The verifier never guesses.
        row = vector_full()
        sealed = hash_row(row, GENESIS_HASH)
        assert row_matches(row, CANONICAL_VERSION, GENESIS_HASH, sealed)
        assert not row_matches(row, label, GENESIS_HASH, sealed), f"a row labelled {label!r} verified"

    def test_the_right_label_does_not_make_another_hash_pass(self):
        row = vector_full()
        sealed = hash_row(row, GENESIS_HASH)
        assert not row_matches(row, CANONICAL_VERSION, GENESIS_HASH, GENESIS_HASH)
        assert not row_matches(row, CANONICAL_VERSION, VECTOR_PREV, sealed)


def _seal(rows, chain_id="chain-x", start=0, prev=GENESIS_HASH, label=CANONICAL_VERSION):
    """Seal rows the way the writer does; `label` is what each row's
    `chain_version` says (the writer always says the current format)."""
    out = []
    for i, row in enumerate(rows):
        h = hash_row(row, prev)
        out.append(
            dict(
                row,
                chain_id=chain_id,
                chain_seq=start + i,
                prev_hash=prev,
                row_hash=h,
                chain_version=label,
            )
        )
        prev = h
    return out


class TestChains:
    def test_a_sealed_chain_verifies(self):
        sealed = _seal([dict(vector_full(), id=f"r{i}") for i in range(4)])
        report = verify_rows(sealed)
        assert report.ok, report
        assert report.rows_checked == 4 and report.links_checked == 3
        assert report.versions == {CANONICAL_VERSION: 4}

    @pytest.mark.parametrize("label", OTHER_LABELS)
    def test_a_row_labelled_with_another_format_fails(self, label):
        sealed = _seal([dict(vector_full(), id=f"r{i}") for i in range(3)])
        sealed[1]["chain_version"] = label
        report = verify_rows(sealed)
        assert report.rows_bad == ["r1"]
        assert report.links_bad == []

    def test_an_agent_implementation_edit_is_caught(self):
        sealed = _seal([dict(vector_full(), id=f"r{i}") for i in range(4)])
        sealed[3]["agent_impl_name"] = "forged"
        assert verify_rows(sealed).rows_bad == ["r3"]

    def test_a_relinked_row_is_caught_and_a_gap_is_not_a_link_failure(self):
        sealed = _seal([dict(vector_full(), id=f"r{i}") for i in range(3)])
        # A tenant export skips other tenants' rows: a missing sequence is
        # expected there and not checked as a link.
        assert verify_rows([sealed[0], sealed[2]]).ok
        sealed[2]["prev_hash"] = "f" * 64
        report = verify_rows(sealed)
        assert "r2" in report.rows_bad and "r2" in report.links_bad


# ---------------------------------------------------------------------------
# The lists — parsed out of the core, compared here
# ---------------------------------------------------------------------------


def _rust_block(src: str, decl: str) -> str:
    start = src.index(decl)
    return src[start : src.index("];", start)]


@pytest.fixture(scope="module")
def core_chain_src() -> str:
    chain = core_repo_path() / "src" / "services" / "audit_chain.rs"
    if not chain.exists():
        pytest.fail(f"{chain} is not in the core — list parity cannot run")
    return chain.read_text()


class TestCoreParity:
    def test_v3_field_order_matches(self, core_chain_src):
        rust = re.findall(r'"([a-z0-9_]+)"', _rust_block(core_chain_src, "pub const V3_FIELDS"))
        assert tuple(rust) == V3_FIELDS

    def test_neither_side_declares_a_removed_format(self, core_chain_src):
        # The core computes one row format; so does this verifier. A field
        # list or version constant of an earlier format on either side would
        # be a format one of them still reads.
        for name in ("V1_FIELDS", "V2_ADDED_FIELDS", "CANONICAL_VERSION_V1", "CANONICAL_VERSION_V2", "CANONICAL_VERSION_V3"):
            assert f"pub const {name}" not in core_chain_src, f"the core declares {name}"
            assert not hasattr(canon, name), f"the reference verifier declares {name}"

    def test_exclusion_map_matches(self, core_chain_src):
        block = _rust_block(core_chain_src, "pub const CHAIN_EXCLUDED_COLUMNS")
        rust = dict(re.findall(r'\(\s*"(\w+)",\s*Excluded::(\w+)\s*\)', block))
        assert rust == CHAIN_EXCLUDED_COLUMNS

    def test_versions_match(self, core_chain_src):
        def const(name):
            m = re.search(rf'pub const {name}: &str = "(\w+)";', core_chain_src)
            assert m, name
            return m.group(1)

        assert const("CANONICAL_VERSION") == canon.CANONICAL_VERSION
        known = re.search(r"pub const KNOWN_VERSIONS: &\[&str\] = &\[([^\]]*)\];", core_chain_src)
        assert known, "KNOWN_VERSIONS"
        core_known = tuple(
            const(name.strip()) if not name.strip().startswith('"') else name.strip().strip('"')
            for name in known.group(1).split(",")
            if name.strip()
        )
        assert core_known == canon.KNOWN_VERSIONS == (canon.CANONICAL_VERSION,)


# ---------------------------------------------------------------------------
# The class guard — no column outside the chain without a decision
# ---------------------------------------------------------------------------


class TestColumnCoverageGuard:
    def test_every_orm_column_is_covered_or_excluded_with_a_reason(self):
        columns = {c.name for c in ProxyLog.__table__.c}
        # The current format's coverage (v5 = v3 + the JSON records + the credential fields).
        covered = set(V5_FIELDS)
        excluded = set(CHAIN_EXCLUDED_COLUMNS)
        undecided = columns - covered - excluded
        assert not undecided, (
            f"proxy_logs columns neither in the audit chain (V5_FIELDS) nor on "
            f"CHAIN_EXCLUDED_COLUMNS: {sorted(undecided)}. Decide in BOTH repos: cover "
            f"it (a new canonical version) or exclude it with a reason."
        )
        stale = (covered | excluded) - columns
        assert not stale, f"lists name columns the table does not have: {sorted(stale)}"
        assert not covered & excluded, f"both covered and excluded: {sorted(covered & excluded)}"

    def test_every_exclusion_reason_is_a_known_category(self, core_chain_src):
        enum = re.search(r"pub enum Excluded \{(.*?)\n\}", core_chain_src, re.S)
        assert enum, "Excluded enum not found in the core"
        variants = set(re.findall(r"^\s{4}(\w+),$", enum.group(1), re.M))
        assert len(variants) >= 9
        assert set(CHAIN_EXCLUDED_COLUMNS.values()) <= variants

    def test_the_orm_check_admits_the_format_the_verifier_computes(self):
        # The CHECK admits exactly the format the verifier computes: no row
        # can be stored under a label the verifier would refuse.
        check = next(
            c for c in ProxyLog.__table__.constraints if c.name == "ck_proxy_logs_chain_version"
        )
        admitted = set(re.findall(r"'(v\d+)'", str(check.sqltext)))
        assert admitted == set(canon.KNOWN_VERSIONS)

    def test_no_control_plane_rewrite_touches_a_covered_column(self):
        # Retention and erasure rewrite stored rows. Touching a covered
        # column there would break the chain they run over — exactly the
        # failure the exclusion categories RemovableContent and
        # ErasableIdentifier exist to prevent. Scanned with `ast`, so a new
        # `update(ProxyLog).values(...)` anywhere in app/ is caught.
        touched: dict[str, set] = {}
        for path in python_sources(APP):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "values":
                    continue
                inner = node.func.value
                targets_proxy_log = False
                while isinstance(inner, (ast.Call, ast.Attribute)):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "update"
                        and inner.args
                        and isinstance(inner.args[0], ast.Name)
                        and inner.args[0].id == "ProxyLog"
                    ):
                        targets_proxy_log = True
                        break
                    inner = inner.func if isinstance(inner, ast.Call) else inner.value
                if targets_proxy_log:
                    for kw in node.keywords:
                        assert kw.arg is not None, f"{path}: **kwargs update of ProxyLog cannot be checked"
                        touched.setdefault(kw.arg, set()).add(str(path.relative_to(ROOT)))
            for const in ast.walk(tree):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    for m in re.finditer(r"UPDATE\s+proxy_logs\s+(?:\w+\s+)?SET\s+(.*?)(?:WHERE|$)", const.value, re.I | re.S):
                        for col in re.findall(r"(\w+)\s*=", m.group(1)):
                            touched.setdefault(col, set()).add(str(path.relative_to(ROOT)))
        assert touched, "the scan found no ProxyLog rewrite at all — it is not looking in the right place"
        assert {"request_body_preview", "erased_at"} <= set(touched), "retention/erasure not seen by the scan"
        bad = {c: sorted(p) for c, p in touched.items() if c in V5_FIELDS}
        assert not bad, f"code rewrites columns inside the audit chain: {bad}"

    def test_no_core_rewrite_touches_a_covered_column(self):
        touched = {}
        for path in (core_repo_path() / "src").rglob("*.rs"):
            for m in re.finditer(r"UPDATE\s+proxy_logs\s+(?:\w+\s+)?SET\s+(.*?)(?:FROM|WHERE|\")", path.read_text(), re.S):
                for col in re.findall(r"(\w+)\s*=", m.group(1)):
                    touched.setdefault(col, set()).add(path.name)
        assert "record_id" in touched, "the scan no longer sees the evidence backfill"
        bad = {c: sorted(p) for c, p in touched.items() if c in V5_FIELDS}
        assert not bad, f"the core rewrites columns inside the audit chain: {bad}"


# ---------------------------------------------------------------------------
# Exported rows verify on their own
# ---------------------------------------------------------------------------

from tests.test_audit_export_service import (  # noqa: E402,F401 — fixtures
    DEST_TYPE_S3,
    _make_destination,
    captured_s3,
    captured_webhook,
    db,
    encryption,
    service_with_stubs,
)
from app.services.audit_export.service import AuditExportService  # noqa: E402
from tests.source_trees import python_sources


def _insert_sealed(db, rows):
    for r in rows:
        db.add(ProxyLog(end_user_session_id="sess", **r))
    db.flush()


class TestExportedRowsVerify:
    def _rows(self, n=3):
        # Safely in the past: replacing the microseconds of "now" could put
        # the row up to a second in the future, outside the export window.
        now = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(microsecond=123456)
        out = []
        for i in range(n):
            r = vector_full()
            r.update(id=f"01K6EXPORT{i:016d}", tenant_id="test-tenant", timestamp=now)
            out.append(r)
        return _seal(out, chain_id="chain-export")

    def _export(self, db, service_with_stubs, captured_s3, allowlist=None):
        dest = _make_destination(db, destination_type=DEST_TYPE_S3, field_allowlist=allowlist)
        AuditExportService(db=db, encryption_service=service_with_stubs).run_destination(dest.id)
        artifact, manifest = captured_s3["calls"][0], captured_s3["calls"][1]
        lines = gzip.decompress(artifact["payload"]).decode().splitlines()
        return [json.loads(line) for line in lines[1:]], json.loads(manifest["payload"])

    def test_an_unfiltered_export_carries_verifiable_chain_blocks(
        self, db, service_with_stubs, captured_s3, captured_webhook
    ):
        _insert_sealed(db, self._rows())
        db.commit()
        rows, manifest = self._export(db, service_with_stubs, captured_s3)
        assert manifest["chain_columns_included"] is True
        blocks = [dict(r["chain"]["fields"], **{k: r["chain"][k] for k in canon.CHAIN_COLUMNS}) for r in rows]
        report = verify_rows(blocks)
        assert report.ok, report
        assert report.rows_checked == 3 and report.links_checked == 2
        assert report.versions == {CANONICAL_VERSION: 3}
        assert all(set(V5_FIELDS) <= set(b) for b in blocks), "a chain block carries every covered field"

        # Editing an exported agent-implementation field is caught offline.
        blocks[1]["client_label"] = "browser"
        assert not verify_rows(blocks).ok

    def test_an_allowlist_without_the_chain_block_is_flagged(
        self, db, service_with_stubs, captured_s3, captured_webhook
    ):
        _insert_sealed(db, self._rows(1))
        db.commit()
        rows, manifest = self._export(db, service_with_stubs, captured_s3, ["id", "tenant_id"])
        assert manifest["chain_columns_included"] is False
        assert "chain" not in rows[0]

    def test_an_allowlist_may_keep_the_chain_block(
        self, db, service_with_stubs, captured_s3, captured_webhook
    ):
        _insert_sealed(db, self._rows(1))
        db.commit()
        rows, manifest = self._export(db, service_with_stubs, captured_s3, ["id", "chain"])
        assert manifest["chain_columns_included"] is True
        assert set(rows[0]) == {"id", "chain"}
