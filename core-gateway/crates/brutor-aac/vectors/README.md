# brutor-aac vectors

## Vendored upstream material (read-only copies — never edit)

| Path here | Upstream path | Notes |
|---|---|---|
| `capsule/` | `vectors/capsule/` | 89 Class-1 cases (49 positive, 35 negative, 3 store, 1 overclaim, 1 honesty) |
| `producer-envelope/` | `vectors/producer-envelope/` | 8 binary COSE_Sign1 envelope cases |
| `disclosure-envelope/` | `vectors/disclosure-envelope/` | 7 DE-1..DE-3 cases |
| `bundle/` | `vectors/bundle/` | 12 `evidence-bundle/v2` outcome cases (manifest only; fixture is built in code) |
| `cross-language/seal-input.json` | `vectors/cross-language/seal-input.json` | interlock producer input |
| `manifest.json`, `SHA256SUMS.upstream-root` | `vectors/manifest.json`, `vectors/SHA256SUMS` | root checksum list (covers files not vendored, e.g. `interop/`) |
| `registry/REGISTRY.md` | `spec/REGISTRY.md` | interim registry of record (drift-tested against `src/registry.rs`) |
| `registry/cpb_provisional.json` | `go/registries/data/cpb_provisional.json` | CPB provisional projection (scitt-payload-binding `e0ad1c7e`) |
| `capsule-emit/capsule_emit_ledger.jsonl` | `python/tests/fixtures/capsule_emit_ledger.jsonl` | real capsule-emit output (signature + key_id, one chained Capsule) |

* **Source repository:** `action-state-group/agent-action-capsule`,
  commit `8a4931e39d17e55975b7c0e91418a7618f0603d4`.
* **Draft revision:** `draft-mih-scitt-agent-action-capsule-04` (format 4,
  `canonicalization_id: "jcs"`); bundle corpus
  `draft-mih-zhang-agent-action-capsule-evidence-bundle-00`; disclosure corpus
  `draft-mih-scitt-agent-action-capsule-disclosure-envelope-00`.
* **Licence:** code and test vectors are BSD-3-Clause, Copyright (c) 2026
  Action State Group, Inc. — full text in `LICENSE-upstream-BSD-3-Clause`.
  `registry/REGISTRY.md` is specification text governed by BCP 78/79 (IETF
  Trust Legal Provisions); it is vendored unmodified for conformance testing.
* **Integrity:** `tests/vectors.rs::vendored_bytes_match_upstream_checksums`
  checks every file against the per-corpus `SHA256SUMS` shipped upstream.

To refresh after an upstream revision: copy the same paths again, keep the
per-corpus `SHA256SUMS`, update the commit above, run `cargo test -p brutor-aac`.
A new draft revision is a new `SPEC_VERSION` in `src/lib.rs` — never an edit
to our stored record model.

## Our vectors (`brutor-mapping/`)

`brutor-mapping/vectors.json` freezes the `ai.brutor/action-record/v1` →
Capsule mapping (`ai.brutor/aac-mapping/v1`) for records built with
`brutor_evidence::RecordBuilder` in the shapes the gateway mints and the
seal API accepts (MCP/LLM/A2A outcomes, approvals, epoch boundary,
declarations, judged lineages). Each case holds the source lineage
(root-first), and per record the expected `capsule_id`, the Capsule, the
lossy codes and a Producer Envelope signed with the **public test seed**
`0x42 × 32` (test material only — never a production key).

`brutor-mapping/interlock/` holds the same Capsules as individual files
(`*.capsule.json`, `*.capsule_id.txt`, `*.envelope.cose`) so any other
implementation can consume them.

Regenerate (only for an intentional mapping change — bump `MAPPING_ID` when
an existing Capsule changes):

```sh
BRUTOR_AAC_REGEN=1 cargo test -p brutor-aac --test mapping_vectors
```

## Interlock (`rust` producer ↔ upstream reference verifiers)

CI never depends on non-Rust toolchains; the interlock test is `#[ignore]`d.

Python Class-1 (all 18 Capsules accepted with identical
IDs, and the Python-sealed `seal-input.json` accepted by our verifier):

```sh
AAC_UPSTREAM=/path/to/agent-action-capsule \
  cargo test -p brutor-aac --test mapping_vectors -- --ignored
# or by hand, per Capsule:
PYTHONPATH=$AAC_UPSTREAM/python \
  python3 $AAC_UPSTREAM/python/scripts/cross_language.py verify \
  < vectors/brutor-mapping/interlock/mcp-executed-confirmed-0.capsule.json
```

Go Class-1 (needs the Go toolchain pinned by upstream `go.mod` and module
downloads):

```sh
cd $AAC_UPSTREAM/go && GOWORK=off go run ./cmd/cross_language verify \
  < <this crate>/vectors/brutor-mapping/interlock/mcp-executed-confirmed-0.capsule.json
```

Producer Envelopes with the Python reference (needs `cbor2`, `cryptography`
and `scitt_cose` importable):

```sh
PYTHONPATH=$AAC_UPSTREAM/python:/path/to/scitt-cose python3 -c '
import sys; from agent_action_capsule.producer_envelope import verify_producer_envelope as v
r = v(open(sys.argv[1]).read().strip(), open(sys.argv[2], "rb").read()); print(r.ok, (r.public_key or b"").hex(), [f.code for f in r.findings])
' vectors/brutor-mapping/interlock/X.capsule_id.txt vectors/brutor-mapping/interlock/X.envelope.cose
```
