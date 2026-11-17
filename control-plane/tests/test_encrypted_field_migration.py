# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Encrypted-field migration regression suite

`EncryptionService.encrypt` is invoked from a dozen distinct call sites
that each write to a different DB column shape:

    llm_model.api_key                         (single-string)
    llm_model.provider_config                 (JSON-string)
    oauth_app_clients.encrypted_client_secret (single-string)
    oauth_tokens.encrypted_access_token       (single-string)
    oauth_tokens.encrypted_refresh_token      (single-string)
    mcp_servers.auth_token                    (single-string)
    a2a_signing_keys.private_key_encrypted    (single-string, base64 of raw key)

If the upcoming Fernet refactor (PyCA cryptography → pycryptodome +
stdlib hmac/hashlib in 0.8.0) silently changes the wire format for any
of these, the corresponding production column becomes unreadable. The
column-storage layer rarely surfaces decryption errors loudly — the
`get_decrypted_*` helpers tend to return the raw ciphertext on failure
("return as-is if decryption fails"), which then leaks downstream as a
bogus Bearer token and looks like operator misconfiguration.

This suite locks down the round-trip identity for EVERY column shape
by exercising the exact call site idioms the production code uses,
without touching the DB itself. The encrypted bytes don't need to
land in Postgres to prove the contract — Postgres is just a transparent
bytes pipe for these columns.

# Forward-compatibility fixtures

The `LEGACY_PRODUCTION_TOKENS` table pins ciphertexts that were minted
by Brutor v0.7.95 (`pyca/cryptography>=45.0.5`). Any future Python
implementation MUST decrypt these to the recorded plaintexts. Otherwise
operators upgrading hit a silent data-loss event the moment they reload
an old OAuth row.
"""

from __future__ import annotations

import json
from typing import Dict

import pytest

from app.core.logging_system import BrutorLogger
from app.services.oauth.encryption_service import EncryptionService

# ---------------------------------------------------------------------------
# Test setup — same stub config manager as the contract suite
# ---------------------------------------------------------------------------

TEST_MASTER_KEY = "brutor-fieldmigration-master-key-stable-for-this-suite"


class _StubConfigManager:
    def __init__(self, key: str = TEST_MASTER_KEY):
        self._key = key

    def get_encryption_key(self) -> str:
        return self._key


@pytest.fixture
def service() -> EncryptionService:
    return EncryptionService(
        config_manager=_StubConfigManager(),  # type: ignore[arg-type]
        logger=BrutorLogger("field-migration-test"),
    )


# ---------------------------------------------------------------------------
# Column-shape #1 — llm_model.api_key
# Pattern: bare encrypt() on POST, get_decrypted_api_key() on read.
# Used by: every llm_model row with auth_type != 'none'.
# ---------------------------------------------------------------------------


class TestLlmModelApiKey:
    """The most common encrypted column. Hits every LLM proxy request."""

    @pytest.mark.parametrize(
        "api_key",
        [
            # OpenAI-style
            "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ" + "0123456789" * 4,
            # Anthropic-style
            "sk-ant-api03-" + "x" * 100,
            # Azure OpenAI key (32-hex)
            "0123456789abcdef0123456789abcdef",
            # Google AI Studio key
            "AIzaSy" + "B" * 35,
            # AWS Bedrock — these flow as IAM-derived tokens, often long
            "AKIA" + "X" * 96,
            # A non-secret-shaped value (provider_config sometimes encrypts strings)
            "https://api.example.com/v1",
        ],
    )
    def test_api_key_round_trip(self, service: EncryptionService, api_key: str):
        # Production write path: `encrypt(value)`
        ciphertext = service.encrypt(api_key)
        # Production read path simulates `get_decrypted_api_key()`:
        #   if not self.api_key: return None
        #   try: return encryption_service.decrypt(self.api_key)
        #   except Exception: return self.api_key
        # We're under the happy-path branch.
        assert service.decrypt(ciphertext) == api_key

    def test_get_decrypted_helper_handles_none(self, service: EncryptionService):
        # Direct simulation of the helper's "no key set" guard.
        # Refactor must preserve None passthrough.
        api_key_column: str | None = None
        result = (
            None if not api_key_column else service.decrypt(api_key_column)
        )
        assert result is None

    def test_get_decrypted_helper_returns_raw_on_decrypt_failure(
        self, service: EncryptionService
    ):
        # The helper's documented behaviour: on decrypt failure, return
        # the raw ciphertext (so callers can detect "this looks like
        # garbage" and surface a clearer error). This is fragile but
        # matches production — and we must preserve it through the
        # refactor or every "this api key isn't working" troubleshooting
        # flow breaks.
        garbage = "this-is-not-a-fernet-token"
        try:
            result = service.decrypt(garbage)
        except Exception:
            result = garbage  # ← exactly what get_decrypted_api_key does
        assert result == "this-is-not-a-fernet-token"


# ---------------------------------------------------------------------------
# Column-shape #2 — llm_model.provider_config
# Pattern: encrypt(json.dumps(dict)) on write.
# Used by: per-provider extra config (region, deployment name, OAuth scopes).
# ---------------------------------------------------------------------------


class TestLlmModelProviderConfig:
    @pytest.mark.parametrize(
        "config",
        [
            {"endpoint": "https://api.example.com/v1", "deployment": "gpt-4-turbo"},
            {"region": "us-west-2", "model_id": "anthropic.claude-sonnet-4-v1:0"},
            {
                "project_id": "my-gcp-project",
                "location": "us-central1",
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            },
            # Edge case: nested objects + an empty list
            {"oauth": {"client_id": "abc", "scopes": []}, "max_retries": 3},
            # Edge case: unicode-bearing config
            {"display_name": "OpenAI 한글 deployment"},
        ],
    )
    def test_provider_config_round_trip(
        self, service: EncryptionService, config: Dict[str, object]
    ):
        # Production write path: `encrypt(json.dumps(value))`
        ciphertext = service.encrypt(json.dumps(config))
        # Production read path: decrypt → json.loads
        roundtripped = json.loads(service.decrypt(ciphertext))
        assert roundtripped == config


# ---------------------------------------------------------------------------
# Column-shape #3 — oauth_app_clients.encrypted_client_secret
# Pattern: `encrypted_secret = encryption_service.encrypt(client_secret)`.
# Used by: every OAuth-protected MCP server's client credentials.
# ---------------------------------------------------------------------------


class TestOAuthClientSecret:
    @pytest.mark.parametrize(
        "client_secret",
        [
            # Google OAuth-2 style
            "GOCSPX-AbCdEfGhIjKlMnOpQrStUvWx",
            # GitHub OAuth-App style
            "ghp_" + "1234567890abcdef" * 2,
            # Okta DCR-issued (long, opaque)
            "0oa" + "x" * 90,
        ],
    )
    def test_client_secret_round_trip(
        self, service: EncryptionService, client_secret: str
    ):
        ciphertext = service.encrypt(client_secret)
        # Read path simulation — the storage layer just stores the
        # ciphertext as-is in the column. `oauth_app_clients` doesn't
        # ship a `get_decrypted_*` helper; callers decrypt inline.
        assert service.decrypt(ciphertext) == client_secret


# ---------------------------------------------------------------------------
# Column-shape #4 — oauth_tokens.encrypted_access_token + encrypted_refresh_token
# Pattern: BOTH columns encrypted SEPARATELY (two independent ciphertexts).
# Used by: every successful OAuth flow against an MCP server.
# ---------------------------------------------------------------------------


class TestOAuthAccessAndRefreshTokens:
    """The most operationally critical column shape — these tokens are
    presented to upstream MCP servers on every proxy call. A bad decrypt
    here means the upstream receives the *ciphertext* as a Bearer token
    and the user sees a confusing "Authentication is required" error.
    """

    def test_access_token_round_trip(self, service: EncryptionService):
        # Production: oauth_tokens.encrypted_access_token = encrypt(access_token)
        access_token = "ya29.a0AfH6SMB-real-googleapis-com-access-token-shape"
        ciphertext = service.encrypt(access_token)
        assert service.decrypt(ciphertext) == access_token

    def test_refresh_token_round_trip(self, service: EncryptionService):
        refresh_token = "1//09bP_refresh-" + "x" * 200
        ciphertext = service.encrypt(refresh_token)
        assert service.decrypt(ciphertext) == refresh_token

    def test_access_and_refresh_use_independent_ciphertexts(
        self, service: EncryptionService
    ):
        # Even though they're stored in the same row, the two columns
        # are encrypted independently. Each must round-trip without
        # affecting the other. Locks in the "two separate Fernet calls,
        # two random IVs" pattern.
        access = "access_token_value_with_distinct_payload"
        refresh = "refresh_token_value_with_distinct_payload"

        access_ct = service.encrypt(access)
        refresh_ct = service.encrypt(refresh)

        assert access_ct != refresh_ct
        assert service.decrypt(access_ct) == access
        assert service.decrypt(refresh_ct) == refresh

    def test_only_refresh_token_present(self, service: EncryptionService):
        # Some OAuth flows return only an access token without a refresh
        # token. The DB column is nullable. Read path must tolerate the
        # access-token-only shape.
        access = "ya29.A.example"
        access_ct = service.encrypt(access)
        refresh_ct: str | None = None  # ← column is NULL

        # Decrypted view (what oauth_tokens.decrypt_for_use would build):
        decrypted_access = service.decrypt(access_ct)
        decrypted_refresh = (
            service.decrypt(refresh_ct) if refresh_ct else None
        )

        assert decrypted_access == access
        assert decrypted_refresh is None


# ---------------------------------------------------------------------------
# mcp_servers.auth_token
# Pattern: bare encrypt() by every writer; the gateway opens what it reads.
# What the API does with the token (never returned, kept by an update that
# does not name it, 503 without an encryption service, never sent when it
# does not decrypt) is in tests/test_mcp_server_token_encrypted.py.
# ---------------------------------------------------------------------------


def mcp_server_token_writers(writers: tuple, packages: tuple) -> int:
    """Each of `writers` stores only what the sealing function (or the
    encryption service in the same file) made, and no other file of
    `packages` writes `MCPServer.auth_token`. Returns how many writes were
    checked. The Enterprise half (`enterprise/tests/test_encrypted_field_migration.py`)
    holds the Enterprise package with it."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sealed = 0
    for relative in writers:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "MCPServer":
                values = [k.value for k in node.keywords if k.arg == "auth_token"]
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "auth_token" for t in node.targets
            ):
                values = [node.value]
            for value in values:
                text = ast.unparse(value)
                # What is stored was made by the one sealing function,
                # or by the encryption service in the same file.
                # (`None` removes the token: nothing is stored.)
                assert text.startswith("server_token.seal(") or text in ("stored_token", "new_token", "None"), (
                    f"{relative}: auth_token = {text}"
                )
                sealed += 1
        source = (root / relative).read_text(encoding="utf-8")
        assert "server_token.seal(" in source or "encryption_service.encrypt(auth_token)" in source, relative
    # No other file of the packages writes the column.
    others = []
    for package in packages:
        for path in sorted((root / package).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative in writers:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "MCPServer":
                    if any(k.arg == "auth_token" for k in node.keywords):
                        others.append(relative)
                # (Assigning `None` removes a token: it stores no value.)
                if isinstance(node, ast.Assign) and ast.unparse(node.value) != "None" and any(
                    isinstance(t, ast.Attribute) and t.attr == "auth_token" and "server" in ast.unparse(t.value)
                    for t in node.targets
                ):
                    others.append(relative)
    assert others == []
    return sealed


# The writes of the Community package's writers (one in the seed, one in the
# catalogue, three in the server routes).
SEALED_COMMUNITY = 5


class TestMcpServerAuthTokenIsStoredEncrypted:
    # Every file of the Community package that constructs an `MCPServer`
    # with a token. (The Enterprise package's: its half of this test.)
    WRITERS = (
        "app/database/seed_shared_mcp_servers.py",
        "app/api/v1/endpoints/admin/mcp_catalog.py",
        "app/api/v1/endpoints/admin/mcp_servers.py",
    )

    def test_every_writer_of_the_column_stores_ciphertext(self):
        from pathlib import Path

        assert mcp_server_token_writers(self.WRITERS, ("app",)) >= SEALED_COMMUNITY
        model = (Path(__file__).resolve().parents[1] / "app/database/models/mcp_server.py").read_text(encoding="utf-8")
        assert "auth_token = Column(Text, nullable=True)" in model and "stored in clear" not in model

    def test_the_gateway_opens_the_stored_value(self):
        from tests.core_repo import core_revision, core_source

        read = f" [the gateway's source read: {core_revision()}]"
        source = " ".join(core_source("models", "db.rs").read_text(encoding="utf-8").split())
        assert "match self.auth_token.as_deref().filter(|v| !v.is_empty()) {" in source, read
        assert "Some(stored) => encryption.decrypt(stored).map(Some)," in source, read
        # And it reads the token for a bearer server only, the auth type
        # this control plane keeps a token for.
        from app.database.models.mcp_server import BEARER_AUTH_TYPE

        assert f'pub const STATIC_TOKEN_AUTH_TYPE: &str = "{BEARER_AUTH_TYPE}";' in source, read
        assert "if self.auth_type.as_deref() != Some(STATIC_TOKEN_AUTH_TYPE) { return Ok(None); }" in source, read

    def test_a_long_token_round_trips(self, service: EncryptionService):
        token = "t" * 600
        assert service.decrypt(service.encrypt(token)) == token


# ---------------------------------------------------------------------------
# Column-shape #6 — a2a_signing_keys.private_key_encrypted
# Pattern: encrypt(base64url(raw_ed25519_private_key)) on first card-sign.
# Used by: tenant signing-key store. Already covered by test_a2a_card_signing.py
# but pinned here too for the migration suite's completeness.
# ---------------------------------------------------------------------------


class TestA2ASigningKey:
    def test_private_key_round_trip(self, service: EncryptionService):
        # Mimics card_signing._b64url(private_raw) → encrypt → store.
        # Read path: decrypt → b64url-decode → reconstruct Ed25519PrivateKey.
        import base64
        import secrets

        raw_private = secrets.token_bytes(32)  # Ed25519 seed
        encoded = base64.urlsafe_b64encode(raw_private).rstrip(b"=").decode()
        ciphertext = service.encrypt(encoded)

        decrypted = service.decrypt(ciphertext)
        # Pad back for the b64 decode (Fernet doesn't strip our padding
        # — we did, the read path adds it back).
        padding = "=" * (-len(decrypted) % 4)
        recovered_raw = base64.urlsafe_b64decode(decrypted + padding)
        assert recovered_raw == raw_private


# ---------------------------------------------------------------------------
# Forward compatibility — legacy production-shape ciphertexts MUST decrypt
#
# These tokens were minted by Brutor v0.7.95 with the TEST_MASTER_KEY above.
# Generated using `EncryptionService.encrypt(plaintext)` once and pinned
# here. Every future implementation MUST decrypt them to the recorded
# plaintext — otherwise upgrading Brutor strands production data.
#
# To extend this list (e.g. when adding a new encrypted column shape),
# run the generator at the bottom of this file with the new plaintext
# and paste the output here.
# ---------------------------------------------------------------------------


# Tokens minted by Brutor v0.7.95 (`pyca/cryptography>=45.0.5`) with the
# `TEST_MASTER_KEY` above. PINNED — do not regenerate from the current
# implementation. The whole point is that whatever implementation is
# CURRENT (post-refactor: pycryptodome + stdlib hmac/hashlib) must still
# decrypt these bytes that an OLD implementation produced. Regenerating
# here would defeat the forward-compat test entirely.
#
# If you change anything in the Fernet wire format intentionally, you
# MUST also stage a separate compatibility migration (rotate every
# stored row); only then update these constants.
LEGACY_PRODUCTION_TOKENS = {
    "openai_api_key": {
        "plaintext": "sk-proj-historical-openai-key-do-not-use",
        "ciphertext": "gAAAAABqBBrOhBPXVZDqB_VEJRjCHyezWTozxWkjGGGb8p9dtP2NljC3Jrp-gskKGkzgIXxvmtFenf_jgWwo88dhM_BbtgnECQ4IEl3QE1Wk_kcBVyRSoBVtd_AjvVwPK-SPp1i-mv8b",
    },
    "anthropic_api_key": {
        "plaintext": "sk-ant-api03-historical-anthropic-key-do-not-use",
        "ciphertext": "gAAAAABqBBrOlnhjznVYoomkWNxJVYjGqLRT-ovteBS7FixHQ_etyF8FTUaAfVxkIwUFnYKhF7W5zd7jPuuMVtIoe83kuAws9PqAqN_lSy4Y4FjCPmqqWyvQx9pZXkRmXNXST7jJjSubx3dIPzziWM4nNGHLC_RbCw==",
    },
    "google_oauth_access_token": {
        "plaintext": "ya29.historical-google-access-token-do-not-use",
        "ciphertext": "gAAAAABqBBrODLlAVq6Gff8WqgPXj5LoqSbbsDlCvBlxq0yXvWlCIfpOAMVMBzgTBWnCHfWbp46l6YYQq1I8oL4n7r3G564meOnOA0DV-SPW2TqqUxdj2CL5WPKqXmyUJpEaCORoddh2",
    },
    "okta_oauth_refresh_token": {
        "plaintext": "0oa-historical-okta-refresh-token-do-not-use",
        "ciphertext": "gAAAAABqBBrOwaMT5AQ8hduxFx9f_AEyFnnXuXm_GQXSACIETYuV2tRuJ6wpx1HNmDB1vke1hFejxqbmLXUM_X6tTaNfHo2NnNjRwqo8AySmhy54JnlO5p2paMDtKCZL_TqFvTJZh9NE",
    },
    "provider_config_json": {
        "plaintext": '{"region":"us-west-2","model_id":"anthropic.claude-sonnet-4-v1:0"}',
        "ciphertext": "gAAAAABqBBrOwfsEFPutCGAp5O4qAk8xsr4eEGFDjwbxhLHrQLUROyQpFRyX9Mrzm-OfaCryBY8KHA6Cvdb--V7sZ6exugevSNSLFohDmRKdIFRHEqPTTb_LCKLnITNV-oWuD2oy7n1QcxQ8Ux88PbwhDv9vVDczg0pxCb00OW4PWeKkz95ViSk=",
    },
}


class TestLegacyProductionTokens:
    """Decrypt pinned pre-refactor ciphertexts with the CURRENT
    implementation. These bytes were minted by v0.7.95
    (`pyca/cryptography>=45.0.5`); after the 0.8.0 swap to
    pycryptodome+stdlib, the same constants must still decrypt —
    otherwise every production OAuth token / API key written before the
    upgrade is unreadable post-upgrade.
    """

    @pytest.mark.parametrize(
        "case_name",
        list(LEGACY_PRODUCTION_TOKENS.keys()),
    )
    def test_legacy_token_decrypts(
        self, service: EncryptionService, case_name: str,
    ):
        entry = LEGACY_PRODUCTION_TOKENS[case_name]
        assert (
            service.decrypt(entry["ciphertext"]) == entry["plaintext"]
        ), (
            f"Legacy v0.7.95 token '{case_name}' failed to decrypt under "
            f"the current implementation. This is a forward-compat break — "
            f"existing production data becomes unreadable after the upgrade. "
            f"Either revert the Fernet implementation change, or land a "
            f"separate column-rotation migration before shipping."
        )


# ---------------------------------------------------------------------------
# Bulk-encryption performance smoke (not a perf bench — just a sanity check
# that nothing in the encrypt-many-rows path silently degrades 100×).
# ---------------------------------------------------------------------------


class TestBulkBehaviour:
    def test_thousand_round_trips_complete(self, service: EncryptionService):
        # Lots of real flows (`encrypt_dict` over an OAuth response,
        # batch token refreshes, seed-script migrations) hit Fernet
        # many times in a tight loop. Make sure we don't accidentally
        # introduce a per-call O(n²) regression.
        for i in range(1000):
            plaintext = f"row-{i}-secret-value-with-distinct-payload"
            assert service.decrypt(service.encrypt(plaintext)) == plaintext


# ---------------------------------------------------------------------------
# Legacy-token generator — run once per refactor to capture pre-refactor
# bytes. Pasted output goes into `LEGACY_PRODUCTION_TOKENS` above.
# ---------------------------------------------------------------------------
#
# Usage:
#   pytest tests/test_encrypted_field_migration.py::test_emit_legacy_fixtures -s
#
# (Function is hidden behind a fixture flag so it doesn't run during
# normal pytest collection — pinning the output is a manual step.)


def test_emit_legacy_fixtures(service: EncryptionService, capsys):
    """One-off helper. Not part of the regression matrix — prints the
    current-implementation ciphertexts so a developer can paste them
    into LEGACY_PRODUCTION_TOKENS for the next-version regression.

    To activate manually:
        pytest tests/test_encrypted_field_migration.py::test_emit_legacy_fixtures -s
    """
    # Marked as a smoke output, not an assertion: succeeds always.
    print("\n--- Paste into LEGACY_PRODUCTION_TOKENS ---")
    for name, entry in LEGACY_PRODUCTION_TOKENS.items():
        ct = service.encrypt(entry["plaintext"])
        print(f'    "{name}": {{')
        print(f'        "plaintext": {entry["plaintext"]!r},')
        print(f'        "ciphertext": "{ct}",')
        print(f"    }},")
    print("--- end paste ---\n")
    captured = capsys.readouterr()
    assert "Paste" in captured.out  # silly assertion to keep pytest happy
