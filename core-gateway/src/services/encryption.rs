// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Fernet-compatible encryption service.
//
// Python's cryptography.fernet.Fernet uses:
//   - Key: 32 bytes (16 signing + 16 encryption), base64url-encoded
//   - Format: version(1) || timestamp(8) || iv(16) || ciphertext || hmac(32)
//   - Encryption: AES-128-CBC with PKCS7 padding
//   - Authentication: HMAC-SHA256 over version || timestamp || iv || ciphertext
//   - Result: base64url-encoded
//
// The Python EncryptionService derives the Fernet key from a master password
// using PBKDF2-HMAC-SHA256 with a static salt, then base64-encodes it.

use aes::cipher::{BlockDecryptMut, BlockEncryptMut, KeyIvInit};
use base64::{
    Engine,
    engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD},
};
use hmac::{Hmac, Mac};
use rand::RngCore;
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

// Python's `cryptography.fernet.Fernet` emits unpadded base64url
// (the library strips trailing `=`). When the proxy reads a value
// that the control-plane wrote, the string is unpadded; using the
// padded `URL_SAFE` engine here would error with "Invalid padding"
// on every cross-language decrypt. So:
//   - encode: NO-PAD to stay byte-identical to Python's output.
//   - decode: tolerant — pad the input up to a multiple of 4 with
//     `=`, then decode with the padded engine. Accepts both padded
//     and unpadded inputs, which keeps us safe whether the value
//     was produced by Python or by an older Rust build that used
//     the padded engine.
fn fernet_b64_encode(bytes: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(bytes)
}

fn fernet_b64_decode(s: &str) -> Result<Vec<u8>, base64::DecodeError> {
    let needed = (4 - s.len() % 4) % 4;
    if needed == 0 {
        URL_SAFE.decode(s.as_bytes())
    } else {
        let mut padded = String::with_capacity(s.len() + needed);
        padded.push_str(s);
        for _ in 0..needed {
            padded.push('=');
        }
        URL_SAFE.decode(padded.as_bytes())
    }
}

type Aes128CbcEnc = cbc::Encryptor<aes::Aes128>;
type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;
type HmacSha256 = Hmac<Sha256>;

const FERNET_VERSION: u8 = 0x80;
const SALT: &[u8] = b"brutor_mcp_gateway_salt_v1";
const PBKDF2_ITERATIONS: u32 = 100_000;

/// The prefix of a value written under a key provider. The product has no
/// key provider: stored secrets are encrypted under `ENCRYPTION_KEY` and
/// nothing else, and nothing writes this prefix. A value that carries it is
/// ciphertext this installation cannot read: [`has_ciphertext_shape`] says
/// so and [`EncryptionService::decrypt`] refuses it. The Control Plane's
/// encryption service recognises the same prefix.
const ENVELOPE_PREFIX: &str = "BAEv1:";

/// Whether a stored value has the shape of something the Control Plane's
/// encryption service writes, as opposed to a clear-text secret.
/// Decided without a key:
///
/// * it starts with the envelope prefix `BAEv1:`; or
/// * it is a Fernet token: base64url (padded or not) of at least 73 bytes
///   that are the version byte `0x80`, an 8-byte big-endian timestamp whose
///   upper four bytes are zero (any time before the year 2106), a 16-byte
///   IV, one or more whole 16-byte AES blocks, and a 32-byte HMAC. In text:
///   98 or more base64url characters beginning `gAAAA` followed by one of
///   `A`–`P`.
///
/// A real API key that begins `gAAAAA` is clear text unless the whole of it
/// also decodes to that layout: all base64url, 98 characters or more, and a
/// decoded length of 57 plus a multiple of 16. No provider's key format
/// does; one that did is treated as ciphertext and refused, never sent as
/// read. Refusing a usable key is the safe side of that mistake.
pub fn has_ciphertext_shape(value: &str) -> bool {
    if value.starts_with(ENVELOPE_PREFIX) {
        return true;
    }
    let Ok(raw) = fernet_b64_decode(value) else {
        return false;
    };
    raw.len() >= FERNET_MIN_LEN
        && (raw.len() - FERNET_OVERHEAD).is_multiple_of(16)
        && raw[0] == FERNET_VERSION
        && raw[1..5] == [0, 0, 0, 0]
}

/// version(1) + timestamp(8) + IV(16) + HMAC(32).
const FERNET_OVERHEAD: usize = 57;
/// The overhead plus one AES block.
const FERNET_MIN_LEN: usize = FERNET_OVERHEAD + 16;

/// Fernet-compatible encryption service that can interoperate with
/// Python's `cryptography.fernet.Fernet`.
#[derive(Clone)]
pub struct EncryptionService {
    /// First 16 bytes: HMAC signing key. Last 16 bytes: AES encryption key.
    fernet_key: [u8; 32],
}

impl EncryptionService {
    /// Create from the master encryption key (same as Python's ENCRYPTION_KEY env var).
    /// Derives the Fernet key using PBKDF2-HMAC-SHA256 with the static salt.
    pub fn new(master_key: &str) -> Self {
        let mut derived = [0u8; 32];
        pbkdf2::pbkdf2_hmac::<Sha256>(master_key.as_bytes(), SALT, PBKDF2_ITERATIONS, &mut derived);

        // Python does: base64.urlsafe_b64encode(derived_key)
        // Then Fernet(base64_key) which base64-decodes it back.
        // So the raw 32 bytes ARE the fernet key — the base64 round-trip is a no-op.
        Self {
            fernet_key: derived,
        }
    }

    /// Create directly from a base64url-encoded Fernet key (for testing).
    pub fn from_fernet_key_b64(key_b64: &str) -> Result<Self, EncryptionError> {
        let decoded =
            fernet_b64_decode(key_b64).map_err(|e| EncryptionError::InvalidKey(e.to_string()))?;
        if decoded.len() != 32 {
            return Err(EncryptionError::InvalidKey(format!(
                "Expected 32 bytes, got {}",
                decoded.len()
            )));
        }
        let mut key = [0u8; 32];
        key.copy_from_slice(&decoded);
        Ok(Self { fernet_key: key })
    }

    fn signing_key(&self) -> &[u8; 16] {
        self.fernet_key[..16].try_into().unwrap()
    }

    fn encryption_key(&self) -> &[u8; 16] {
        self.fernet_key[16..].try_into().unwrap()
    }

    /// Encrypt a plaintext string. Returns a base64url-encoded Fernet token,
    /// compatible with Python's Fernet.encrypt().
    pub fn encrypt(&self, plaintext: &str) -> Result<String, EncryptionError> {
        if plaintext.is_empty() {
            return Err(EncryptionError::EmptyInput);
        }

        let plaintext_bytes = plaintext.as_bytes();

        // PKCS7 padding
        let block_size = 16usize;
        let pad_len = block_size - (plaintext_bytes.len() % block_size);
        let mut padded = Vec::with_capacity(plaintext_bytes.len() + pad_len);
        padded.extend_from_slice(plaintext_bytes);
        padded.extend(std::iter::repeat(pad_len as u8).take(pad_len));

        // Random IV
        let mut iv = [0u8; 16];
        rand::rng().fill_bytes(&mut iv);

        // AES-128-CBC encrypt
        let encryptor = Aes128CbcEnc::new(self.encryption_key().into(), &iv.into());
        let ciphertext =
            encryptor.encrypt_padded_vec_mut::<aes::cipher::block_padding::NoPadding>(&padded);

        // Timestamp (big-endian u64)
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        // Build token: version || timestamp || iv || ciphertext
        let mut token_data = Vec::with_capacity(1 + 8 + 16 + ciphertext.len() + 32);
        token_data.push(FERNET_VERSION);
        token_data.extend_from_slice(&timestamp.to_be_bytes());
        token_data.extend_from_slice(&iv);
        token_data.extend_from_slice(&ciphertext);

        // HMAC-SHA256 over version || timestamp || iv || ciphertext
        let mut mac =
            HmacSha256::new_from_slice(self.signing_key()).expect("HMAC key length is valid");
        mac.update(&token_data);
        let hmac_result = mac.finalize().into_bytes();
        token_data.extend_from_slice(&hmac_result);

        Ok(fernet_b64_encode(&token_data))
    }

    /// Decrypt a Fernet token. Compatible with Python's Fernet.decrypt().
    /// Decrypt a Fernet-encrypted-at-rest string column with
    /// best-effort, log-on-failure semantics. Use this at every
    /// boundary that loads an `api_key` (or any other ciphertext
    /// column) directly from Postgres before forwarding to an
    /// upstream — the alternative pattern, sprinkling
    /// `match self.decrypt(...) { ... }` per call site, has
    /// historically diverged: some sites swallow errors silently,
    /// some return the ciphertext untouched, some log without
    /// blanking the field. All three failure modes lead to the
    /// upstream receiving the **encrypted** value as the bearer
    /// token, which providers reject with confusing
    /// "Authentication is required" / "Invalid API key" /
    /// "Application access denied" errors that look like operator
    /// misconfiguration but are really a Brutor decrypt bug.
    ///
    /// Contract:
    ///   - `Some(ciphertext)` → returns `Some(plaintext)` on
    ///     success, `None` on failure (with an `error!` log
    ///     including the supplied `context_label` so an operator
    ///     debugging "my key isn't working" can find the trail).
    ///   - `None` → returns `None` (pass-through; not a failure).
    ///
    /// `context_label` should identify the call site, e.g.
    /// `"video_pipeline:llm_model.api_key"` or
    /// `"search_server:provider_config.api_key"`.
    pub fn decrypt_optional(
        &self,
        ciphertext: Option<&str>,
        context_label: &str,
    ) -> Option<String> {
        let s = ciphertext?;
        match self.decrypt(s) {
            Ok(plain) => Some(plain),
            Err(e) => {
                tracing::error!(
                    context = %context_label,
                    "decrypt failed: {e} — upstream call will see no api_key"
                );
                None
            }
        }
    }

    /// Decrypt a stored secret. Every stored secret column holds ciphertext
    /// and nothing else: anything that does not decrypt is an error, a value
    /// that is not ciphertext included, and the caller refuses the row. No
    /// reader returns a stored value as the secret ("decrypt or leave" sent
    /// a ciphertext written under another `ENCRYPTION_KEY`, a value written
    /// under a key provider, or a value nobody encrypted, to the upstream
    /// as the credential).
    pub fn decrypt(&self, encrypted: &str) -> Result<String, EncryptionError> {
        if encrypted.is_empty() {
            return Err(EncryptionError::EmptyInput);
        }

        if encrypted.starts_with(ENVELOPE_PREFIX) {
            return Err(EncryptionError::DecryptionFailed(
                WRITTEN_UNDER_A_KEY_PROVIDER.into(),
            ));
        }

        let token_data = fernet_b64_decode(encrypted)
            .map_err(|e| EncryptionError::DecryptionFailed(format!("Base64 decode: {e}")))?;

        // Minimum: version(1) + timestamp(8) + iv(16) + at least 1 block(16) + hmac(32) = 73
        if token_data.len() < 73 {
            return Err(EncryptionError::DecryptionFailed("Token too short".into()));
        }

        // Verify version
        if token_data[0] != FERNET_VERSION {
            return Err(EncryptionError::DecryptionFailed(format!(
                "Invalid version: {:#x}",
                token_data[0]
            )));
        }

        let hmac_offset = token_data.len() - 32;
        let payload = &token_data[..hmac_offset];
        let stored_hmac = &token_data[hmac_offset..];

        // Verify HMAC
        let mut mac =
            HmacSha256::new_from_slice(self.signing_key()).expect("HMAC key length is valid");
        mac.update(payload);
        mac.verify_slice(stored_hmac)
            .map_err(|_| EncryptionError::DecryptionFailed("HMAC verification failed".into()))?;

        // Extract IV and ciphertext
        let iv: [u8; 16] = token_data[9..25]
            .try_into()
            .map_err(|_| EncryptionError::DecryptionFailed("Invalid IV".into()))?;
        let ciphertext = &token_data[25..hmac_offset];

        if ciphertext.is_empty() || ciphertext.len() % 16 != 0 {
            return Err(EncryptionError::DecryptionFailed(
                "Invalid ciphertext length".into(),
            ));
        }

        // AES-128-CBC decrypt
        let decryptor = Aes128CbcDec::new(self.encryption_key().into(), &iv.into());
        let mut buf = ciphertext.to_vec();
        let decrypted = decryptor
            .decrypt_padded_mut::<aes::cipher::block_padding::Pkcs7>(&mut buf)
            .map_err(|_| EncryptionError::DecryptionFailed("Decryption/padding failed".into()))?;

        String::from_utf8(decrypted.to_vec())
            .map_err(|e| EncryptionError::DecryptionFailed(format!("UTF-8 decode: {e}")))
    }
}

/// Why a value with the key-provider prefix is not decrypted.
const WRITTEN_UNDER_A_KEY_PROVIDER: &str = "the value was written under a key provider, and \
     this installation has none: stored secrets are encrypted under ENCRYPTION_KEY and nothing else";

#[derive(Debug, thiserror::Error)]
pub enum EncryptionError {
    #[error("Empty input")]
    EmptyInput,
    #[error("Invalid key: {0}")]
    InvalidKey(String),
    #[error("Decryption failed: {0}")]
    DecryptionFailed(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_round_trip() {
        let svc = EncryptionService::new("test-master-key");
        let plaintext = "hello world secret token";
        let encrypted = svc.encrypt(plaintext).unwrap();
        let decrypted = svc.decrypt(&encrypted).unwrap();
        assert_eq!(decrypted, plaintext);
    }

    // ---- Python -> Rust interop. The bug class that this catches:
    //   Python's `cryptography.fernet.Fernet` strips trailing `=`
    //   from base64url output, producing an UNPADDED string. Older
    //   Rust builds used the padded `URL_SAFE` engine on decode,
    //   which rejected the missing padding with "Invalid padding"
    //   and shipped the encrypted ciphertext to upstreams as the
    //   bearer token — surfacing as 401 "Missing bearer
    //   authentication in header" from OpenAI et al.
    //
    //   The vector below is produced by Python with a deterministic
    //   IV (0x00..0x0f), a fixed unix timestamp, and a fixed master
    //   key + plaintext. Reproducible via the snippet in
    //   `tools/generate_fernet_vector.py` (or by running the same
    //   PBKDF2/AES/HMAC sequence the production encryption service
    //   uses). If this test ever starts failing, the cross-language
    //   contract is broken — investigate before shipping.

    /// Master password (NOT a Fernet key — passed through PBKDF2).
    const PY_VECTOR_MASTER_KEY: &str = "test-fernet-interop-vector-do-not-rotate";
    /// What the plaintext should decrypt to.
    const PY_VECTOR_PLAINTEXT: &str = "sk-openai-fake-key-for-cross-language-decrypt-test";
    /// Python-produced ciphertext, UNPADDED base64url (no trailing `=`).
    /// Pinned — do NOT regenerate without coordinating a migration.
    const PY_VECTOR_CIPHERTEXT_UNPADDED: &str = "gAAAAABoI4igAAECAwQFBgcICQoLDA0OD-XtbwAwYWUpm_-bWWE0K_LWoFGuUqSsjh325SibmFl_DVFvYFU9as-NPHvGHlKFnOFgT7phm3LecGZ73qxwBs8wMzaJF84NZONw53rsS8OHBR-MHrmxCYSxJat0PXl2qA";

    #[test]
    fn decrypts_python_produced_unpadded_ciphertext() {
        // The exact production-bug repro: Rust must accept the
        // value the Python control plane wrote into the DB.
        let svc = EncryptionService::new(PY_VECTOR_MASTER_KEY);
        let recovered = svc
            .decrypt(PY_VECTOR_CIPHERTEXT_UNPADDED)
            .expect("Python-produced unpadded Fernet token must decrypt");
        assert_eq!(recovered, PY_VECTOR_PLAINTEXT);
    }

    #[test]
    fn decrypts_python_produced_ciphertext_with_added_padding() {
        // Some intermediaries (e.g. JSON round-trips, certain admin
        // UIs) may re-pad the value. The decoder must accept both
        // padded and unpadded forms so we don't paint ourselves
        // into a corner.
        let svc = EncryptionService::new(PY_VECTOR_MASTER_KEY);
        let len = PY_VECTOR_CIPHERTEXT_UNPADDED.len();
        let need = (4 - len % 4) % 4;
        let mut padded = PY_VECTOR_CIPHERTEXT_UNPADDED.to_string();
        for _ in 0..need {
            padded.push('=');
        }
        let recovered = svc
            .decrypt(&padded)
            .expect("Padded variant of the same token must also decrypt");
        assert_eq!(recovered, PY_VECTOR_PLAINTEXT);
    }

    #[test]
    fn rust_encode_is_unpadded_to_match_python_wire_format() {
        // Belt-and-braces: assert that Rust's own encrypt() never
        // emits trailing `=`. If this ever flips, Python's own
        // historic-ciphertext tests might break too.
        let svc = EncryptionService::new("test-master-key");
        let ct = svc.encrypt("anything").unwrap();
        assert!(
            !ct.ends_with('='),
            "Fernet output must be unpadded base64url to match Python",
        );
    }

    // ---- decrypt_optional — boundary helper used by every direct
    //     DB reader (video_pipeline, search_server, llm_router).
    //     Regression coverage for the bug class where Fernet
    //     ciphertext was being shipped to upstream providers as
    //     auth headers because callers forgot to decrypt.

    #[test]
    fn decrypt_optional_round_trips_real_ciphertext() {
        let svc = EncryptionService::new("test-master-key");
        let plaintext = "sk_brutor_api_VPBhQgykZX2uuetrFK47gOxCa";
        let ciphertext = svc.encrypt(plaintext).unwrap();
        let recovered = svc.decrypt_optional(Some(&ciphertext), "test:round-trip");
        assert_eq!(recovered.as_deref(), Some(plaintext));
    }

    #[test]
    fn decrypt_optional_passes_through_none() {
        let svc = EncryptionService::new("test-master-key");
        // None in → None out. Pass-through is critical because
        // every direct DB reader hands us `model.api_key.as_deref()`
        // and `None` is a perfectly valid "no key configured"
        // signal that mustn't be confused with a decrypt failure.
        assert!(svc.decrypt_optional(None, "test:none").is_none());
    }

    #[test]
    fn decrypt_optional_returns_none_on_garbage_ciphertext() {
        let svc = EncryptionService::new("test-master-key");
        // Operator with a plaintext key in a ciphertext column
        // (the search_provider_configs bug). The helper must
        // return `None` so the upstream call sees no api_key
        // (and 401s with a *meaningful* error) rather than
        // forwarding the plaintext as a Fernet token.
        let recovered = svc.decrypt_optional(
            Some("brave-plaintext-key-pretending-to-be-encrypted"),
            "test:plaintext-pretending",
        );
        assert!(recovered.is_none());
    }

    #[test]
    fn decrypt_optional_returns_none_on_empty_ciphertext() {
        // Edge: NULL columns sometimes round-trip as Some("") via
        // certain ORMs. Should be treated as a decrypt failure,
        // not as "decrypt of empty == empty".
        let svc = EncryptionService::new("test-master-key");
        assert!(svc.decrypt_optional(Some(""), "test:empty").is_none());
    }

    #[test]
    fn decrypt_optional_with_wrong_key_returns_none() {
        // Operator rotated ENCRYPTION_KEY without re-encrypting
        // existing rows. `decrypt_optional` must return None,
        // not panic, not return the ciphertext.
        let writer = EncryptionService::new("key-A");
        let reader = EncryptionService::new("key-B");
        let ciphertext = writer.encrypt("api-key-from-old-deployment").unwrap();
        let recovered = reader.decrypt_optional(Some(&ciphertext), "test:rotated-key");
        assert!(recovered.is_none());
    }

    #[test]
    fn test_different_encryptions_differ() {
        let svc = EncryptionService::new("test-master-key");
        let e1 = svc.encrypt("same text").unwrap();
        let e2 = svc.encrypt("same text").unwrap();
        // Different IVs → different ciphertexts
        assert_ne!(e1, e2);
        // But both decrypt to the same value
        assert_eq!(svc.decrypt(&e1).unwrap(), svc.decrypt(&e2).unwrap());
    }

    #[test]
    fn test_wrong_key_fails() {
        let svc1 = EncryptionService::new("key-one");
        let svc2 = EncryptionService::new("key-two");
        let encrypted = svc1.encrypt("secret").unwrap();
        assert!(svc2.decrypt(&encrypted).is_err());
    }

    // ---- the shape of ciphertext ------------

    #[test]
    fn what_the_encryption_service_writes_has_the_shape_of_ciphertext() {
        let svc = EncryptionService::new("k");
        for plain in ["x", "sk-made-up", &"long ".repeat(200)] {
            let token = svc.encrypt(plain).unwrap();
            assert!(has_ciphertext_shape(&token), "{} chars", token.len());
            // Padded, as an older writer produced it.
            let padded = format!("{token}{}", "=".repeat((4 - token.len() % 4) % 4));
            assert!(has_ciphertext_shape(&padded));
        }
        assert!(has_ciphertext_shape(PY_VECTOR_CIPHERTEXT_UNPADDED));
        for envelope in [
            "BAEv1:aws_kms:YWJjZA:gAAAAAtokenz",
            "BAEv1:vault:YWJjZA:gAAAAAtokenz",
            "BAEv1:local:YWJjZA:gAAAAAtokenz",
            "BAEv1:",
        ] {
            assert!(has_ciphertext_shape(envelope), "{envelope}");
        }
    }

    #[test]
    fn a_clear_secret_does_not_have_the_shape_of_ciphertext() {
        let long_key_beginning_like_a_token = format!("gAAAAA{}", "b".repeat(200));
        for clear in [
            "",
            "sk-made-up-key",
            // A real key may begin the way a Fernet token does.
            "gAAAAA-made-up-provider-key",
            "gAAAAABmadeUpKeyThatIsShort",
            // Long, all base64url, begins `gAAAAA`, wrong length for a token.
            long_key_beginning_like_a_token.as_str(),
            // An AWS secret access key (40 characters of base64).
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            // A JWT, and cloud credentials as JSON.
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLW1hZGUtdXA",
            r#"{"aws_access_key_id":"AKIDEXAMPLE","aws_region":"eu-west-1"}"#,
            "baev1:local:lowercase-is-not-the-prefix",
        ] {
            assert!(!has_ciphertext_shape(clear), "{clear}");
        }
        // The right length and alphabet, the wrong first byte.
        let token = EncryptionService::new("k").encrypt("x").unwrap();
        assert!(!has_ciphertext_shape(&format!("h{}", &token[1..])));
        // A timestamp past the year 2106 is not one the service writes.
        assert!(!has_ciphertext_shape(&format!("gA_AAA{}", &token[6..])));
    }

    /// The one case that is decided against a clear value: a secret that is
    /// itself, byte for byte, a well-formed Fernet token is ciphertext.
    #[test]
    fn a_clear_secret_that_is_a_whole_fernet_token_is_ciphertext_and_refused() {
        let someone_elses_token = EncryptionService::new("their-key").encrypt("x").unwrap();
        let svc = EncryptionService::new("k");
        assert!(has_ciphertext_shape(&someone_elses_token));
        assert!(svc.decrypt(&someone_elses_token).is_err());
    }

    #[test]
    fn decrypt_opens_its_own_ciphertext_and_refuses_everything_else() {
        let svc = EncryptionService::new("k");
        let token = svc.encrypt("sk-made-up").unwrap();
        assert_eq!(svc.decrypt(&token).unwrap(), "sk-made-up");
        assert!(svc.decrypt("sk-clear").is_err());
        let other = EncryptionService::new("another-key")
            .encrypt("sk-made-up")
            .unwrap();
        assert!(svc.decrypt(&other).is_err());
        for envelope in [
            format!("BAEv1:aws_kms:YWJjZA:{token}"),
            format!("BAEv1:vault:YWJjZA:{token}"),
            format!("BAEv1:someone:YWJjZA:{token}"),
            "BAEv1:".to_string(),
        ] {
            assert!(svc.decrypt(&envelope).is_err(), "{envelope}");
        }
    }

    #[test]
    fn test_empty_input_fails() {
        let svc = EncryptionService::new("key");
        assert!(svc.encrypt("").is_err());
        assert!(svc.decrypt("").is_err());
    }

    #[test]
    fn test_unicode_round_trip() {
        let svc = EncryptionService::new("test-master-key");
        let texts = [
            "\u{1F600}\u{1F601}\u{1F602}",      // emoji
            "\u{4F60}\u{597D}\u{4E16}\u{754C}", // CJK: 你好世界
            "\u{00E9}\u{00E8}\u{00EA}\u{00EB}", // accented Latin
            "\u{0410}\u{0411}\u{0412}\u{0413}", // Cyrillic
        ];
        for text in &texts {
            let encrypted = svc.encrypt(text).unwrap();
            let decrypted = svc.decrypt(&encrypted).unwrap();
            assert_eq!(&decrypted, text);
        }
    }

    #[test]
    fn test_long_text_round_trip() {
        let svc = EncryptionService::new("test-master-key");
        // Create a string > 10KB
        let long_text: String = "abcdefghij".repeat(1100); // 11_000 bytes
        assert!(long_text.len() > 10_000);
        let encrypted = svc.encrypt(&long_text).unwrap();
        let decrypted = svc.decrypt(&encrypted).unwrap();
        assert_eq!(decrypted, long_text);
    }

    #[test]
    fn test_from_fernet_key_b64_valid() {
        // A valid 32-byte key, base64url-encoded
        let key_bytes = [0x42u8; 32];
        let key_b64 = URL_SAFE.encode(key_bytes);
        let svc = EncryptionService::from_fernet_key_b64(&key_b64);
        assert!(svc.is_ok());
        // Verify round-trip works with this key
        let svc = svc.unwrap();
        let encrypted = svc.encrypt("test").unwrap();
        let decrypted = svc.decrypt(&encrypted).unwrap();
        assert_eq!(decrypted, "test");
    }

    #[test]
    fn test_from_fernet_key_b64_invalid_length() {
        // 16 bytes instead of 32
        let short_key = [0x42u8; 16];
        let key_b64 = URL_SAFE.encode(short_key);
        let result = EncryptionService::from_fernet_key_b64(&key_b64);
        match result {
            Err(EncryptionError::InvalidKey(msg)) => assert!(msg.contains("32 bytes")),
            Err(other) => panic!("Expected InvalidKey, got: {:?}", other),
            Ok(_) => panic!("Expected error, got Ok"),
        }
    }

    #[test]
    fn test_from_fernet_key_b64_invalid_base64() {
        let result = EncryptionService::from_fernet_key_b64("not!valid!base64!@#$%");
        match result {
            Err(EncryptionError::InvalidKey(_)) => {}
            Err(other) => panic!("Expected InvalidKey, got: {:?}", other),
            Ok(_) => panic!("Expected error, got Ok"),
        }
    }

    #[test]
    fn test_decrypt_garbage() {
        let svc = EncryptionService::new("test-master-key");
        // Random garbage that happens to be valid base64
        let garbage =
            URL_SAFE.encode(b"this is just random garbage data that is not a fernet token");
        let result = svc.decrypt(&garbage);
        assert!(result.is_err());
    }

    #[test]
    fn test_decrypt_truncated_token() {
        let svc = EncryptionService::new("test-master-key");
        let encrypted = svc.encrypt("hello world").unwrap();
        // Truncate the token (remove last 20 chars)
        let truncated = &encrypted[..encrypted.len() - 20];
        let result = svc.decrypt(truncated);
        assert!(result.is_err());
    }

    #[test]
    fn test_single_byte_round_trip() {
        let svc = EncryptionService::new("test-master-key");
        let encrypted = svc.encrypt("x").unwrap();
        let decrypted = svc.decrypt(&encrypted).unwrap();
        assert_eq!(decrypted, "x");
    }

    #[test]
    fn test_tampered_ciphertext_fails() {
        let svc = EncryptionService::new("test-master-key");
        let encrypted = svc.encrypt("sensitive data").unwrap();

        // Decode, flip a byte in the middle, re-encode.
        // Must go through the same Fernet-convention helpers the
        // production code uses — `encrypted` is unpadded base64url
        // (Python wire format), so the padded `URL_SAFE` engine
        // would reject it with "Invalid padding".
        let mut raw = fernet_b64_decode(&encrypted).unwrap();
        let mid = raw.len() / 2;
        raw[mid] ^= 0xFF;
        let tampered = fernet_b64_encode(&raw);

        let result = svc.decrypt(&tampered);
        assert!(result.is_err());
    }

    #[test]
    fn test_decrypt_wrong_version_byte() {
        let svc = EncryptionService::new("test-master-key");
        let encrypted = svc.encrypt("test").unwrap();

        let mut raw = fernet_b64_decode(&encrypted).unwrap();
        raw[0] = 0x00; // Wrong version
        let bad_version = fernet_b64_encode(&raw);

        match svc.decrypt(&bad_version) {
            Err(EncryptionError::DecryptionFailed(msg)) => {
                assert!(msg.contains("Invalid version") || msg.contains("HMAC"));
            }
            Err(_) => {} // Any error is acceptable
            Ok(_) => panic!("Expected decryption to fail with wrong version byte"),
        }
    }

    #[test]
    fn test_exact_block_size_plaintext() {
        let svc = EncryptionService::new("test-master-key");
        // Exactly 16 bytes — PKCS7 should add a full block of padding
        let plaintext = "0123456789abcdef";
        assert_eq!(plaintext.len(), 16);
        let encrypted = svc.encrypt(plaintext).unwrap();
        let decrypted = svc.decrypt(&encrypted).unwrap();
        assert_eq!(decrypted, plaintext);
    }

    /// A value with the key-provider prefix is refused whatever follows the
    /// prefix: a provider this product once named, any other, a value that
    /// carries a data key and a token made under this very key, nothing at
    /// all. The refusal is the unreadable-secret error and says why.
    #[test]
    fn a_value_written_under_a_key_provider_is_refused() {
        let svc = EncryptionService::new("test-master-key");
        let own_token = svc.encrypt("hello").unwrap();
        let own_key = fernet_b64_encode(&svc.fernet_key);
        for value in [
            format!("{ENVELOPE_PREFIX}aws_kms:YWJjZA:gAAAAAtokenz"),
            format!("{ENVELOPE_PREFIX}vault:YWJjZA:gAAAAAtokenz"),
            format!("{ENVELOPE_PREFIX}local:{own_key}:{own_token}"),
            format!("{ENVELOPE_PREFIX}someone:YWJjZA:{own_token}"),
            format!("{ENVELOPE_PREFIX}local:abc"),
            format!("{ENVELOPE_PREFIX}{own_token}"),
            ENVELOPE_PREFIX.to_string(),
        ] {
            for reader in [&svc, &EncryptionService::new("another-master-key")] {
                match reader.decrypt(&value) {
                    Err(e @ EncryptionError::DecryptionFailed(_)) => assert_eq!(
                        e.to_string(),
                        "Decryption failed: the value was written under a key provider, and \
                         this installation has none: stored secrets are encrypted under \
                         ENCRYPTION_KEY and nothing else",
                        "{value}"
                    ),
                    other => panic!("{value}: expected DecryptionFailed, got {other:?}"),
                }
            }
        }
    }

    /// The refusal names no setting: there is none to change.
    #[test]
    fn the_key_provider_refusal_advises_no_setting() {
        for stale in ["BRUTOR_KEY_PROVIDER", "KMS", "re-encrypt", "provider="] {
            assert!(
                !WRITTEN_UNDER_A_KEY_PROVIDER.contains(stale),
                "the refusal mentions `{stale}`"
            );
        }
    }

    #[test]
    fn a_token_without_the_prefix_decrypts() {
        let svc = EncryptionService::new("test-master-key");
        let token = svc.encrypt("stored secret").unwrap();
        assert!(!token.starts_with(ENVELOPE_PREFIX));
        assert_eq!(svc.decrypt(&token).unwrap(), "stored secret");
    }
}
