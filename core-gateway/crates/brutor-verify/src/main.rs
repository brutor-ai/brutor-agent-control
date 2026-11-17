// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `brutor-verify` — offline verification of Brutor evidence.
//!
//! ```text
//! brutor-verify bundle    <bundle.json>  [--keys well-known.json]... [--key HEX]... [--expected-digest HEX]
//!                                        [--witness-key HEX]... [--tsa-pin PEM-FILE|HEX]... [--json]
//! brutor-verify record    <record.json>  [--statement FILE | --statement-b64 B64] [--store FILE] [keys] [--json]
//! brutor-verify statement <FILE | --b64 B64> [--record FILE] [keys] [--json]
//! brutor-verify head      <FILE | --b64 B64> [--previous FILE | --previous-b64 B64] [--consistency HEX,...] [keys] [--json]
//! brutor-verify receipt   <FILE | --b64 B64> (--statement FILE | --statement-b64 B64) [--kind scitt|rfc3161] [--pin HEX|PEM-FILE] [--json]
//! ```
//!
//! Binary inputs (statements, heads, receipts) may be raw bytes or base64
//! text. Exit status: 0 = no error finding, 1 = at least one error finding,
//! 2 = usage or I/O error.

use brutor_verify::{BundleOptions, Report, TrustedKeys};
use clap::{Args, Parser, Subcommand};
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[derive(Parser)]
#[command(
    name = "brutor-verify",
    version,
    about = "Offline verification of Brutor evidence: action records, Signed Statements, tree heads, witness receipts and evidence bundles",
    long_about = None
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Args, Default)]
struct KeyArgs {
    /// A /.well-known/brutor-evidence-keys.json document (repeatable).
    #[arg(long = "keys", value_name = "FILE")]
    keys: Vec<PathBuf>,
    /// A trusted Ed25519 public key in hex (repeatable).
    #[arg(long = "key", value_name = "HEX")]
    key: Vec<String>,
}

#[derive(Args)]
struct Output {
    /// Print the structured report as JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Subcommand)]
enum Cmd {
    /// Verify an evidence bundle (ai.brutor/evidence-bundle/v1).
    Bundle {
        file: PathBuf,
        #[command(flatten)]
        keys: KeyArgs,
        /// The digest the producer sealed (its ai.brutor.bundle record).
        #[arg(long, value_name = "HEX")]
        expected_digest: Option<String>,
        /// Pinned Ed25519 key (hex) of a witness whose scitt receipts to check.
        #[arg(long = "witness-key", value_name = "HEX")]
        witness_key: Vec<String>,
        /// Pinned TSA certificate (PEM file or SHA-256 hex of its DER).
        #[arg(long = "tsa-pin", value_name = "PEM-FILE|HEX")]
        tsa_pin: Vec<String>,
        #[command(flatten)]
        out: Output,
    },
    /// Verify an action record and, optionally, its Signed Statement.
    Record {
        file: PathBuf,
        /// The record's Signed Statement (raw COSE or base64).
        #[arg(long, value_name = "FILE", conflicts_with = "statement_b64")]
        statement: Option<PathBuf>,
        #[arg(long, value_name = "B64")]
        statement_b64: Option<String>,
        /// A JSON array of records (log order) for the chain-parent check.
        #[arg(long, value_name = "FILE")]
        store: Option<PathBuf>,
        #[command(flatten)]
        keys: KeyArgs,
        #[command(flatten)]
        out: Output,
    },
    /// Verify a Signed Statement (record statement or tree head).
    Statement {
        #[arg(required_unless_present = "b64")]
        file: Option<PathBuf>,
        #[arg(long, value_name = "B64")]
        b64: Option<String>,
        /// The record the statement should commit to.
        #[arg(long, value_name = "FILE")]
        record: Option<PathBuf>,
        #[command(flatten)]
        keys: KeyArgs,
        #[command(flatten)]
        out: Output,
    },
    /// Verify a signed tree head and its consistency with an earlier head.
    Head {
        #[arg(required_unless_present = "b64")]
        file: Option<PathBuf>,
        #[arg(long, value_name = "B64")]
        b64: Option<String>,
        #[arg(long, value_name = "FILE", conflicts_with = "previous_b64")]
        previous: Option<PathBuf>,
        #[arg(long, value_name = "B64")]
        previous_b64: Option<String>,
        /// RFC 6962 consistency proof (hex nodes, comma-separated) from the
        /// previous head's size to this head's, for non-adjacent heads.
        #[arg(long, value_name = "HEX,...", value_delimiter = ',')]
        consistency: Vec<String>,
        #[command(flatten)]
        keys: KeyArgs,
        #[command(flatten)]
        out: Output,
    },
    /// Grade a witness receipt (scitt COSE Receipt or RFC 3161 token).
    Receipt {
        #[arg(required_unless_present = "b64")]
        file: Option<PathBuf>,
        #[arg(long, value_name = "B64")]
        b64: Option<String>,
        /// The head statement the receipt was issued for.
        #[arg(long, value_name = "FILE", required_unless_present = "statement_b64")]
        statement: Option<PathBuf>,
        #[arg(long, value_name = "B64")]
        statement_b64: Option<String>,
        /// scitt | rfc3161 (detected when absent).
        #[arg(long)]
        kind: Option<String>,
        /// Witness key hex (scitt) or TSA certificate PEM file / SHA-256 hex (rfc3161).
        #[arg(long, value_name = "HEX|PEM-FILE")]
        pin: Option<String>,
        #[command(flatten)]
        out: Output,
    },
}

type Fallible<T> = Result<T, String>;

fn read_text(p: &Path) -> Fallible<String> {
    std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))
}

fn read_json(p: &Path) -> Fallible<Value> {
    serde_json::from_str(&read_text(p)?).map_err(|e| format!("{}: not JSON: {e}", p.display()))
}

fn decode_b64(s: &str) -> Fallible<Vec<u8>> {
    use base64::Engine as _;
    let t: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    base64::engine::general_purpose::STANDARD
        .decode(t)
        .map_err(|e| format!("not base64: {e}"))
}

/// Raw bytes, or base64 text decoded when the file is base64.
fn read_binary(p: &Path) -> Fallible<Vec<u8>> {
    let raw = std::fs::read(p).map_err(|e| format!("{}: {e}", p.display()))?;
    if let Ok(t) = std::str::from_utf8(&raw)
        && !t.trim().is_empty()
        && t.trim()
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"+/=\r\n ".contains(&b))
        && let Ok(d) = decode_b64(t)
    {
        return Ok(d);
    }
    Ok(raw)
}

fn binary(file: &Option<PathBuf>, b64: &Option<String>, what: &str) -> Fallible<Vec<u8>> {
    match (file, b64) {
        (_, Some(b)) => decode_b64(b).map_err(|e| format!("{what}: {e}")),
        (Some(f), None) => read_binary(f),
        (None, None) => Err(format!("{what} is required")),
    }
}

fn load_keys(k: &KeyArgs) -> Fallible<TrustedKeys> {
    let mut keys = TrustedKeys::unpinned();
    for f in &k.keys {
        keys.extend(
            TrustedKeys::parse(&read_text(f)?).map_err(|e| format!("{}: {e}", f.display()))?,
        );
    }
    if !k.key.is_empty() {
        let mut extra = TrustedKeys::unpinned();
        extra.add_hex(&k.key).map_err(|e| e.to_string())?;
        keys.extend(extra);
    }
    Ok(keys)
}

/// A pin argument that names an existing file is read (PEM); anything else
/// is taken literally (hex).
fn pin_value(p: &str) -> Fallible<String> {
    let path = Path::new(p);
    if path.is_file() {
        read_text(path)
    } else {
        Ok(p.to_owned())
    }
}

fn run(cli: Cli) -> Fallible<(Report, bool)> {
    Ok(match cli.cmd {
        Cmd::Bundle {
            file,
            keys,
            expected_digest,
            witness_key,
            tsa_pin,
            out,
        } => {
            let v = read_json(&file)?;
            let (bundle, wrapped) = brutor_verify::unwrap_bundle(&v);
            let opts = BundleOptions {
                keys: load_keys(&keys)?,
                expected_digest: expected_digest
                    .map(|d| d.trim().to_ascii_lowercase())
                    .or(wrapped),
                witness_keys: witness_key,
                tsa_pins: tsa_pin
                    .iter()
                    .map(|p| pin_value(p))
                    .collect::<Fallible<_>>()?,
            };
            (brutor_verify::verify_bundle(bundle, &opts), out.json)
        }
        Cmd::Record {
            file,
            statement,
            statement_b64,
            store,
            keys,
            out,
        } => {
            let rec = read_json(&file)?;
            let st = match (&statement, &statement_b64) {
                (None, None) => None,
                _ => Some(binary(&statement, &statement_b64, "statement")?),
            };
            let store: Option<Vec<Value>> = match store {
                None => None,
                Some(p) => Some(
                    serde_json::from_value(read_json(&p)?)
                        .map_err(|e| format!("{}: not a JSON array: {e}", p.display()))?,
                ),
            };
            (
                brutor_verify::verify_record(
                    &rec,
                    st.as_deref(),
                    store.as_deref(),
                    &load_keys(&keys)?,
                ),
                out.json,
            )
        }
        Cmd::Statement {
            file,
            b64,
            record,
            keys,
            out,
        } => {
            let raw = binary(&file, &b64, "statement")?;
            let rec = record.as_deref().map(read_json).transpose()?;
            (
                brutor_verify::verify_statement(&raw, rec.as_ref(), &load_keys(&keys)?),
                out.json,
            )
        }
        Cmd::Head {
            file,
            b64,
            previous,
            previous_b64,
            consistency,
            keys,
            out,
        } => {
            let raw = binary(&file, &b64, "head")?;
            let prev = match (&previous, &previous_b64) {
                (None, None) => None,
                _ => Some(binary(&previous, &previous_b64, "previous head")?),
            };
            (
                brutor_verify::verify_head(
                    &raw,
                    prev.as_deref(),
                    if consistency.is_empty() {
                        None
                    } else {
                        Some(&consistency)
                    },
                    &load_keys(&keys)?,
                ),
                out.json,
            )
        }
        Cmd::Receipt {
            file,
            b64,
            statement,
            statement_b64,
            kind,
            pin,
            out,
        } => {
            let receipt = binary(&file, &b64, "receipt")?;
            let stmt = binary(&statement, &statement_b64, "statement")?;
            let pin = pin.as_deref().map(pin_value).transpose()?;
            (
                brutor_verify::verify_receipt(&receipt, &stmt, kind.as_deref(), pin.as_deref()),
                out.json,
            )
        }
    })
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok((report, json)) => {
            if json {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&report).unwrap_or_default()
                );
            } else {
                print!("{}", report.render_text());
            }
            ExitCode::from(report.exit_code as u8)
        }
        Err(e) => {
            eprintln!("brutor-verify: {e}");
            ExitCode::from(2)
        }
    }
}
