# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a190_audit_chain_v4 — audit chain canonical format v4

v4 brings the JSON decision records (`compliance_tags`, `guardrails_skipped`,
`guardrails_degraded`, `pre_action_state`, `custom_metadata`, `tags`) inside
the row hash, each as its RFC 8785 (JSON Canonicalization Scheme) text. The
core (`services/jcs.rs`) and this repo's reference verifier
(`app/services/audit_chain_canonical.py`) implement JCS independently; this
suite pins:

* **RFC 8785's own vectors** — number serialisation (Appendix B), the main
  example, UTF-16 key ordering, string escaping — identical to the core's
  `jcs::tests`, and checks the core still pins every one of them.
* **Brutor row vectors** — the same rows, the same hashes, as the core's
  `audit_chain::tests::fixed_vectors_pin_the_format`. v4 is the one format
  this release writes and the one the verifier computes.
* **Tamper behaviour** — a value change in any JSON column breaks a row;
  re-ordering keys, re-spacing or re-spelling numbers does not; the version
  label cannot make a row verify; exported rows verify offline.
* **The migration** — a190_audit_chain_v4 extends the CHECK to v4, and nothing else.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import struct

import pytest

from app.services import audit_chain_canonical as canon
from app.services.audit_chain_canonical import (
    CHAIN_EXCLUDED_COLUMNS,
    GENESIS_HASH,
    V3_FIELDS,
    V4_ADDED_FIELDS,
    V4_FIELDS,
    CANONICAL_VERSION,
    canonical_row_bytes_v4,
    hash_row,
    jcs_canonicalize,
    jcs_number,
    row_matches,
    verify_rows,
)
from tests.core_repo import core_source
from tests.test_audit_chain_v3 import (  # noqa: F401 — shared vectors and fixtures
    DEST_TYPE_S3,
    VECTOR_PREV,
    _make_destination,
    _seal,
    captured_s3,
    captured_webhook,
    db,
    encryption,
    service_with_stubs,
    vector_full as vector_full_v3,
    vector_sparse,
)


@pytest.fixture(autouse=True)
def _rows_are_exported_as_soon_as_they_are_written(monkeypatch):
    """A run leaves the newest moments for the next one
    (`tests/test_audit_export_service.py`); these tests export what they
    have just written."""
    from app.services.audit_export import service as export_service

    monkeypatch.setattr(export_service, "COMMIT_LAG_SECONDS", 0)

# The core's files these tests read, under its `src/` (`tests/core_repo.py`).
CORE_CHAIN = ("services", "audit_chain.rs")
CORE_JCS = ("services", "jcs.rs")


def _core(parts: tuple) -> str:
    path = core_source(*parts)
    if not path.exists():
        pytest.fail(f"{path} is not in the core — the cross-repo pin cannot run")
    return path.read_text()


# ---------------------------------------------------------------------------
# RFC 8785 vectors — mirrored verbatim in the core's `jcs::tests`
# ---------------------------------------------------------------------------

#: RFC 8785 Appendix B: IEEE-754 bit pattern -> the text JCS requires.
RFC8785_NUMBERS = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]

#: RFC 8785 §3.2.2 — the worked example (input text, canonical text).
RFC8785_MAIN_IN = (
    '{\n  "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],\n'
    '  "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",\n'
    '  "literals": [null, true, false]\n}'
)
RFC8785_MAIN_OUT = (
    '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
    '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
)

#: RFC 8785 §3.2.3 — members sorted by UTF-16 code units: U+1F600
#: (0xD83D 0xDE00) sorts before U+FB33 although its code point is larger.
RFC8785_SORT_IN = (
    '{"\\u20ac": "Euro Sign", "\\r": "Carriage Return", '
    '"\\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One", '
    '"\\ud83d\\ude00": "Emoji: Grinning Face", "\\u0080": "Control", '
    '"\\u00f6": "Latin Small Letter O With Diaeresis"}'
)
RFC8785_SORT_OUT = (
    '{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
    '"ö":"Latin Small Letter O With Diaeresis","€":"Euro Sign",'
    '"\U0001f600":"Emoji: Grinning Face","דּ":"Hebrew Letter Dalet With Dagesh"}'
)


class TestRfc8785:
    @pytest.mark.parametrize(("bits", "want"), RFC8785_NUMBERS)
    def test_number_vectors(self, bits, want):
        assert jcs_number(struct.unpack(">d", bytes.fromhex(bits))[0]) == want

    @pytest.mark.parametrize(("bits", "want"), RFC8785_NUMBERS)
    def test_the_core_pins_the_same_number_vectors(self, bits, want):
        src = _core(CORE_JCS)
        assert f'(0x{bits}, "{want}")' in src, f"core jcs.rs no longer pins {bits} -> {want}"

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 10**400])
    def test_non_finite_numbers_have_no_canonical_form(self, bad):
        with pytest.raises(ValueError):
            jcs_number(bad)

    def test_main_example(self):
        assert jcs_canonicalize(json.loads(RFC8785_MAIN_IN)) == RFC8785_MAIN_OUT

    def test_utf16_key_ordering(self):
        assert jcs_canonicalize(json.loads(RFC8785_SORT_IN)) == RFC8785_SORT_OUT

    def test_string_escaping_follows_json_stringify(self):
        # Same input and output as the core's `string_escaping_follows_json_stringify`.
        value = "\x00\x01\x08\t\n\x0b\x0c\r\x1f\x7f \"\\/é"
        assert jcs_canonicalize(value) == '"\\u0000\\u0001\\b\\t\\n\\u000b\\f\\r\\u001f\x7f \\"\\\\/é"'

    def test_nested_structures_and_literals(self):
        v = {"b": [1, {"z": None, "a": [True, False]}], "a": {}, "c": []}
        assert jcs_canonicalize(v) == '{"a":{},"b":[1,{"a":[true,false],"z":null}],"c":[]}'

    def test_integers_beyond_2_pow_53_serialise_as_doubles(self):
        v = json.loads("[9007199254740993, 18446744073709551615, -1]")
        assert jcs_canonicalize(v) == "[9007199254740992,18446744073709552000,-1]"

    def test_layout_and_number_spelling_do_not_matter(self):
        a = json.loads('{"kind":"llm_quota","used":1.50,"n":10}')
        b = json.loads('{ "n" : 1e1 ,\n "used":1.5, "kind" :"llm_quota" }')
        assert jcs_canonicalize(a) == jcs_canonicalize(b) == '{"kind":"llm_quota","n":10,"used":1.5}'

    @pytest.mark.parametrize(
        "bad",
        ["\ud800", {1: "x"}, {"x": object()}, b"bytes"],
        ids=["lone-surrogate", "non-string-key", "non-json-value", "bytes"],
    )
    def test_non_json_input_is_refused(self, bad):
        with pytest.raises((ValueError, TypeError)):
            jcs_canonicalize(bad)

    def test_booleans_are_not_numbers(self):
        # bool subclasses int in Python; JCS must still print true/false.
        assert jcs_canonicalize([True, 1, False, 0]) == "[true,1,false,0]"
        with pytest.raises(TypeError):
            jcs_number(True)


# ---------------------------------------------------------------------------
# Brutor row vectors — mirrored in the core's `fixed_vectors_pin_the_format`
# ---------------------------------------------------------------------------

#: The JSON documents of the core's `vector_full()`: keys out of order,
#: non-ASCII and escapable text, a key above U+FFFF beside one in
#: U+E000..U+FFFF, and numbers whose ES spelling differs from the literal.
JSON_DOCS = dict(
    compliance_tags=["soc2.cc6.6", "eu_ai_act.high"],
    guardrails_skipped=[{"reason": "detector_timeout", "config": "gr-2", "check": "toxic_content"}],
    guardrails_degraded=[{"tier": "fallback", "config": "gr-1", "check": "pii"}],
    pre_action_state={
        "kind": "llm_quota",
        "window_tokens": 120000,
        "spend_usd": 41.5,
        "ratio": 0.1234,
        "big": 1e21,
        "tiny": 1e-7,
        "flags": {"z": True, "a": None},
    },
    custom_metadata={
        "otel": {"span_name": "tool €", "service_name": 'crm\n"svc"'},
        "conversation_handle": "01K6V4HANDLE0000000000000",
        "\U0001f600": 1,
        "דּ": 2,
    },
    tags={"refusal": "quota", "scope": "tenant", "limits": [1, 2.5, -0.0]},
)


def vector_full() -> dict:
    return dict(vector_full_v3(), **json.loads(json.dumps(JSON_DOCS)))


def vector_sparse_json() -> dict:
    """The documented v4 vector (docs reference/audit-row.mdx)."""
    return dict(vector_sparse(0), pre_action_state=json.loads('{"window":"day","used":1.50,"kind":"llm_quota"}'))


#: The gateway's exact-digits vector: an integer outside the range
#: a double holds exactly is stored by the writer as a string of its digits,
#: and the verifier hashes the stored document character for character.
EXACT_DIGITS = (
    '{"n":"9007199254740993","safe":9007199254740991,"neg":"-9007199254740993",'
    '"list":[1,"18446744073709551615"],"ratio":0.25}'
)


def vector_sparse_exact_digits(n: str = "9007199254740993") -> dict:
    document = json.loads(EXACT_DIGITS)
    document["n"] = n
    return dict(vector_sparse(0), custom_metadata=document)


V4_VECTORS = [
    ("full", GENESIS_HASH, "648db704aad64e13a93ec3862a488b2fa561ffa0bc1ec92832f677b9576f6b78"),
    ("sparse", VECTOR_PREV, "9e96cc27d103dee60a7b4d8df573f76eedffb7f5f6f496f14e4f3abf8fab2557"),
    ("sparse_ms", VECTOR_PREV, "07bc370a29b5638cc61a66cd1a0449409df65fc4f978445391ce09b534933f46"),
    ("sparse_json", VECTOR_PREV, "c18201c95d62f47743a543738a18ddd4dbc97e5eaa99a85daae869ea562c45ff"),
    ("sparse_exact_digits", VECTOR_PREV, "120dbfc18038f31ef6b9841ebae02ff1669b9fa3fe0b5a69f7c830dace0909f9"),
    # One less, the same double: another hash.
    ("sparse_exact_digits_less", VECTOR_PREV, "0d8a3731c74545bd5ba42de20ae19f707bcac7acb472ed1704b5a7bb23d326f0"),
]


def _vector(name: str) -> dict:
    return {
        "full": vector_full,
        "sparse": lambda: vector_sparse(0),
        "sparse_ms": lambda: vector_sparse(250000),
        "sparse_json": vector_sparse_json,
        "sparse_exact_digits": vector_sparse_exact_digits,
        "sparse_exact_digits_less": lambda: vector_sparse_exact_digits("9007199254740992"),
    }[name]()


class TestFixedVectors:
    @pytest.mark.parametrize(("name", "prev", "expected"), V4_VECTORS)
    def test_reference_verifier_matches_the_core(self, name, prev, expected):
        assert hash_row(_vector(name), prev) == expected

    @pytest.mark.parametrize(("name", "prev", "expected"), V4_VECTORS)
    def test_the_core_pins_the_same_vectors(self, name, prev, expected):
        assert expected in _core(CORE_CHAIN), f"the core no longer pins v4 {name} = {expected}"

    def test_the_exact_digits_vector_is_the_gateways_stored_document(self):
        # The row the gateway's test hashes: `vector_sparse(0)` with this
        # stored document, as its source has it.
        core = re.sub(r"\s+", "", _core(CORE_CHAIN))
        assert "fnthe_exact_digits_vector_is_what_the_control_plane_verifier_computes()" in core
        assert EXACT_DIGITS in core
        assert jcs_canonicalize(json.loads(EXACT_DIGITS)) == (
            '{"list":[1,"18446744073709551615"],"n":"9007199254740993","neg":"-9007199254740993",'
            '"ratio":0.25,"safe":9007199254740991}'
        )

    def test_the_json_documents_match_the_core_vector(self):
        # The core writes the same documents with `json!`; pin their
        # canonical text here so a changed vector in either repo is a
        # visible, reviewed edit rather than two hashes quietly moving.
        assert jcs_canonicalize(JSON_DOCS["pre_action_state"]) == (
            '{"big":1e+21,"flags":{"a":null,"z":true},"kind":"llm_quota",'
            '"ratio":0.1234,"spend_usd":41.5,"tiny":1e-7,"window_tokens":120000}'
        )
        assert jcs_canonicalize(JSON_DOCS["custom_metadata"]) == (
            '{"conversation_handle":"01K6V4HANDLE0000000000000",'
            '"otel":{"service_name":"crm\\n\\"svc\\"","span_name":"tool €"},'
            '"\U0001f600":1,"דּ":2}'
        )
        assert jcs_canonicalize(JSON_DOCS["tags"]) == '{"limits":[1,2.5,0],"refusal":"quota","scope":"tenant"}'

    def test_the_v4_layout_is_exactly_as_documented(self):
        present = {
            "id": "01K6V3VECTOR0000000000SPARSE",
            "tenant_id": "tenant-vec",
            "timestamp": "2026-09-27T14:03:07.000000Z",
            "source": "otel",
            "proxy_type": "llm",
            "http_method": "POST",
            "endpoint": "/v1/chat/completions",
            "is_auth_event": "0",
            "guardrail_blocked": "0",
            "argument_policy_denied": "0",
            "pii_detected": "0",
            "success": "0",
            "pre_action_state": '{"kind":"llm_quota","used":1.5,"window":"day"}',
        }

        def field(s: str) -> bytes:
            raw = s.encode()
            return b"\x01" + len(raw).to_bytes(4, "big") + raw

        want = b"v4\x00" + b"".join(
            field(present[n]) if n in present else b"\x00" for n in V3_FIELDS + V4_ADDED_FIELDS
        ) + field(VECTOR_PREV)
        assert canonical_row_bytes_v4(vector_sparse_json(), VECTOR_PREV) == want


# ---------------------------------------------------------------------------
# Tamper behaviour
# ---------------------------------------------------------------------------


def _reordered(value):
    """The same JSON data with every object's keys in reverse order —
    Python dicts keep insertion order, so this is a real re-ordering."""
    if isinstance(value, dict):
        return {k: _reordered(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reordered(v) for v in value]
    return value


class TestTamper:
    @pytest.mark.parametrize("column", V4_ADDED_FIELDS)
    def test_a_value_change_breaks_a_v4_row(self, column):
        row = vector_full()
        v4 = hash_row(row, GENESIS_HASH)
        assert not row_matches(dict(row, **{column: {"forged": column}}), "v4", GENESIS_HASH, v4)
        assert not row_matches(dict(row, **{column: None}), "v4", GENESIS_HASH, v4), "NULLing is an edit"

    @pytest.mark.parametrize("column", V4_ADDED_FIELDS)
    def test_key_reordering_and_relayout_do_not_break_a_v4_row(self, column):
        row = vector_full()
        v4 = hash_row(row, GENESIS_HASH)
        reordered = _reordered(row[column])
        assert row_matches(dict(row, **{column: reordered}), "v4", GENESIS_HASH, v4)
        # Through text with different whitespace and number spelling, as a
        # recipient re-parsing an export would see it.
        relaid = json.loads(json.dumps(reordered, indent=3, ensure_ascii=True))
        assert row_matches(dict(row, **{column: relaid}), "v4", GENESIS_HASH, v4)

    def test_small_edits_inside_a_document_are_detected(self):
        row = vector_full()
        v4 = hash_row(row, GENESIS_HASH)
        for edit in (
            {"pre_action_state": dict(row["pre_action_state"], spend_usd=41.500001)},
            {"pre_action_state": dict(row["pre_action_state"], flags={"z": False, "a": None})},
            {"compliance_tags": list(reversed(row["compliance_tags"]))},
        ):
            assert not row_matches(dict(row, **edit), "v4", GENESIS_HASH, v4), edit

    def test_sql_null_and_a_json_null_document_are_the_same_absence(self):
        # The ORM reads a JSON `null` document back as None; the core treats
        # Some(Value::Null) as absent. Either way the bytes are 0x00.
        row = dict(vector_full(), tags=None)
        assert hash_row(row, GENESIS_HASH) != hash_row(dict(row, tags={}), GENESIS_HASH)

    def test_the_version_label_cannot_make_a_row_verify(self):
        row = vector_full()
        v4 = hash_row(row, GENESIS_HASH)
        assert row_matches(row, CANONICAL_VERSION, GENESIS_HASH, v4)
        for label in ("v3", "v2", "v1", None):
            assert not row_matches(row, label, GENESIS_HASH, v4), label

    def test_a_json_edit_in_a_chain_breaks_that_row(self):
        sealed = _seal([dict(vector_full(), id=f"r{i}") for i in range(3)])
        assert verify_rows(sealed).ok
        sealed[2]["guardrails_skipped"] = []
        assert verify_rows(sealed).rows_bad == ["r2"]


# ---------------------------------------------------------------------------
# Parity with the core and the migration
# ---------------------------------------------------------------------------


class TestCoreParity:
    def test_v4_added_fields_match(self):
        src = _core(CORE_CHAIN)
        start = src.index("pub const V4_ADDED_FIELDS")
        block = src[start : src.index("];", start)]
        assert tuple(re.findall(r'"([a-z0-9_]+)"', block)) == V4_ADDED_FIELDS

    def test_no_json_column_is_left_outside_and_the_category_is_gone(self):
        assert V4_FIELDS == V3_FIELDS + V4_ADDED_FIELDS
        assert not set(V4_ADDED_FIELDS) & set(CHAIN_EXCLUDED_COLUMNS)
        assert "JsonDocument" not in set(CHAIN_EXCLUDED_COLUMNS.values())
        assert "JsonDocument" not in _core(CORE_CHAIN), "the retired exclusion reason is still named in the core"

    def test_every_orm_json_column_is_covered(self):
        # The class guard for this format: a JSON column is either inside the
        # chain (final when sealed) or excluded for a reason that is not
        # "it is JSON" — JCS removed that reason.
        from sqlalchemy import JSON

        from app.database.models.proxy_log import ProxyLog

        json_columns = {c.name for c in ProxyLog.__table__.c if isinstance(c.type, JSON)}
        assert json_columns, "the scan found no JSON column"
        assert json_columns <= set(V4_FIELDS), sorted(json_columns - set(V4_FIELDS))


# ---------------------------------------------------------------------------
# Exported v4 rows verify on their own
# ---------------------------------------------------------------------------


class TestExportedRowsVerify:
    def test_an_export_carries_the_json_records_and_they_verify(
        self, db, service_with_stubs, captured_s3, captured_webhook
    ):
        from datetime import datetime, timedelta, timezone

        from app.database.models.proxy_log import ProxyLog
        from app.services.audit_export.service import AuditExportService

        now = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(microsecond=654321)
        rows = []
        for i in range(3):
            r = vector_full()
            r.update(id=f"01K6EXPORTV4{i:014d}", tenant_id="test-tenant", timestamp=now)
            rows.append(r)
        sealed = _seal(rows, chain_id="chain-export-v4")
        for r in sealed:
            # Stored with keys in a different order than they were hashed in.
            r["pre_action_state"] = _reordered(r["pre_action_state"])
            r["custom_metadata"] = _reordered(r["custom_metadata"])
            db.add(ProxyLog(end_user_session_id="sess", **r))
        db.commit()

        dest = _make_destination(db, destination_type=DEST_TYPE_S3)
        AuditExportService(db=db, encryption_service=service_with_stubs).run_destination(dest.id)
        lines = gzip.decompress(captured_s3["calls"][0]["payload"]).decode().splitlines()
        exported = [json.loads(line) for line in lines[1:]]
        blocks = [dict(r["chain"]["fields"], **{k: r["chain"][k] for k in canon.CHAIN_COLUMNS}) for r in exported]

        for block in blocks:
            assert set(V4_ADDED_FIELDS) <= set(block), "a chain block must carry the JSON records"

        report = verify_rows(blocks)
        assert report.ok, report
        assert report.versions == {CANONICAL_VERSION: 3}

        # Editing an exported decision record is caught offline.
        blocks[1]["guardrails_skipped"] = []
        assert not verify_rows(blocks).ok
