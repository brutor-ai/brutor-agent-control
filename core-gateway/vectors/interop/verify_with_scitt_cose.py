#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Cross-implementation check of the evidence vector corpus
against the upstream ``scitt-cose`` package — a clean-room Python
implementation of RFC 9052 COSE_Sign1, RFC 9943 Signed Statements, RFC 9942
receipts and RFC 6962 / RFC 9162 Merkle proofs that shares no code with the
Rust crates that produced the corpus — and, optionally, its Go verifier
(veraison/go-cose + fxamacker/cbor) and OpenSSL for the RFC 3161 tokens.

What is cross-checked, and by whom:

  canon      RFC 8785 canonical bytes and digests — an independent JCS written
             here (UTF-16 member order, JSON-spec escapes, float refusal).
  records    every Signed Statement: envelope, Ed25519 signature under the key
             the kid names, CWT claims (label 15), hash-envelope labels 258/259,
             payload == SHA-256(JCS(record)) — scitt-cose + the JCS above.
  heads      every tree head: Full-Content statement, content type, iss/sub,
             the embedded consistency proof — scitt-cose.
  logs       every root, inclusion proof and consistency proof of the 13-entry
             log, and the stored-node set — scitt-cose (recursive RFC 6962
             definition; ours come from a node store).
  receipts   every RFC 9942 receipt (vds 1): inclusion proof + log signature
             over the reconstructed root — scitt-cose; RFC 3161 tokens —
             ``openssl ts -verify`` when available.
  bundles    the golden bundles: statements, both heads, every inclusion and
             consistency proof, the full root recomputed from the entries, the
             Transparent Statement and its receipt — scitt-cose.
  go         the same statements and receipts through the Go verifier when
             ``--go`` names its binary.

Usage:
  python vectors/interop/verify_with_scitt_cose.py [--go PATH] [--vectors DIR]
                                                   [--openssl PATH|none] [-v]

Exit status: 0 every check agrees; 1 a check disagrees (each is printed);
2 setup (scitt-cose not importable, corpus not found). Deterministic: no
clock, no network, no randomness.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import cbor2
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    import scitt_cose
    from scitt_cose import (
        consistency_proof,
        extract_receipts,
        inclusion_proof,
        leaf_hash,
        merkle_root,
        parse_signed_statement,
        verify_consistency,
        verify_inclusion,
        verify_receipt,
    )
    from scitt_cose.cose_sign1 import strict_decode
except ImportError as exc:  # pragma: no cover - setup error
    print(f"setup: {exc}\n  pip install /path/to/scitt-cose  (see vectors/README.md)")
    sys.exit(2)

RECORD_CONTENT_TYPE = "application/vnd.brutor.action-record+json"
TREE_HEAD_CONTENT_TYPE = "application/vnd.brutor.tree-head+cbor"
HDR_ALG, HDR_CONTENT_TYPE, HDR_KID, HDR_CWT = 1, 3, 4, 15
HDR_PAYLOAD_HASH_ALG, HDR_PREIMAGE_CONTENT_TYPE = 258, 259
COSE_ALG_EDDSA, COSE_ALG_SHA256 = -8, -16
CWT_ISS, CWT_SUB = 1, 2
MAX_SAFE_INTEGER = (1 << 53) - 1


# ---------------------------------------------------------------------------
# An independent RFC 8785 canonicalizer with Brutor's digest hygiene
# ---------------------------------------------------------------------------


class CanonError(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def jcs(value) -> bytes:
    """RFC 8785 canonical JSON. Members ordered by UTF-16 code units (the
    UTF-16BE encoding of a key compares like its code-unit sequence); floats
    and integers outside ±(2^53−1) are refused."""
    out: list[str] = []

    def w(v) -> None:
        if v is None:
            out.append("null")
        elif v is True:
            out.append("true")
        elif v is False:
            out.append("false")
        elif isinstance(v, float):
            raise CanonError("float")
        elif isinstance(v, int):
            if abs(v) > MAX_SAFE_INTEGER:
                raise CanonError("unsafe_integer")
            out.append(str(v))
        elif isinstance(v, str):
            out.append(json.dumps(v, ensure_ascii=False))
        elif isinstance(v, list):
            out.append("[")
            for i, x in enumerate(v):
                if i:
                    out.append(",")
                w(x)
            out.append("]")
        elif isinstance(v, dict):
            out.append("{")
            for i, k in enumerate(sorted(v, key=lambda s: s.encode("utf-16-be"))):
                if i:
                    out.append(",")
                out.append(json.dumps(k, ensure_ascii=False))
                out.append(":")
                w(v[k])
            out.append("}")
        else:
            raise TypeError(type(v).__name__)

    w(value)
    return "".join(out).encode("utf-8")


def parse_json_text(text: str):
    """json.loads that keeps floats as floats (so jcs() can refuse them) and
    integers exact."""
    return json.loads(text, parse_float=float, parse_int=int)


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def preimage_digest(record: dict, spec: dict) -> str:
    scheme = spec["scheme"]
    if scheme == "jcs":
        return sha256_hex(jcs(spec["value"]))
    if scheme == "jcs_salted":
        salt = record["attestation"]["digest_salt"]
        return sha256_hex(jcs(spec["value"]) + b"|" + salt.encode("ascii"))
    if scheme == "utf8":
        return sha256_hex(spec["value"].encode("utf-8"))
    raise ValueError(f"unknown preimage scheme {scheme}")


def body_value(body: str):
    """The JSON value a captured body commits to (canon::body_value): the
    parsed document with floats and unsafe integers turned into their
    decimal strings, else the body as a JSON string. Only the shapes the
    corpus uses (short decimals) are reproduced here."""
    try:
        v = json.loads(body)
    except json.JSONDecodeError:
        return body

    def walk(x):
        if isinstance(x, bool) or x is None or isinstance(x, str):
            return x
        if isinstance(x, float):
            return repr(x) if math.isfinite(x) else str(x)
        if isinstance(x, int):
            return str(x) if abs(x) > MAX_SAFE_INTEGER else x
        if isinstance(x, list):
            return [walk(i) for i in x]
        return {k: walk(i) for k, i in x.items()}

    return walk(v)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class Checks:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.sections: dict[str, list[int]] = {}
        self.failures: list[str] = []
        self.current = ""

    def section(self, name: str) -> None:
        self.current = name
        self.sections.setdefault(name, [0, 0])

    def ok(self, cond: bool, what: str, detail: str = "") -> bool:
        s = self.sections[self.current]
        s[0] += 1
        if not cond:
            s[1] += 1
            msg = f"{self.current}: {what}" + (f" — {detail}" if detail else "")
            self.failures.append(msg)
            print(f"  FAIL {msg}")
        elif self.verbose:
            print(f"  ok   {what}")
        return cond

    def eq(self, got, want, what: str) -> bool:
        return self.ok(got == want, what, f"got {got!r}, want {want!r}")

    def note(self, text: str) -> None:
        print(f"  note {text}")

    def summary(self) -> int:
        print()
        for name, (n, bad) in self.sections.items():
            print(f"{name:10s} {n:5d} checks  {bad:3d} disagreements")
        total = sum(n for n, _ in self.sections.values())
        print(f"{'total':10s} {total:5d} checks  {len(self.failures):3d} disagreements")
        print("interop: " + ("PASS" if not self.failures else "FAIL"))
        return 0 if not self.failures else 1


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


def read_json(p: Path):
    return parse_json_text(p.read_text(encoding="utf-8"))


def pem_from_raw(kid_hex: str) -> bytes:
    pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(kid_hex))
    return pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def protected_map(msg: bytes) -> dict:
    """The protected header as a plain dict (strict decode: the same gate the
    upstream verifier applies, so a non-deterministic encoding fails here)."""
    outer = strict_decode(msg)
    protected_bstr = outer.value[0]
    return scitt_cose.cose_sign1._plain(cbor2.loads(protected_bstr)) if protected_bstr else {}


def kid_hex_of(msg: bytes) -> str:
    return bytes(protected_map(msg)[HDR_KID]).hex()


class Keys:
    def __init__(self, vectors: Path):
        wk = read_json(vectors / "keys/well-known.json")
        self.issuer = wk["issuer"]
        self.well_known = {k["kid"]: k for k in wk["keys"]}
        self.stranger = {k["kid"] for k in read_json(vectors / "keys/stranger.json")["keys"]}
        self.witness = read_json(vectors / "keys/witness.json")["witness_key_hex"]
        for k in wk["keys"]:
            # kid = raw public key = JWK x (base64url), an internal consistency the
            # keys document promises.
            x = base64.urlsafe_b64decode(k["x"] + "=" * (-len(k["x"]) % 4))
            assert x.hex() == k["kid"], f"keys/well-known.json: kid != x for {k['kid']}"


def verify_statement(msg: bytes) -> dict:
    """Upstream parse + verify under the key the statement's own kid names."""
    return parse_signed_statement(msg, public_key_pem=pem_from_raw(kid_hex_of(msg)))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def check_canon(c: Checks, vectors: Path) -> None:
    c.section("canon")
    for case in read_json(vectors / "canon/jcs.json")["cases"]:
        desc = case["description"]
        v = parse_json_text(case["input"])
        try:
            got = jcs(v)
        except CanonError as e:
            c.eq(e.kind, case.get("error"), f"{desc}: refused")
            continue
        c.ok("error" not in case, f"{desc}: accepted", f"expected refusal {case.get('error')}")
        c.eq(got.decode("utf-8"), case.get("canonical"), f"{desc}: canonical")
        c.eq(sha256_hex(got), case.get("sha256"), f"{desc}: sha256")


def check_records(c: Checks, vectors: Path, keys: Keys, go: "GoVerifier | None") -> None:
    c.section("records")
    files = sorted((vectors / "records").glob("*/*.json"))
    c.ok(len(files) >= 40, f"{len(files)} record vectors present")
    for f in files:
        name = f"{f.parent.name}/{f.stem}"
        v = read_json(f)
        record, expect = v["record"], v["expect"]
        codes = expect["codes"]

        # --- identity: record_id = SHA-256(JCS(record without record_id)) ---
        body = {k: x for k, x in record.items() if k != "record_id"}
        try:
            canonical = jcs(body)
        except CanonError as e:
            c.ok(
                f"{e.kind}_in_digest_field" in codes,
                f"{name}: JCS refuses ({e.kind})",
                f"codes {codes}",
            )
            c.ok("canonical" not in v, f"{name}: no canonical bytes for a refused record")
            canonical = None
        if canonical is not None:
            c.eq(canonical.decode("utf-8"), v.get("canonical"), f"{name}: canonical bytes")
            c.eq(sha256_hex(canonical), expect["record_id"], f"{name}: record_id")

        # --- literal preimages ---
        for path, spec in v.get("preimages", {}).items():
            carried = record
            for part in path.split("."):
                if "[" in part:
                    key, idx = part[: part.index("[")], int(part[part.index("[") + 1 : -1])
                    carried = carried[key][idx]
                else:
                    carried = carried[part]
            c.eq(preimage_digest(record, spec), carried, f"{name}: preimage {path}")
            if "canonical" in spec:
                c.eq(jcs(spec["value"]).decode("utf-8"), spec["canonical"], f"{name}: {path} canonical")
            if "body" in spec:
                c.eq(body_value(spec["body"]), spec["value"], f"{name}: {path} body value")

        # --- the Signed Statement (RFC 9943, RFC 9995 hash envelope) ---
        st_b64 = v.get("statement_b64")
        if st_b64 is None:
            c.ok(canonical is None, f"{name}: a record with no statement is one JCS refused")
            continue
        st = b64d(st_b64)
        prot = protected_map(st)
        kid = bytes(prot[HDR_KID]).hex()
        c.eq(prot.get(HDR_ALG), COSE_ALG_EDDSA, f"{name}: alg = EdDSA")
        c.ok(HDR_CONTENT_TYPE not in prot, f"{name}: no label 3 in hash-envelope mode")
        c.eq(prot.get(HDR_PAYLOAD_HASH_ALG), COSE_ALG_SHA256, f"{name}: label 258 = SHA-256")
        c.eq(prot.get(HDR_PREIMAGE_CONTENT_TYPE), RECORD_CONTENT_TYPE, f"{name}: label 259")
        parsed = verify_statement(st)
        sig_ok = "signature_invalid" not in codes
        c.eq(parsed["signature_verified"], sig_ok, f"{name}: upstream signature verdict")
        if parsed["signature_verified"]:
            c.eq(parsed["issuer"], keys.issuer, f"{name}: iss")
            c.ok(parsed["subject"].startswith("ai-system:"), f"{name}: sub is ai-system:<id>", parsed["subject"])
            c.eq(parsed["content_type"], None, f"{name}: upstream sees no label-3 content type")
            payload = parsed["payload"]
            c.eq(len(payload), 32, f"{name}: payload is a 32-byte digest")
            bound = "statement_payload_mismatch" not in codes
            c.eq(payload.hex() == expect["record_id"], bound, f"{name}: payload == record_id")
        else:
            c.ok(parsed["unverified"] is not None, f"{name}: envelope still parses structurally")
        trusted = kid in keys.well_known
        c.eq(trusted, "untrusted_kid" not in codes, f"{name}: kid listed in keys/well-known.json")
        if trusted:
            retired = keys.well_known[kid]["status"] == "retired"
            c.eq(retired, "kid_retired" in codes, f"{name}: kid status")
        if go is not None:
            go.statement(c, name, st, kid, sig_ok, iss=keys.issuer if sig_ok else None)


def head_payload(msg: bytes) -> dict:
    outer = strict_decode(msg)
    return scitt_cose.cose_sign1._plain(cbor2.loads(bytes(outer.value[2])))


def check_head_statement(c: Checks, name: str, msg: bytes, sig_ok: bool = True) -> dict | None:
    """Verify one tree-head Signed Statement upstream; return its payload."""
    parsed = verify_statement(msg)
    c.eq(parsed["signature_verified"], sig_ok, f"{name}: upstream signature verdict")
    if not parsed["signature_verified"]:
        return None
    c.eq(parsed["content_type"], TREE_HEAD_CONTENT_TYPE, f"{name}: content type (label 3)")
    head = head_payload(msg)
    c.eq(parsed["issuer"], head["log_id"], f"{name}: iss = log_id")
    c.eq(parsed["subject"], f"{head['log_id']}#{head['tree_size']}", f"{name}: sub = log_id#size")
    c.eq(len(head["root"]), 32, f"{name}: root is 32 bytes")
    c.eq((head["prev_size"] == 0), (len(head["prev_root"]) == 0), f"{name}: prev_root iff prev_size")
    return head


def check_heads(c: Checks, vectors: Path, go: "GoVerifier | None") -> None:
    c.section("heads")
    for f in sorted((vectors / "heads").glob("*.json")):
        name = f.stem
        v = read_json(f)
        codes = v["expect"]["codes"]
        st = b64d(v["statement_b64"])
        sig_ok = "head_signature_invalid" not in codes
        head = check_head_statement(c, name, st, sig_ok)
        if go is not None:
            go.statement(c, f"head {name}", st, kid_hex_of(st), sig_ok)
        if head is None:
            continue
        e = v["expect"]
        # brutor-verify reports head facts only once the head is accepted far
        # enough to read them; upstream decodes them regardless. Compare when
        # both sides have them, and require them whenever the vector is ok.
        for fact, got in (("log_id", head["log_id"]), ("tree_size", head["tree_size"]), ("root", head["root"].hex())):
            if e[fact] is not None or e["ok"]:
                c.eq(got, e[fact], f"{name}: {fact}")
        if head["prev_size"]:
            embedded_ok = verify_consistency(
                head["prev_root"].hex(),
                head["root"].hex(),
                head["prev_size"],
                head["tree_size"],
                [h.hex() for h in head["consistency_proof"]],
            )
            c.eq(embedded_ok, "head_consistency_invalid" not in codes, f"{name}: embedded consistency proof")
        prev_b64 = v.get("previous_b64")
        if prev_b64:
            prev = check_head_statement(c, f"{name} (previous)", b64d(prev_b64))
            if prev is None:
                continue
            adjacent = (
                prev["log_id"] == head["log_id"]
                and head["prev_size"] == prev["tree_size"]
                and head["prev_root"] == prev["root"]
            )
            if adjacent:
                c.eq(e["consistent_with_previous"], True, f"{name}: adjacent heads chain")
            proof = v.get("consistency_proof")
            if proof is not None:
                got = verify_consistency(
                    prev["root"].hex(), head["root"].hex(), prev["tree_size"], head["tree_size"], proof
                )
                c.eq(got, e["consistent_with_previous"], f"{name}: supplied consistency proof")
            elif prev["log_id"] == head["log_id"] and prev["tree_size"] == head["tree_size"]:
                c.eq(prev["root"] == head["root"], e["consistent_with_previous"], f"{name}: same-size heads agree on the root")


def check_logs(c: Checks, vectors: Path) -> None:
    c.section("logs")
    inc = read_json(vectors / "logs/inclusion.json")
    entries = inc["log"]["entries_hex"]
    n_total = len(entries)
    c.eq([leaf_hash(e) for e in entries], inc["log"]["leaf_hashes"], "leaf hashes")
    for n_str, root in inc["log"]["roots"].items():
        c.eq(merkle_root(entries[: int(n_str)]), root, f"root at size {n_str}")
    for case in inc["cases"]:
        i, n = case["index"], case["tree_size"]
        c.eq(inclusion_proof(entries[:n], i), case["path"], f"inclusion path {i} of {n}")
        c.eq(verify_inclusion(entries[i], i, n, case["path"], case["root"]), case["expect"], f"inclusion {i} of {n}")
    for case in inc["negative"]:
        got = verify_inclusion(entries[case["entry_index"]], case["index"], case["tree_size"], case["path"], case["root"])
        c.eq(got, case["expect"], f"negative inclusion: {case['description']}")
    cons = read_json(vectors / "logs/consistency.json")
    c.eq(cons["log"]["entries_hex"], entries, "consistency.json shares the log")
    for case in cons["cases"]:
        m, n = case["first_size"], case["second_size"]
        c.eq(consistency_proof(entries, m, n), case["path"], f"consistency proof {m}->{n}")
        c.eq(verify_consistency(case["first_root"], case["second_root"], m, n, case["path"]), case["expect"], f"consistency {m}->{n}")
    for case in cons["negative"]:
        got = verify_consistency(case["first_root"], case["second_root"], case["first_size"], case["second_size"], case["path"])
        c.eq(got, case["expect"], f"negative consistency: {case['description']}")
    nodes = read_json(vectors / "logs/store-nodes.json")
    expected_nodes = 0
    for node in nodes["nodes"]:
        lv, ix = node["level"], node["index"]
        a, b = ix << lv, (ix + 1) << lv
        c.ok(b <= n_total, f"node ({lv},{ix}) covers a full range")
        c.eq(merkle_root(entries[a:b]), node["hash"], f"node ({lv},{ix}) = MTH(entries[{a}:{b}])")
    for lv in range(0, n_total.bit_length()):
        expected_nodes += n_total >> lv
    c.eq(len(nodes["nodes"]), expected_nodes, "the node set is exactly the full ranges")


def check_receipts(c: Checks, vectors: Path, keys: Keys, go: "GoVerifier | None", openssl: str | None) -> None:
    c.section("receipts")
    ca = vectors.parent / "tests/fixtures/rfc3161/ca.crt"
    for f in sorted((vectors / "receipts").glob("*.json")):
        name = f.stem
        v = read_json(f)
        e = v["expect"]
        receipt, statement = b64d(v["receipt_b64"]), b64d(v["statement_b64"])
        valid = "receipt_invalid" not in e["codes"]
        if v["kind"] == "scitt":
            pin = v["pin"] or keys.witness
            r = verify_receipt(receipt, leaf_entry_hex=statement.hex(), log_public_key_pem=pem_from_raw(pin))
            c.eq(r.ok, valid, f"{name}: upstream verdict ({'; '.join(r.errors)})")
            # Facts (tree_size, leaf_index, iat) are reported by brutor-verify
            # only for a receipt it checked and accepted; upstream decodes them
            # before the signature check. Compare when ours are present.
            for fact, got in (("tree_size", r.tree_size), ("leaf_index", r.leaf_index), ("iat", r.iat)):
                if e[fact] is not None or e["grade"] == "witnessed":
                    c.eq(got, e[fact], f"{name}: {fact}")
            c.ok(not r.protected_header_ext, f"{name}: no vendor-private protected labels", str(r.protected_header_ext))
            if go is not None:
                go.receipt(c, name, receipt, statement, pin, valid, r.root)
        elif v["kind"] == "rfc3161":
            if openssl is None or not ca.is_file():
                c.note(f"{name}: RFC 3161 token not cross-checked (openssl or {ca} missing)")
                continue
            with tempfile.TemporaryDirectory() as d:
                tok, data = Path(d) / "token.tsr", Path(d) / "data.bin"
                tok.write_bytes(receipt)
                data.write_bytes(statement)
                proc = subprocess.run(
                    [openssl, "ts", "-verify", "-data", str(data), "-in", str(tok), "-CAfile", str(ca)],
                    capture_output=True,
                    text=True,
                )
            c.eq(proc.returncode == 0, valid, f"{name}: openssl ts -verify ({(proc.stderr or proc.stdout).strip().splitlines()[-1:]})")


def check_bundles(c: Checks, vectors: Path, keys: Keys, go: "GoVerifier | None") -> None:
    c.section("bundles")
    for f in sorted((vectors / "bundles/golden").glob("*.json")):
        name = f.stem
        b = read_json(f)
        # heads: statements, payloads, roots by size
        heads: dict[int, dict] = {}
        head_statements: list[bytes] = []
        for i, h in enumerate(b["tree_heads"]):
            st = b64d(h["statement_b64"])
            head_statements.append(st)
            head = check_head_statement(c, f"{name}: head {i}", st)
            if head is None:
                continue
            heads[head["tree_size"]] = head
            c.eq(head["log_id"], b["manifest"]["log_id"], f"{name}: head {i} log_id")
            if head["prev_size"]:
                c.ok(
                    verify_consistency(
                        head["prev_root"].hex(), head["root"].hex(), head["prev_size"], head["tree_size"],
                        [x.hex() for x in head["consistency_proof"]],
                    ),
                    f"{name}: head {i} embedded consistency proof",
                )
            tb64 = h.get("transparent_statement_b64")
            for j, rc in enumerate(h.get("receipts", [])):
                receipt = b64d(rc["receipt_b64"])
                c.eq(rc["kind"], "scitt", f"{name}: head {i} receipt {j} kind")
                r = verify_receipt(receipt, leaf_entry_hex=st.hex(), log_public_key_pem=pem_from_raw(keys.witness))
                c.ok(r.ok, f"{name}: head {i} receipt {j} under the witness key", "; ".join(r.errors))
                if tb64:
                    transparent = b64d(tb64)
                    c.ok(receipt in extract_receipts(transparent), f"{name}: head {i} receipt {j} carried by the Transparent Statement")
                    tparsed = verify_statement(transparent)
                    c.ok(tparsed["signature_verified"] is True, f"{name}: head {i} Transparent Statement signature")
                    c.eq(protected_map(transparent), protected_map(st), f"{name}: head {i} Transparent Statement keeps the protected header")
                if go is not None:
                    go.receipt(c, f"{name}: head {i} receipt {j}", receipt, st, keys.witness, True, r.root)
        # records: statements bound to record ids, kid trusted
        by_seq: dict[int, bytes] = {}
        for r in b["records"]:
            st = b64d(r["statement_b64"])
            rid = r["record"]["record_id"]
            parsed = verify_statement(st)
            c.ok(parsed["signature_verified"] is True, f"{name}: record {r['seq']} statement signature")
            if parsed["signature_verified"]:
                c.eq(parsed["payload"].hex(), rid, f"{name}: record {r['seq']} payload == record_id")
                c.eq(parsed["issuer"], b["manifest"]["issuer"], f"{name}: record {r['seq']} iss = manifest issuer")
            c.eq(sha256_hex(jcs({k: x for k, x in r["record"].items() if k != "record_id"})), rid, f"{name}: record {r['seq']} record_id")
            c.ok(kid_hex_of(st) in keys.well_known, f"{name}: record {r['seq']} kid trusted")
            by_seq[r["seq"]] = st
        # inclusion proofs against the head of that size
        for p in b["proofs"]:
            head = heads.get(p["tree_size"])
            if not c.ok(head is not None, f"{name}: proof for {p['record_id'][:12]} names a carried head size"):
                continue
            st = next((s for s in b["records"] if s["record"]["record_id"] == p["record_id"]), None)
            if not c.ok(st is not None, f"{name}: proof names a carried record"):
                continue
            c.eq(p["leaf_index"], st["seq"], f"{name}: proof leaf_index == seq")
            c.ok(
                verify_inclusion(b64d(st["statement_b64"]).hex(), p["leaf_index"], p["tree_size"], p["path"], head["root"].hex()),
                f"{name}: inclusion of seq {st['seq']} in size {p['tree_size']}",
            )
        for cp in b["consistency"]:
            a, z = heads.get(cp["from_size"]), heads.get(cp["to_size"])
            if c.ok(a is not None and z is not None, f"{name}: consistency names carried heads"):
                c.ok(verify_consistency(a["root"].hex(), z["root"].hex(), cp["from_size"], cp["to_size"], cp["path"]), f"{name}: consistency {cp['from_size']}->{cp['to_size']}")
        # full-root recomputation: the entries the bundle carries plus the
        # earlier heads' own entries (each head is appended to its log after it
        # is issued) must reproduce every head's root from scratch.
        newest = max(heads) if heads else 0
        missing = [i for i in range(newest) if i not in by_seq]
        others = [s for s in head_statements if heads.get(head_payload(s)["tree_size"], {}).get("tree_size") != newest]
        if len(missing) == len(others) and by_seq and newest:
            entries = dict(by_seq)
            for i, s in zip(missing, others):
                entries[i] = s
            entries_hex = [entries[i].hex() for i in range(newest)]
            for size, head in heads.items():
                c.eq(merkle_root(entries_hex[:size]), head["root"].hex(), f"{name}: MTH of the {size} entries == head root")
            for p in b["proofs"]:
                st = next(s for s in b["records"] if s["record"]["record_id"] == p["record_id"])
                c.eq(inclusion_proof(entries_hex[: p["tree_size"]], st["seq"]), p["path"], f"{name}: upstream regenerates the path of seq {st['seq']}")
            for cp in b["consistency"]:
                c.eq(consistency_proof(entries_hex, cp["from_size"], cp["to_size"]), cp["path"], f"{name}: upstream regenerates consistency {cp['from_size']}->{cp['to_size']}")
        else:
            c.note(f"{name}: root not recomputed from entries ({len(missing)} unknown entries, {len(others)} earlier heads)")


# ---------------------------------------------------------------------------
# Go verifier
# ---------------------------------------------------------------------------


class GoVerifier:
    def __init__(self, binary: str):
        self.binary = binary
        self.runs = 0

    def _run(self, args: list[str]) -> tuple[int, dict]:
        self.runs += 1
        proc = subprocess.run([self.binary, *args], capture_output=True, text=True)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            out = {"error": proc.stdout + proc.stderr}
        return proc.returncode, out

    def statement(self, c: Checks, name: str, st: bytes, kid: str, valid: bool, iss: str | None = None) -> None:
        with tempfile.TemporaryDirectory() as d:
            sp, kp = Path(d) / "statement.cose", Path(d) / "key.pem"
            sp.write_bytes(st)
            kp.write_bytes(pem_from_raw(kid))
            code, out = self._run(["--statement", str(sp), "--pubkey", str(kp), "--alg", "EdDSA"])
        c.eq((code == 0, out.get("valid")), (valid, valid), f"go: {name} signature ({out.get('error', '')})")
        if valid:
            c.eq(out.get("kid"), kid, f"go: {name} kid")
            if iss is not None:
                c.eq(out.get("iss"), iss, f"go: {name} iss")

    def receipt(self, c: Checks, name: str, receipt: bytes, statement: bytes, pin: str, valid: bool, root: str | None) -> None:
        with tempfile.TemporaryDirectory() as d:
            rp, kp = Path(d) / "receipt.cose", Path(d) / "log.pem"
            rp.write_bytes(receipt)
            kp.write_bytes(pem_from_raw(pin))
            code, out = self._run(["--receipt", str(rp), "--log-pubkey", str(kp), "--leaf-entry-hex", statement.hex()])
        r = out.get("receipt") or {}
        c.eq((code == 0, r.get("ok")), (valid, valid), f"go: {name} receipt ({r.get('error', out.get('error', ''))})")
        if valid and root:
            c.eq(r.get("root"), root, f"go: {name} root")


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vectors", default=str(Path(__file__).resolve().parent.parent), help="corpus root (default: the directory above this script)")
    ap.add_argument("--go", help="path to a built scitt-cose-go-verify binary (optional)")
    ap.add_argument("--openssl", default=os.environ.get("OPENSSL", "openssl"), help="openssl binary for RFC 3161 tokens, or 'none'")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    vectors = Path(args.vectors)
    if not (vectors / "keys/well-known.json").is_file():
        print(f"setup: no corpus at {vectors} (run `cargo run -p brutor-verify --example gen-vectors`)")
        return 2
    openssl = None if args.openssl == "none" else shutil.which(args.openssl)
    go = None
    if args.go:
        if not Path(args.go).is_file():
            print(f"setup: --go {args.go} is not a file")
            return 2
        go = GoVerifier(args.go)

    print(f"scitt-cose {scitt_cose.__version__}; corpus {vectors}")
    print(f"go verifier: {args.go or 'not run'}; openssl: {openssl or 'not run'}")
    c = Checks(args.verbose)
    keys = Keys(vectors)
    check_canon(c, vectors)
    check_records(c, vectors, keys, go)
    check_heads(c, vectors, go)
    check_logs(c, vectors)
    check_receipts(c, vectors, keys, go, openssl)
    check_bundles(c, vectors, keys, go)
    if go is not None:
        print(f"go verifier invoked {go.runs} times")
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
