# Evidence vector corpus

Test vectors for the Brutor evidence formats — action records,
Signed Statements, tree heads, witness receipts and evidence bundles — so a
third party can check, and reimplement, every verification step without our
code.

Everything under `vectors/` except this file and `interop/` is **generated**
by `crates/brutor-verify/tests/corpus/mod.rs`:

```text
cargo run -p brutor-verify --example gen-vectors
```

The generator is deterministic (fixed key seeds, fixed timestamps, Ed25519
signatures are deterministic per RFC 8032, JSON written with sorted keys) so
a regeneration reproduces every file byte for byte. `cargo test -p
brutor-verify --test vectors` fails when the committed corpus differs from
what the generator produces (`vectors_are_up_to_date`), then re-reads every
file from disk and checks it meets its declared expectation, as a third-party
implementation would. **A changed vector is a changed contract** — review
the diff, never just regenerate.

The generator also writes the control plane's parity fixture
`brutor-gateway-control/tests/fixtures/evidence_interop_v1.json` (the
Python side, which has no cryptography library, recomputes canonical bytes,
record ids, salted digests, leaf hashes, stored nodes, roots, inclusion
proofs and bundle digests from it — `tests/test_evidence_rust_interop.py`).

## Layout

| Path | Files | What |
|---|---|---|
| `keys/well-known.json` | 1 | The corpus deployment's `/.well-known/brutor-evidence-keys.json`: one active and one retired Ed25519 key |
| `keys/stranger.json` | 1 | A keys document from another deployment (for `untrusted_kid` cases) |
| `keys/witness.json` | 1 | The witness's public key — the pin for every `scitt` receipt |
| `records/positive/*.json` | 24 | Records that verify (`ok: true`), one per verdict, effect status and chain relation, plus digest, canonicalisation and key-status cases |
| `records/negative/*.json` | 17 | Records that fail one named rule each |
| `heads/*.json` | 9 | Signed tree heads: first head, adjacent heads, non-adjacent with and without a proof, fork, rewritten history, wrong log, bad signature, untrusted kid |
| `receipts/*.json` | 9 | Witness receipts: 4 RFC 9942 (`scitt`) and 5 RFC 3161 (`rfc3161`) cases |
| `bundles/golden/*.json` | 4 | Evidence bundles that verify |
| `bundles/tampered/*.json` | 15 | One tampering each of a golden bundle |
| `bundles/expect.json` | 1 | The expected outcome of every bundle file under every trust configuration it is checked with (21 cases) |
| `logs/inclusion.json` | 1 | RFC 6962 inclusion proofs of a 13-entry log, positive and negative |
| `logs/consistency.json` | 1 | RFC 6962 consistency proofs for every `1 ≤ m < n ≤ 13`, and negative cases |
| `logs/store-nodes.json` | 1 | The complete-subtree node set a store-backed log of 13 leaves holds |
| `canon/jcs.json` | 1 | RFC 8785 canonicalisation cases, including the digest-hygiene refusals |
| `SHA256SUMS` | 1 | SHA-256 of every generated file |
| `interop/verify_with_scitt_cose.py` | — | Hand-written: cross-checks the corpus against the upstream `scitt-cose` implementation (see [Interop](#interop)) |

## Conventions

* CBOR artefacts (statements, receipts, Transparent Statements, RFC 3161
  tokens) are carried as standard base64 in `*_b64` members. Digests, Merkle
  hashes, kids and pins are lowercase hex.
* `description` says what a vector demonstrates. It is prose, not contract.
* `expect.ok` is `true` iff verification produced **no `error` finding**.
  Warnings and infos never fail a vector.
* `expect.codes` lists **every** finding code the verifier emits for the
  vector — all severities — in emission order. A reimplementation must
  produce the same set; the order is what `brutor-verify` does and is the
  order the rules are specified in, so a different order in a
  reimplementation is a defect only if it changes which rule fires first.
* Finding codes are stable identifiers. The prose beside them is not.
* Keys: the corpus signing keys are derived from
  `SHA-256("brutor-verify/vectors/v1/<label>")` with labels
  `deployment-key`, `retired-key`, `stranger-key`, `witness-key`. They are
  published on purpose and sign nothing but this corpus. A `kid` is the raw
  32-byte Ed25519 public key (hex in JSON, bstr in COSE).

## Keys

`keys/well-known.json` is the shape a deployment serves at
`/.well-known/brutor-evidence-keys.json`:

```json
{
  "issuer": "urn:brutor:deployment:vectors",
  "keys": [ { "kid": "<64 hex>", "kty": "OKP", "crv": "Ed25519", "alg": "EdDSA",
              "x": "<base64url of the same 32 bytes>", "use": "sig",
              "issuer": "...", "status": "active" | "retired", "scope": "deployment" } ],
  "statement_content_type": "application/vnd.brutor.action-record+json",
  "tree_head_content_type": "application/vnd.brutor.tree-head+cbor",
  "record_schema": "ai.brutor/action-record/v1"
}
```

A retired key stays trusted for what it signed (`kid_retired`, info). A kid
not in the document is `untrusted_kid` (error). With no document at all, a
verifier checks the signature under the key the statement itself names and
reports `kid_not_pinned` (warning): a self-consistency check, not an
attribution.

## Records — `records/{positive,negative}/*.json`

```json
{
  "description": "...",
  "record": { ...the action record, including record_id... },
  "statement_b64": "<COSE_Sign1>",          // absent when no identity is computable
  "store": [ ...records a chain check may look up (parents)... ],   // optional
  "canonical": "<JCS bytes of record minus record_id>",            // absent when refused
  "preimages": {
    "<path in record>": { "scheme": "jcs" | "jcs_salted" | "utf8", "value": ...,
                          "canonical": "...", "body": "..." }       // canonical/body optional
  },
  "expect": { "ok": bool, "codes": [...], "record_id": "<64 hex>",
              "effect_mode": "not_applicable" | "dispatched_unconfirmed" | "confirmed" }
}
```

### Identity (rule 2)

`record_id = hex(SHA-256(JCS(record without "record_id")))` where JCS is RFC
8785 with two hygiene rules: a JSON **float anywhere is
refused** (`float_in_digest_field`) and an integer outside ±(2^53−1) is
refused (`unsafe_integer_in_digest_field`) — no identity exists for such a
record, so it carries no statement. `canonical` is the exact JCS text so a
reimplementation can diff bytes, not just digests. RFC 8785 orders object
members by **UTF-16 code units**, not code points: `utf16-key-order`
demonstrates U+10000 (surrogate pair D800 DC00) sorting before U+E000 and
U+FFFD.

### Digests over literal preimages

Every `preimages` entry names a digest-bearing member (`effect.request_digest`,
`attestation.output_digest`, `references[0].digest`, …) and the literal value
it was computed from:

| scheme | digest |
|---|---|
| `jcs` | `SHA-256(JCS(value))` |
| `jcs_salted` | `SHA-256(JCS(value) ‖ "\|" ‖ salt)` — `salt` is the ASCII bytes of `attestation.digest_salt` |
| `utf8` | `SHA-256(UTF-8 bytes of value)` — rendered text, e.g. an AI-interaction notice |

When `body` is present it is the captured wire body and `value` is what the
body commits to: the parsed JSON with floats and unsafe integers rewritten
as decimal strings (the record then says `numbers_canonicalized: true`), or
the body itself as a JSON string when it is not JSON.

### The Signed Statement (RFC 9943 in RFC 9995 hash-envelope mode)

`statement_b64` is a COSE_Sign1 (CBOR tag 18):

```text
protected   = { 1: -8,                       ; EdDSA
                4: <32-byte public key>,     ; kid
                15: { 1: iss, 2: sub },      ; CWT claims (RFC 9597: label 15, never 13)
                258: -16,                    ; payload hash alg = SHA-256 (RFC 9995)
                259: "application/vnd.brutor.action-record+json" }
unprotected = {}                             ; receipts (394) only on Transparent Statements
payload     = <32 raw bytes> == record_id
signature   = Ed25519( CBOR(["Signature1", protected_bstr, h'', payload]) )
```

There is **no label 3** in hash-envelope mode. `iss` is
`urn:brutor:deployment:<id>`; `sub` is `ai-system:<id>` or `tenant:<id>`.
The protected header is a deterministically encoded map (RFC 8949 §4.2.1:
shortest-form integers, keys sorted length-first then bytewise); a verifier
must sign-check the **bytes as received**, never a re-encoding.

Verification: decode; `alg` must be −8; take `kid`; decide trust from the
keys document; verify the signature; then **bind**: the payload must equal
the record's *recomputed* identity (`statement_payload_mismatch` otherwise —
`neg/wrong-statement-payload`, `neg/tampered-content`). A statement over a
record whose carried `record_id` was edited still binds (the content is
intact) but the record fails rule 2 (`neg/tampered-record-id`).

### Semantic rules and their codes

Fixed order; structured result; never throws; no clock, network or model.

| Rule | What | Codes in the corpus |
|---|---|---|
| 1 structural | schema, admitted fields only (allow-list as code), never-enters names at any depth, types, vocabulary of `approver` | `missing_required_field`, `unadmitted_field`, `never_enters_member`, `approver_invalid`, `float_in_digest_field`, `unsafe_integer_in_digest_field` |
| 2 identity | `record_id` recomputes | `record_id_mismatch` |
| 3 confirmed-effect binding | `confirmed` needs both digests; `planned` carries none | `confirmed_without_response`, `planned_with_digests` |
| 4 verdict/effect orthogonality | a never-dispatching verdict (blocked, denied, engine_failure, awaiting_human, …) cannot carry a dispatched effect | `verdict_effect_conflict` |
| 5 attestation presence | a dispatched effect names who observed it; an attestation on a planned effect is informational | `attestation_missing`, `attestation_on_planned` (info) |
| 6 chain | the parent of `chain.parent_record_id` is in the supplied `store`; a chain edge is stated once | `chain_parent_missing`, `reference_duplicates_chain_parent` |
| 7 claims never above derived | `expect.effect_mode` is derived from the effect block, never from what the record claims | (asserted through `effect_mode`) |
| 8 unknown vocabulary | a verdict or decision outside the known set is info, never an error, never graded up | `unknown_vocabulary_value` (info) |
| honesty | `human_disposed: true` with `approver: policy` | `dishonest_human_disposed` (warning) |
| statement | trust and binding, above | `untrusted_kid`, `kid_retired` (info), `signature_invalid`, `statement_payload_mismatch` |

The full code list, with severities, is in
`crates/brutor-evidence/src/verify.rs` (records) and
`crates/brutor-verify/src/lib.rs` (`codes`).

`positive/` covers every verdict (`executed`, `errored`, `timeout`, `blocked`,
`denied`, `engine_failure`, `awaiting_human`, `resolved`, `expired`,
`epoch_boundary`, `assessment`, `adjudication`, `recorded`, `planned`), every
effect status (`planned`, `dispatched`, `confirmed`, `failed`, `reverted`)
and every chain relation (`confirms`, `supersedes`, `epoch_opens`,
`assesses`, `adjudicates`); `every_verdict_effect_status_and_relation_is_covered`
enforces that.

## Tree heads — `heads/*.json`

```json
{
  "description": "...",
  "statement_b64": "<signed head>",
  "previous_b64": "<signed head or null>",
  "consistency_proof": ["<hex>", ...] | null,   // PROOF(prev.size, D[head.size]) when heads are not adjacent
  "keys": "keys/well-known.json" | "keys/stranger.json",
  "expect": { "ok": bool, "codes": [...], "consistent_with_previous": true | false | null,
              "log_id": "<hex>", "tree_size": n, "root": "<hex>" }
}
```

A head is a Signed Statement in **Full-Content** mode:

```text
protected = { 1: -8, 3: "application/vnd.brutor.tree-head+cbor", 4: <kid>,
              15: { 1: <log_id>, 2: "<log_id>#<tree_size>" } }
payload   = CBOR map { "log_id": tstr, "tree_size": uint, "root": bstr(32),
                       "prev_size": uint, "prev_root": bstr(32) | bstr(0),
                       "issued_at": tstr (RFC 3339 UTC),
                       "consistency_proof": [bstr(32)...] }   ; PROOF(prev_size, D[tree_size])
```

`prev_root` is the empty bstr and `consistency_proof` empty for a first head
(`prev_size` 0). `log_id` is opaque (a SHA-256 of deployment and tenant
identifiers), never the tenant id.

Verification: content type; signature under a trusted kid; `iss`/`sub`
match the payload (`head_malformed`); when `prev_size > 0` the embedded
proof must carry `prev_root` to `root` (`head_consistency_invalid` —
`embedded-proof-fails`). Against a previous head: same log
(`head_log_mismatch`); previous head itself acceptable
(`previous_head_invalid`); if `head.prev_size == previous.tree_size` and
`head.prev_root == previous.root` the embedded proof decides
(`adjacent-heads`); else a supplied `consistency_proof` decides
(`non-adjacent-with-proof`); two heads of the **same size with different
roots** are a fork (`head_not_consistent`, `consistent_with_previous:
false`); otherwise consistency is **undetermined**, reported as an error,
never assumed (`consistency_undetermined`, `non-adjacent-without-proof`).

## Receipts — `receipts/*.json`

```json
{
  "description": "...",
  "kind": "scitt" | "rfc3161",
  "receipt_b64": "<receipt or TimeStampToken>",
  "statement_b64": "<the bytes the receipt is over>",
  "pin": "<hex Ed25519 key of the witness>" | "<hex SHA-256 of the TSA certificate DER>" | null,
  "expect": { "grade": "witnessed" | "timestamped" | "unverified" | "invalid",
              "ok": bool, "codes": [...], "tree_size": n | null, "leaf_index": i | null, "iat": t | null }
}
```

**`scitt`** — an RFC 9942 COSE Receipt for the `RFC9162_SHA256` structure:

```text
protected   = { 1: -8, 15: { 6: iat }?, 395: 1 }                 ; vds from the PROTECTED header only
unprotected = { 396: { -1: [ bstr( CBOR([tree_size, leaf_index, [path bstr(32)...]]) ) ] } }
payload     = nil (detached); the signed bytes are the 32-byte tree root
```

The leaf is `SHA-256(0x00 ‖ statement bytes)` — the witness registered the
head statement as an entry of **its own** log. Verification: read
`vds` from the protected header; decode the inclusion proof; check the path
length against the tree shape **before** hashing; fold to the root; verify
the signature over that root under the **pinned** key. Without a pin nothing
is checked: `receipt_unpinned` (warning), grade `unverified` — not evidence
of forgery. A wrong pin, a tampered signature or the wrong statement are
`receipt_invalid`, grade `invalid`.

**`rfc3161`** — an RFC 3161 `TimeStampResp` whose token is over
`SHA-256(statement bytes)`; the pin is the SHA-256 of the DER of the TSA's
signing certificate (embedded in the token). Three TSA key types are
covered (ECDSA P-256, ECDSA P-384, RSA-2048) from
`tests/fixtures/rfc3161/`; `tree_size`, `leaf_index` and `iat` are `null`
for tokens (a timestamp proves a date, not inclusion).

## Bundles — `bundles/**` and `bundles/expect.json`

A bundle (`ai.brutor/evidence-bundle/v1`) carries `manifest`,
`records[]` (`record`, `statement_b64`, `seq`), `proofs[]` (`record_id`,
`leaf_index`, `tree_size`, `path[]`), `tree_heads[]` (`statement_b64`,
optional `transparent_statement_b64`, `receipts[]` with `kind`,
`receipt_b64`, `grade`), `consistency[]` (`from_size`, `to_size`, `path[]`),
`prunes[]` and `extensions{}`. The bundle digest is
`SHA-256(JCS(bundle without countersignatures))`.

`expect.json` lists one case per (file, trust configuration):

```json
{ "file": "golden/run-contiguous.json", "description": "...",
  "keys": "keys/well-known.json" | "keys/stranger.json" | null,
  "witness_keys": ["<hex>"] | [],
  "expected_digest": "<hex>" | null,
  "expect": { "ok": bool, "codes": [...], "bundle_digest": "<hex>",
              "records_verified": n, "proofs_verified": n, "heads_verified": n } }
```

Golden bundles: `run-contiguous` (a whole run selected by `root_task_id`:
an approval chain, a two-phase action, a block and a denial; two heads
linked by a consistency proof; the newest head witnessed and carried as a
Transparent Statement with its receipt at label 394); `producer-selected-closure`
(explicit ids plus their chain parents at `closure_depth` 1);
`declared-incomplete` (a parent not followed and **listed** in
`completeness.missing`, `records_mode: declared_incomplete`);
`single-record` (a one-leaf log). `run-contiguous` is also checked with no
trusted keys (`kid_not_pinned`, `receipt_not_checked`) and against a keys
document that does not list the signer (`untrusted_kid` for every
statement and head).

Each `tampered/` file is one edit of a golden bundle and names the finding
the edit must produce: `statement_payload_mismatch`, `proof_invalid`,
`proof_missing` (+ `self_report_mismatch`, warning), `leaf_index_mismatch`,
`head_invalid` (+ `receipt_invalid`), `untrusted_kid`, `consistency_invalid`,
`consistency_missing`, `transparent_statement_mismatch`, `receipt_invalid`,
`bundle_digest_mismatch` (an edit to an extension, caught only when the
relying party supplies the digest the producer sealed),
`closure_parent_dropped` (a parent neither included nor declared missing),
`records_mode_mismatch`, `record_not_in_selector` (a validly signed and
proven record nobody selected).

Bundle verification order: structure and manifest; heads (each as above,
ascending sizes, the Transparent Statement must be the head it is carried
with, receipts under the pinned witness keys); consistency between
consecutive heads (`consistency_missing` when absent); every record's
semantics (`record_semantic_error`) and statement binding; every proof
against the head of its `tree_size`; closure (every chain parent is in the
bundle or in `missing`); selection (`record_ids` selectors admit only the
listed records and their closure); the self-reported counts; and the digest
when one is expected.

## Logs — `logs/*.json`

A 13-entry log whose entries are the UTF-8 bytes `brutor-log-vector-00` …
`-12`, given as `entries_hex`, with `leaf_hashes` (`SHA-256(0x00 ‖ entry)`)
and `roots` for every size 1..13. Interior nodes are
`SHA-256(0x01 ‖ left ‖ right)`; the split is the largest power of two
strictly below `n` (RFC 6962 §2.1).

* `inclusion.json` — `cases[]` `{index, tree_size, path[], root, expect: true}`
  for every leaf of sizes 1–8, 11 and 13; `negative[]` adds `entry_index`
  (the leaf that is really at index 5) and `description`: truncated, padded
  and bit-flipped paths, wrong index, wrong tree shape, root of a smaller
  tree — all `expect: false`. A verifier must check the path length against
  `(tree_size, index)` before hashing (RFC 9162 §2.1.3.2).
* `consistency.json` — `cases[]` `{first_size, second_size, first_root,
  second_root, path[], expect}` for every `1 ≤ m < n ≤ 13`, and `negative[]`
  (truncated, bit-flipped, rewritten prefix, swapped sizes, wrong first
  size).
* `store-nodes.json` — `nodes[]` `{level, index, hash}`: the node
  `(level, index)` is the MTH of leaves `[index·2^level, (index+1)·2^level)`
  and exists iff that range is full. Roots and inclusion paths of the other
  two files are derivable from this set alone; it is what a store-backed
  log (`brutor-ctlog`, and the control plane's `merkle.py`) persists.

The RFC 9162 published vectors are pinned in
`crates/brutor-cose/src/merkle.rs`, not duplicated here.

## Canonicalisation — `canon/jcs.json`

`cases[]` of `{description, input, canonical, sha256}` or
`{description, input, error: "float" | "unsafe_integer", path}`: member
ordering by UTF-16 code units (recursively), the surrogate-pair case, JSON
escaping (`\b \t \n \f \r \" \\`, `\u00xx` for the rest of C0, DEL and
non-ASCII literal, `/` not escaped, U+2028 literal), whitespace removal,
integers as plain decimals, empty containers, and the two refusals
(`1.5`, `1.0` — an integral spelling is still a float — and `2^53`).

## Reimplementing a verifier

The order a third-party verifier should follow, and what each corpus
directory proves about it:

1. **Canonicalise and digest** (`canon/`, `records/*/canonical`,
   `preimages`): RFC 8785 with the hygiene rules. Get the UTF-16 ordering
   right.
2. **COSE_Sign1** (`records/`, `heads/`): RFC 9052 §4.4 `Sig_structure` with
   empty external AAD, over the protected bytes as received; Ed25519 (RFC
   8032); reject non-unique map keys; `kid` = raw key; CWT claims at label
   **15**.
3. **Bind** the statement to the artefact: hash envelope payload ==
   `record_id` (records); Full-Content payload decodes to a head whose
   `log_id`/`tree_size` match `iss`/`sub` (heads).
4. **Trust** from a keys document; status `retired` is trusted, unknown is
   not; no document is a warning, not a pass.
5. **Semantic rules** 1–8 and the honesty check (`records/`), in order, all
   collected, only errors gate.
6. **Merkle** (`logs/`): RFC 6962 hashing, RFC 9162 iterative verification
   with the length check first; consistency as RFC 6962 section 2.1.2 and RFC 9162 section 2.1.4 define it.
7. **Receipts** (`receipts/`): RFC 9942 with `vds` from the protected header
   only; RFC 3161 tokens via any PKI toolkit, pinned by certificate digest.
8. **Bundles** (`bundles/`): the order above; every negative file names the
   code it must produce.

Report what each check established, never a verdict about the system the
evidence describes: `brutor-verify` and its page use no "verified",
"compliant" or "certified" wording (`cli_*` tests enforce it), and a relying
party draws conclusions from the checks, not from the tool.

## Interop

`interop/verify_with_scitt_cose.py` checks the corpus against the upstream
[`scitt-cose`](https://github.com/action-state-group/scitt-cose) package
(Apache-2.0): a clean-room Python implementation of COSE_Sign1 (RFC 9052),
Signed / Transparent Statements (RFC 9943), Receipts (RFC 9942, vds 1) and
RFC 6962 / RFC 9162 Merkle proofs that shares no code with the crates that
produced the corpus. It also carries its own RFC 8785 canonicaliser (~30
lines, UTF-16 ordering, float refusal) so the JCS bytes are checked by a
second implementation too, and optionally drives the upstream **Go**
verifier (`scitt-cose-go-verify`, veraison/go-cose + fxamacker/cbor) and
`openssl ts -verify` for the RFC 3161 tokens.

What it cross-checks: every record statement (envelope, signature under
the kid, labels 258/259 and no label 3, payload == recomputed `record_id`,
trust status against the keys document); every tree head (label 3, iss/sub
vs payload, embedded and supplied consistency proofs); every root, inclusion
path, consistency proof and stored node of the 13-entry log — **generated**
by upstream from the entries and compared to ours node for node, not only
verified; every `scitt` receipt (verdict, `tree_size`, `leaf_index`, `iat`,
no vendor-private labels) and RFC 3161 token; and the golden bundles: all
statements, both heads, every proof, the Transparent Statement's attached
receipt, and the newest head's root **recomputed from the entries** with
upstream's recursive MTH (the bundle carries every entry: the records plus
the earlier head's own log entry).

Run it (scitt-cose is not a dependency of this repo, so CI does not):

```text
python3 -m venv /tmp/scitt-venv
/tmp/scitt-venv/bin/pip install /path/to/scitt-cose        # or: pip install scitt-cose
SCITT_PYTHON=/tmp/scitt-venv/bin/python \
  cargo test -p brutor-verify --test vectors -- --ignored interop_scitt_cose
# with the Go verifier as well:
(cd /path/to/scitt-cose/scitt-cose-go-verify && go build -o /tmp/scitt-cose-go-verify .)
SCITT_PYTHON=... SCITT_GO_VERIFY=/tmp/scitt-cose-go-verify \
  cargo test -p brutor-verify --test vectors -- --ignored interop_scitt_cose
# or directly:
/tmp/scitt-venv/bin/python vectors/interop/verify_with_scitt_cose.py [--go /tmp/scitt-cose-go-verify] [-v]
```

Exit 0 means every check agreed; 1 lists each disagreement; 2 is a setup
problem.

### Recorded results

Last run against scitt-cose 0.3.0 (upstream commit 1091045,
Python 3.13, cbor2 6.1.4, cryptography 50.0.1), its Go verifier
built from the same checkout (go 1.22, veraison/go-cose 1.3.0,
fxamacker/cbor 2.9.2) and OpenSSL 3.6.2:

| section | checks | disagreements | what upstream confirmed |
|---|---|---|---|
| canon | 18 | 0 | every `canon/jcs.json` case: canonical bytes, digests, both refusals |
| records | 682 | 0 | 39 statements (Python and Go): signature verdicts incl. `neg/bad-signature`, kid, iss, no label 3, 258/259, payload == recomputed `record_id`; 41 records' JCS bytes and every literal preimage |
| heads | 123 | 0 | 9 heads + 5 previous heads (Python and Go): label 3, iss/sub vs payload, embedded proofs, the supplied non-adjacent proof, the fork |
| logs | 349 | 0 | 13 roots, 60 inclusion paths and 78 consistency proofs **regenerated** by upstream equal ours; 6 + 5 negative cases rejected; all 23 stored nodes |
| receipts | 22 | 0 | 4 `scitt` receipts (Python and Go, incl. wrong pin / wrong statement), `iat`, no vendor labels; 5 RFC 3161 tokens via `openssl ts -verify` (the wrong-statement token fails with `message imprint mismatch`) |
| bundles | 180 | 0 | 4 golden bundles: 10 record statements, 7 heads, 10 inclusion and 3 consistency proofs; `run-contiguous`'s two roots recomputed from the 7 entries by upstream's recursive MTH; the Transparent Statement carries the receipt, verifies, keeps the protected header; the receipt verifies under the witness key (Python and Go) |
| **total** | **1374** | **0** | Go verifier invoked 55 times |

`cargo test -p brutor-verify --test vectors -- --ignored interop_scitt_cose`
with `SCITT_PYTHON` and `SCITT_GO_VERIFY` set: passed.

Deviations found: **none**. Two behavioural differences that are not
conformance issues, handled by the script:

* upstream reports decoded receipt facts (`tree_size`, `leaf_index`, `iat`)
  and head facts before the signature check; `brutor-verify` reports them
  only once the artefact is accepted far enough to trust them. Facts are
  compared whenever ours are present, and required when a vector is ok.
* upstream is profile-agnostic: it reads a content type only from label 3,
  so a hash-envelope record statement has `content_type: None` there;
  the script reads labels 258/259 itself.

Not cross-checked by upstream: the semantic rules and the bundle
verifier (Brutor's own, vectorised here and pinned by the control plane's
parity test), and the bundle digest (control-plane parity test).

## What the corpus does not cover

* CCF receipts (`vds` 2), ES256/ES384 statements, and non-Ed25519 deployment
  keys — the platform issues none; `brutor-verify` reports them as
  unsupported rather than invalid.
* Receipts from live witnesses and public TSAs — those are the live tests,
  not vectors.
* The Agent Action Capsule profile — its vectors are upstream's, run
  by `crates/brutor-aac`.
* Framework obligation evidence (Part D) — control-plane data, not sealed
  artefacts.
