// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! RFC 3161 Time-Stamp Protocol — the `rfc3161` witness kind.
//!
//! A timestamp authority (TSA) signs "this digest existed at time T". It
//! says nothing about ordering or inclusion — a tree head that carries only
//! a timestamp token is *dated*, not *witnessed* — but it is universally
//! accepted, and a relying party can check it with `openssl ts -verify`.
//!
//! This crate does exactly three things, all standards-only:
//!
//! * [`build_request`] — a DER `TimeStampReq` over SHA-256 of the message,
//!   with a nonce and `certReq = TRUE` (so the TSA embeds its certificate).
//! * [`token_from_response`] — unwrap a `TimeStampResp`, refusing any status
//!   other than `granted` / `grantedWithMods`.
//! * [`verify_token`] — offline verification of a `TimeStampToken`
//!   (`ContentInfo` → `SignedData` → `TSTInfo`) against a **pinned** TSA
//!   certificate: the message imprint must equal the message digest, the
//!   signed attributes must bind the `TSTInfo` (content type and message
//!   digest), the signer must be the pinned certificate, and the CMS
//!   signature must verify under its key. RSA PKCS#1 v1.5 (SHA-256/384) and
//!   ECDSA P-256/P-384 (SHA-256/384) are accepted; anything else is
//!   `invalid`, never silently trusted.
//!
//! The pin is either the certificate itself (PEM) or the SHA-256 of its DER
//! (64 hex) — the form that fits `witnesses.pinned_key`; with a fingerprint
//! pin the certificate is taken from the token's own certificate set and
//! trusted only because its hash matches.
//!
//! Grades never go up: no pin ⇒ [`Grade::Unverified`] (not evidence of
//! forgery), any failed check ⇒ [`Grade::Invalid`].

use cms::cert::CertificateChoices;
use cms::content_info::ContentInfo;
use cms::signed_data::{SignedData, SignerIdentifier, SignerInfo};
use der::asn1::{Int, ObjectIdentifier, OctetString};
use der::{Any, Decode, DecodePem, Encode};
use sha2::{Digest, Sha256, Sha384, Sha512};
use x509_cert::Certificate;
use x509_tsp::{MessageImprint, TimeStampReq, TimeStampResp, TspVersion, TstInfo};

pub mod oid {
    use der::asn1::ObjectIdentifier as O;
    pub const SIGNED_DATA: O = O::new_unwrap("1.2.840.113549.1.7.2");
    pub const CT_TST_INFO: O = O::new_unwrap("1.2.840.113549.1.9.16.1.4");
    pub const ATTR_CONTENT_TYPE: O = O::new_unwrap("1.2.840.113549.1.9.3");
    pub const ATTR_MESSAGE_DIGEST: O = O::new_unwrap("1.2.840.113549.1.9.4");
    pub const SHA256: O = O::new_unwrap("2.16.840.1.101.3.4.2.1");
    pub const SHA384: O = O::new_unwrap("2.16.840.1.101.3.4.2.2");
    pub const SHA512: O = O::new_unwrap("2.16.840.1.101.3.4.2.3");
    pub const RSA_ENCRYPTION: O = O::new_unwrap("1.2.840.113549.1.1.1");
    pub const SHA256_WITH_RSA: O = O::new_unwrap("1.2.840.113549.1.1.11");
    pub const SHA384_WITH_RSA: O = O::new_unwrap("1.2.840.113549.1.1.12");
    pub const EC_PUBLIC_KEY: O = O::new_unwrap("1.2.840.10045.2.1");
    pub const ECDSA_SHA256: O = O::new_unwrap("1.2.840.10045.4.3.2");
    pub const ECDSA_SHA384: O = O::new_unwrap("1.2.840.10045.4.3.3");
    pub const CURVE_P256: O = O::new_unwrap("1.2.840.10045.3.1.7");
    pub const CURVE_P384: O = O::new_unwrap("1.3.132.0.34");
    pub const EXT_KEY_USAGE: O = O::new_unwrap("2.5.29.37");
    pub const KP_TIME_STAMPING: O = O::new_unwrap("1.3.6.1.5.5.7.3.8");
}

pub const REQUEST_CONTENT_TYPE: &str = "application/timestamp-query";
pub const RESPONSE_CONTENT_TYPE: &str = "application/timestamp-reply";

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum TspError {
    #[error("der: {0}")]
    Der(String),
    #[error("TSA refused the request (PKIStatus {0})")]
    Refused(u8),
    #[error("TimeStampResp carries no token")]
    NoToken,
    #[error("pinned key is neither a PEM certificate nor a 64-hex SHA-256 fingerprint")]
    BadPin,
}

impl From<der::Error> for TspError {
    fn from(e: der::Error) -> Self {
        Self::Der(e.to_string())
    }
}

/// DER `TimeStampReq` over SHA-256(`message`), `certReq = TRUE`.
pub fn build_request(message: &[u8], nonce: u64) -> Result<Vec<u8>, TspError> {
    let digest = Sha256::digest(message);
    let req = TimeStampReq {
        version: TspVersion::V1,
        message_imprint: MessageImprint {
            hash_algorithm: spki::AlgorithmIdentifier {
                oid: oid::SHA256,
                parameters: Some(Any::null()),
            },
            hashed_message: OctetString::new(digest.to_vec())?,
        },
        req_policy: None,
        nonce: Some(Int::new(&nonce_bytes(nonce))?),
        cert_req: true,
        extensions: None,
    };
    Ok(req.to_der()?)
}

/// The nonce as a positive DER INTEGER body (leading zero when the top bit
/// is set), so the value round-trips through [`TokenInfo::nonce_hex`].
pub fn nonce_bytes(nonce: u64) -> Vec<u8> {
    let raw = nonce.to_be_bytes();
    let first = raw.iter().position(|b| *b != 0).unwrap_or(7);
    let mut out = raw[first..].to_vec();
    if out[0] & 0x80 != 0 {
        out.insert(0, 0);
    }
    out
}

/// Unwrap a `TimeStampResp` to its `TimeStampToken` DER. `granted` (0) and
/// `grantedWithMods` (1) are accepted; every other status is a refusal.
pub fn token_from_response(resp_der: &[u8]) -> Result<Vec<u8>, TspError> {
    let resp = TimeStampResp::from_der(resp_der)?;
    let status = resp.status.status as u8;
    if status > 1 {
        return Err(TspError::Refused(status));
    }
    let token = resp.time_stamp_token.ok_or(TspError::NoToken)?;
    Ok(token.to_der()?)
}

/// What the witness row pins.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pin {
    /// The TSA certificate (DER), from a PEM pin.
    Certificate(Vec<u8>),
    /// SHA-256 of the TSA certificate DER.
    Fingerprint([u8; 32]),
}

impl Pin {
    pub fn parse(raw: &str) -> Result<Self, TspError> {
        let s = raw.trim();
        if s.starts_with("-----BEGIN") {
            let cert = Certificate::from_pem(s.as_bytes()).map_err(|_| TspError::BadPin)?;
            return Ok(Self::Certificate(cert.to_der()?));
        }
        let fp = hex::decode(s.replace(':', "").to_ascii_lowercase())
            .ok()
            .and_then(|b| <[u8; 32]>::try_from(b).ok())
            .ok_or(TspError::BadPin)?;
        Ok(Self::Fingerprint(fp))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Grade {
    /// Every check passed under the pinned certificate.
    Timestamped,
    /// No pin (or the pinned certificate is not available): nothing was
    /// checked, which is not evidence of forgery.
    Unverified,
    /// A check failed.
    Invalid,
}

impl Grade {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Timestamped => "timestamped",
            Self::Unverified => "unverified",
            Self::Invalid => "invalid",
        }
    }
}

/// Fields read from a token, reported whatever the grade.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TokenInfo {
    /// `genTime`, seconds since the Unix epoch.
    pub gen_time: i64,
    pub gen_time_rfc3339: String,
    pub serial_hex: String,
    pub policy: String,
    pub imprint_alg: String,
    pub imprint_hex: String,
    pub nonce_hex: Option<String>,
    /// SHA-256 of the signer certificate DER, when it was resolved.
    pub signer_cert_sha256: Option<String>,
    pub signature_alg: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TokenVerification {
    pub grade: Grade,
    pub info: Option<TokenInfo>,
    pub errors: Vec<String>,
}

impl TokenVerification {
    fn fail(info: Option<TokenInfo>, e: impl Into<String>) -> Self {
        Self {
            grade: Grade::Invalid,
            info,
            errors: vec![e.into()],
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HashAlg {
    Sha256,
    Sha384,
    Sha512,
}

impl HashAlg {
    fn from_oid(o: &ObjectIdentifier) -> Option<Self> {
        match *o {
            oid::SHA256 => Some(Self::Sha256),
            oid::SHA384 => Some(Self::Sha384),
            oid::SHA512 => Some(Self::Sha512),
            _ => None,
        }
    }
    fn digest(self, data: &[u8]) -> Vec<u8> {
        match self {
            Self::Sha256 => Sha256::digest(data).to_vec(),
            Self::Sha384 => Sha384::digest(data).to_vec(),
            Self::Sha512 => Sha512::digest(data).to_vec(),
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Sha256 => "SHA-256",
            Self::Sha384 => "SHA-384",
            Self::Sha512 => "SHA-512",
        }
    }
}

/// Accept either a full `TimeStampResp` or a bare `TimeStampToken`.
fn token_content_info(raw: &[u8]) -> Result<ContentInfo, TspError> {
    if let Ok(resp) = TimeStampResp::from_der(raw) {
        let status = resp.status.status as u8;
        if status > 1 {
            return Err(TspError::Refused(status));
        }
        return resp.time_stamp_token.ok_or(TspError::NoToken);
    }
    Ok(ContentInfo::from_der(raw)?)
}

/// Verify a timestamp token over `message` under `pin`. Never panics.
pub fn verify_token(raw: &[u8], message: &[u8], pin: Option<&Pin>) -> TokenVerification {
    let ci = match token_content_info(raw) {
        Ok(ci) => ci,
        Err(e) => return TokenVerification::fail(None, format!("MALFORMED_TOKEN: {e}")),
    };
    if ci.content_type != oid::SIGNED_DATA {
        return TokenVerification::fail(None, "NOT_SIGNED_DATA");
    }
    let sd = match ci
        .content
        .to_der()
        .map_err(TspError::from)
        .and_then(|d| SignedData::from_der(&d).map_err(TspError::from))
    {
        Ok(sd) => sd,
        Err(e) => return TokenVerification::fail(None, format!("MALFORMED_SIGNED_DATA: {e}")),
    };
    if sd.encap_content_info.econtent_type != oid::CT_TST_INFO {
        return TokenVerification::fail(None, "ECONTENT_NOT_TSTINFO");
    }
    let Some(econtent) = sd.encap_content_info.econtent.as_ref() else {
        return TokenVerification::fail(None, "ECONTENT_ABSENT");
    };
    let tst_der = econtent.value().to_vec();
    let tst = match TstInfo::from_der(&tst_der) {
        Ok(t) => t,
        Err(e) => return TokenVerification::fail(None, format!("MALFORMED_TSTINFO: {e}")),
    };
    let imprint_alg = HashAlg::from_oid(&tst.message_imprint.hash_algorithm.oid);
    let gen_at = tst.gen_time.to_unix_duration();
    let mut info = TokenInfo {
        gen_time: gen_at.as_secs() as i64,
        gen_time_rfc3339: tst.gen_time.to_date_time().to_string(),
        serial_hex: hex::encode(tst.serial_number.as_bytes()),
        policy: tst.policy.to_string(),
        imprint_alg: imprint_alg
            .map(HashAlg::name)
            .unwrap_or("unsupported")
            .to_string(),
        imprint_hex: hex::encode(tst.message_imprint.hashed_message.as_bytes()),
        nonce_hex: tst.nonce.as_ref().map(|n| hex::encode(n.as_bytes())),
        signer_cert_sha256: None,
        signature_alg: None,
    };

    // 1. The token is about *this* message.
    let Some(imprint_alg) = imprint_alg else {
        return TokenVerification::fail(Some(info), "UNSUPPORTED_IMPRINT_ALG");
    };
    if imprint_alg.digest(message) != tst.message_imprint.hashed_message.as_bytes() {
        return TokenVerification::fail(Some(info), "MESSAGE_IMPRINT_MISMATCH");
    }

    // 2. Exactly one signer (RFC 3161 §2.4.2).
    let signers: Vec<&SignerInfo> = sd.signer_infos.0.iter().collect();
    if signers.len() != 1 {
        return TokenVerification::fail(
            Some(info),
            format!("EXPECTED_ONE_SIGNER: {}", signers.len()),
        );
    }
    let si = signers[0];

    // 3. Resolve the pinned certificate.
    let Some(pin) = pin else {
        return TokenVerification {
            grade: Grade::Unverified,
            info: Some(info),
            errors: vec!["NO_PINNED_CERTIFICATE".into()],
        };
    };
    let embedded: Vec<Vec<u8>> = sd
        .certificates
        .as_ref()
        .map(|set| {
            set.0
                .iter()
                .filter_map(|c| match c {
                    CertificateChoices::Certificate(cert) => cert.to_der().ok(),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default();
    let cert_der = match pin {
        Pin::Certificate(der) => der.clone(),
        Pin::Fingerprint(fp) => {
            match embedded
                .iter()
                .find(|d| Sha256::digest(d).as_slice() == fp.as_slice())
            {
                Some(d) => d.clone(),
                None => {
                    return TokenVerification {
                        grade: Grade::Unverified,
                        info: Some(info),
                        errors: vec!["PINNED_CERTIFICATE_NOT_IN_TOKEN".into()],
                    };
                }
            }
        }
    };
    info.signer_cert_sha256 = Some(hex::encode(Sha256::digest(&cert_der)));
    let cert = match Certificate::from_der(&cert_der) {
        Ok(c) => c,
        Err(e) => {
            return TokenVerification::fail(Some(info), format!("PINNED_CERT_MALFORMED: {e}"));
        }
    };

    // 4. The signer is the pinned certificate.
    let tbs = &cert.tbs_certificate;
    let sid_ok = match &si.sid {
        SignerIdentifier::IssuerAndSerialNumber(ias) => {
            ias.issuer == tbs.issuer && ias.serial_number == tbs.serial_number
        }
        SignerIdentifier::SubjectKeyIdentifier(ski) => tbs
            .extensions
            .as_ref()
            .and_then(|exts| {
                exts.iter()
                    .find(|e| e.extn_id == ObjectIdentifier::new_unwrap("2.5.29.14"))
            })
            .and_then(|e| OctetString::from_der(e.extn_value.as_bytes()).ok())
            .map(|v| v.as_bytes() == ski.0.as_bytes())
            .unwrap_or(false),
    };
    if !sid_ok {
        return TokenVerification::fail(Some(info), "SIGNER_IS_NOT_PINNED_TSA");
    }
    if !has_time_stamping_eku(&cert) {
        return TokenVerification::fail(Some(info), "TSA_CERT_LACKS_TIMESTAMPING_EKU");
    }
    let validity = &tbs.validity;
    let (nb, na) = (
        validity.not_before.to_unix_duration().as_secs(),
        validity.not_after.to_unix_duration().as_secs(),
    );
    if gen_at.as_secs() < nb || gen_at.as_secs() > na {
        return TokenVerification::fail(Some(info), "GENTIME_OUTSIDE_CERT_VALIDITY");
    }

    // 5. Signed attributes bind the TSTInfo.
    let Some(digest_alg) = HashAlg::from_oid(&si.digest_alg.oid) else {
        return TokenVerification::fail(Some(info), "UNSUPPORTED_DIGEST_ALG");
    };
    let Some(attrs) = si.signed_attrs.as_ref() else {
        return TokenVerification::fail(Some(info), "SIGNED_ATTRS_ABSENT");
    };
    let attr = |o: ObjectIdentifier| {
        attrs
            .iter()
            .find(|a| a.oid == o)
            .and_then(|a| a.values.iter().next())
    };
    let ct_ok = attr(oid::ATTR_CONTENT_TYPE)
        .and_then(|v| v.decode_as::<ObjectIdentifier>().ok())
        .map(|o| o == oid::CT_TST_INFO)
        .unwrap_or(false);
    if !ct_ok {
        return TokenVerification::fail(Some(info), "CONTENT_TYPE_ATTR_MISMATCH");
    }
    let md_ok = attr(oid::ATTR_MESSAGE_DIGEST)
        .and_then(|v| v.decode_as::<OctetString>().ok())
        .map(|md| md.as_bytes() == digest_alg.digest(&tst_der).as_slice())
        .unwrap_or(false);
    if !md_ok {
        return TokenVerification::fail(Some(info), "MESSAGE_DIGEST_ATTR_MISMATCH");
    }
    let signed_bytes = match attrs.to_der() {
        Ok(b) => b,
        Err(e) => return TokenVerification::fail(Some(info), format!("SIGNED_ATTRS_ENCODE: {e}")),
    };

    // 6. The CMS signature under the pinned key.
    let sig = si.signature.as_bytes();
    let spki = &tbs.subject_public_key_info;
    let key_bits = spki.subject_public_key.raw_bytes();
    let sig_oid = si.signature_algorithm.oid;
    let result: Result<&'static str, String> = match spki.algorithm.oid {
        oid::RSA_ENCRYPTION => {
            let hash = match sig_oid {
                oid::RSA_ENCRYPTION => Some(digest_alg),
                oid::SHA256_WITH_RSA => Some(HashAlg::Sha256),
                oid::SHA384_WITH_RSA => Some(HashAlg::Sha384),
                _ => None,
            };
            match hash {
                Some(h) if h == digest_alg && h != HashAlg::Sha512 => {
                    verify_rsa(key_bits, h, &signed_bytes, sig).map(|_| match h {
                        HashAlg::Sha256 => "RSA-PKCS1v15-SHA256",
                        _ => "RSA-PKCS1v15-SHA384",
                    })
                }
                _ => Err(format!("UNSUPPORTED_SIGNATURE_ALG: {sig_oid}")),
            }
        }
        oid::EC_PUBLIC_KEY => {
            let curve = spki
                .algorithm
                .parameters
                .as_ref()
                .and_then(|p| p.decode_as::<ObjectIdentifier>().ok());
            let hash = match sig_oid {
                oid::ECDSA_SHA256 => Some(HashAlg::Sha256),
                oid::ECDSA_SHA384 => Some(HashAlg::Sha384),
                _ => None,
            };
            match (curve, hash) {
                (Some(c), Some(h)) => verify_ecdsa(c, key_bits, h, &signed_bytes, sig),
                _ => Err(format!("UNSUPPORTED_SIGNATURE_ALG: {sig_oid}")),
            }
        }
        other => Err(format!("UNSUPPORTED_KEY_ALG: {other}")),
    };
    match result {
        Ok(name) => {
            info.signature_alg = Some(name.to_string());
            TokenVerification {
                grade: Grade::Timestamped,
                info: Some(info),
                errors: vec![],
            }
        }
        Err(e) => TokenVerification::fail(Some(info), e),
    }
}

fn has_time_stamping_eku(cert: &Certificate) -> bool {
    use x509_cert::ext::pkix::ExtendedKeyUsage;
    cert.tbs_certificate
        .extensions
        .as_ref()
        .and_then(|exts| exts.iter().find(|e| e.extn_id == oid::EXT_KEY_USAGE))
        .and_then(|e| ExtendedKeyUsage::from_der(e.extn_value.as_bytes()).ok())
        .map(|eku| eku.0.contains(&oid::KP_TIME_STAMPING))
        .unwrap_or(false)
}

fn verify_rsa(key: &[u8], h: HashAlg, msg: &[u8], sig: &[u8]) -> Result<(), String> {
    use rsa::pkcs1::DecodeRsaPublicKey;
    use rsa::{Pkcs1v15Sign, RsaPublicKey};
    let pk = RsaPublicKey::from_pkcs1_der(key).map_err(|e| format!("RSA_KEY: {e}"))?;
    let scheme = match h {
        HashAlg::Sha256 => Pkcs1v15Sign::new::<Sha256>(),
        HashAlg::Sha384 => Pkcs1v15Sign::new::<Sha384>(),
        HashAlg::Sha512 => Pkcs1v15Sign::new::<Sha512>(),
    };
    pk.verify(scheme, &h.digest(msg), sig)
        .map_err(|_| "BAD_TOKEN_SIGNATURE".to_string())
}

fn verify_ecdsa(
    curve: ObjectIdentifier,
    key: &[u8],
    h: HashAlg,
    msg: &[u8],
    sig: &[u8],
) -> Result<&'static str, String> {
    use p256::ecdsa::signature::hazmat::PrehashVerifier;
    let prehash = h.digest(msg);
    match curve {
        oid::CURVE_P256 => {
            let vk = p256::ecdsa::VerifyingKey::from_sec1_bytes(key)
                .map_err(|e| format!("EC_KEY: {e}"))?;
            let s = p256::ecdsa::Signature::from_der(sig)
                .map_err(|_| "BAD_TOKEN_SIGNATURE".to_string())?;
            vk.verify_prehash(&prehash, &s)
                .map_err(|_| "BAD_TOKEN_SIGNATURE".to_string())?;
            Ok(if h == HashAlg::Sha256 {
                "ECDSA-P256-SHA256"
            } else {
                "ECDSA-P256-SHA384"
            })
        }
        oid::CURVE_P384 => {
            let vk = p384::ecdsa::VerifyingKey::from_sec1_bytes(key)
                .map_err(|e| format!("EC_KEY: {e}"))?;
            let s = p384::ecdsa::Signature::from_der(sig)
                .map_err(|_| "BAD_TOKEN_SIGNATURE".to_string())?;
            vk.verify_prehash(&prehash, &s)
                .map_err(|_| "BAD_TOKEN_SIGNATURE".to_string())?;
            Ok(if h == HashAlg::Sha256 {
                "ECDSA-P384-SHA256"
            } else {
                "ECDSA-P384-SHA384"
            })
        }
        other => Err(format!("UNSUPPORTED_CURVE: {other}")),
    }
}

#[cfg(test)]
mod tests {
    //! Fixtures under `tests/fixtures/rfc3161/` (repo root) were generated
    //! with OpenSSL 3.6: a throwaway root (`ca.crt`) issued three TSA
    //! certificates (EKU timeStamping, critical); each `<name>.tsr` is a
    //! full `TimeStampResp` over `statement.bin` produced with
    //! `openssl ts -query -cert` + `openssl ts -reply` and checked with
    //! `openssl ts -verify`. `<name>.crt.sha256` is the SHA-256 of the
    //! certificate DER. The private keys were discarded; every tamper case
    //! below is derived from the genuine bytes.
    use super::*;

    const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/fixtures/rfc3161");

    fn read(name: &str) -> Vec<u8> {
        std::fs::read(format!("{DIR}/{name}")).unwrap()
    }
    fn text(name: &str) -> String {
        String::from_utf8(read(name)).unwrap()
    }
    fn statement() -> Vec<u8> {
        read("statement.bin")
    }

    #[test]
    fn genuine_tokens_verify_under_pem_and_fingerprint_pins() {
        for (name, alg) in [
            ("rsa2048", "RSA-PKCS1v15-SHA256"),
            ("ecp256", "ECDSA-P256-SHA256"),
            ("ecp384", "ECDSA-P384-SHA384"),
        ] {
            let resp = read(&format!("{name}.tsr"));
            let pem = Pin::parse(&text(&format!("{name}.crt"))).unwrap();
            let fp = Pin::parse(&text(&format!("{name}.crt.sha256"))).unwrap();
            for pin in [&pem, &fp] {
                let v = verify_token(&resp, &statement(), Some(pin));
                assert_eq!(v.grade, Grade::Timestamped, "{name}: {:?}", v.errors);
                let info = v.info.unwrap();
                assert_eq!(info.signature_alg.as_deref(), Some(alg));
                assert!(info.gen_time > 1_700_000_000);
                assert_eq!(info.policy, "1.2.3.4.1");
            }
            // The bare token verifies the same way as the full response.
            let token = token_from_response(&resp).unwrap();
            assert_eq!(
                verify_token(&token, &statement(), Some(&fp)).grade,
                Grade::Timestamped
            );
        }
    }

    #[test]
    fn no_pin_is_unverified_not_invalid() {
        let v = verify_token(&read("rsa2048.tsr"), &statement(), None);
        assert_eq!(v.grade, Grade::Unverified);
        assert!(v.info.is_some());
    }

    #[test]
    fn wrong_message_is_invalid() {
        let pin = Pin::parse(&text("ecp256.crt")).unwrap();
        let v = verify_token(&read("ecp256.tsr"), b"another statement", Some(&pin));
        assert_eq!(v.grade, Grade::Invalid);
        assert_eq!(v.errors, vec!["MESSAGE_IMPRINT_MISMATCH".to_string()]);
    }

    #[test]
    fn wrong_pinned_certificate_is_rejected() {
        // Pinning the P-256 TSA and presenting the RSA TSA's token.
        let pin = Pin::parse(&text("ecp256.crt")).unwrap();
        let v = verify_token(&read("rsa2048.tsr"), &statement(), Some(&pin));
        assert_eq!(v.grade, Grade::Invalid);
        assert_eq!(v.errors, vec!["SIGNER_IS_NOT_PINNED_TSA".to_string()]);
        // A fingerprint pin that matches no embedded certificate cannot be
        // checked — unverified, never timestamped.
        let fp = Pin::parse(&text("ecp256.crt.sha256")).unwrap();
        let v = verify_token(&read("rsa2048.tsr"), &statement(), Some(&fp));
        assert_eq!(v.grade, Grade::Unverified);
        // The root is not the signer either.
        let root = Pin::parse(&text("ca.crt")).unwrap();
        let v = verify_token(&read("rsa2048.tsr"), &statement(), Some(&root));
        assert_eq!(v.grade, Grade::Invalid);
    }

    /// Flip one byte inside `needle`'s first occurrence.
    fn flip_in(buf: &[u8], needle: &[u8], offset: usize) -> Vec<u8> {
        let pos = buf
            .windows(needle.len())
            .position(|w| w == needle)
            .expect("needle present");
        let mut out = buf.to_vec();
        out[pos + offset] ^= 0x01;
        out
    }

    #[test]
    fn tampered_imprint_is_detected() {
        // Flip a byte of the imprint inside the TSTInfo: the token no longer
        // names this statement.
        let resp = read("rsa2048.tsr");
        let imprint = Sha256::digest(statement());
        let tampered = flip_in(&resp, &imprint, 0);
        let pin = Pin::parse(&text("rsa2048.crt")).unwrap();
        let v = verify_token(&tampered, &statement(), Some(&pin));
        assert_eq!(v.grade, Grade::Invalid);
        assert_eq!(v.errors, vec!["MESSAGE_IMPRINT_MISMATCH".to_string()]);
    }

    #[test]
    fn tampered_signature_is_invalid() {
        for name in ["rsa2048", "ecp256", "ecp384"] {
            let resp = read(&format!("{name}.tsr"));
            let pin = Pin::parse(&text(&format!("{name}.crt"))).unwrap();
            // The signature is the last OCTET STRING of the SignerInfo, at
            // the very end of the response: flip its last byte.
            let mut t = resp.clone();
            let n = t.len();
            t[n - 1] ^= 0x01;
            let v = verify_token(&t, &statement(), Some(&pin));
            assert_eq!(v.grade, Grade::Invalid, "{name}");
            assert!(
                v.errors[0].starts_with("BAD_TOKEN_SIGNATURE"),
                "{name}: {:?}",
                v.errors
            );
        }
    }

    #[test]
    fn tampered_gen_time_breaks_the_signature_chain() {
        // Changing genTime changes the TSTInfo, so the signed messageDigest
        // attribute no longer matches.
        let resp = read("ecp256.tsr");
        let token = token_from_response(&resp).unwrap();
        let ci = ContentInfo::from_der(&token).unwrap();
        let sd = SignedData::from_der(&ci.content.to_der().unwrap()).unwrap();
        let tst = sd.encap_content_info.econtent.unwrap().value().to_vec();
        let parsed = TstInfo::from_der(&tst).unwrap();
        let gt = parsed.gen_time.to_der().unwrap();
        // gt = 18 0f YYYYMMDDhhmmssZ: move the year forward by one (still
        // inside the certificate's validity).
        let tampered = flip_in(&resp, &gt, 5);
        let pin = Pin::parse(&text("ecp256.crt")).unwrap();
        let v = verify_token(&tampered, &statement(), Some(&pin));
        assert_eq!(v.grade, Grade::Invalid);
        assert_eq!(v.errors, vec!["MESSAGE_DIGEST_ATTR_MISMATCH".to_string()]);
    }

    #[test]
    fn garbage_and_refusals_are_named() {
        let v = verify_token(b"not der", &statement(), None);
        assert_eq!(v.grade, Grade::Invalid);
        assert!(v.errors[0].starts_with("MALFORMED_TOKEN"));
        // TimeStampResp { status { rejection(2) } }
        let refused = [0x30, 0x05, 0x30, 0x03, 0x02, 0x01, 0x02];
        assert_eq!(token_from_response(&refused), Err(TspError::Refused(2)));
        assert!(Pin::parse("xyz").is_err());
    }

    #[test]
    fn request_round_trips() {
        let req = build_request(b"head", 0x8000_0000_0000_0001).unwrap();
        let parsed = TimeStampReq::from_der(&req).unwrap();
        assert!(parsed.cert_req);
        assert_eq!(
            parsed.message_imprint.hashed_message.as_bytes(),
            Sha256::digest(b"head").as_slice()
        );
        assert_eq!(
            hex::encode(parsed.nonce.unwrap().as_bytes()),
            "008000000000000001"
        );
        assert_eq!(nonce_bytes(1), vec![1]);
    }
}
