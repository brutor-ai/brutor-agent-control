// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `BRUTOR_LOG_JSON_ENABLED`: on, every log line is one JSON object carrying
//! what the text line carries (time, level, target, span, fields, message);
//! off (the default), the line is human-readable text.

use std::io;
use std::sync::{Arc, Mutex};

use brutor_skill_runner::env_registry::Env;
use brutor_skill_runner::env_vars::REGISTRY;
use brutor_skill_runner::logging;
use tracing_subscriber::fmt::MakeWriter;
use tracing_subscriber::layer::SubscriberExt;

/// Collects what the formatter writes.
#[derive(Clone, Default)]
struct Captured(Arc<Mutex<Vec<u8>>>);

impl Captured {
    fn text(&self) -> String {
        String::from_utf8(self.0.lock().unwrap().clone()).unwrap()
    }
}

impl io::Write for Captured {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<'a> MakeWriter<'a> for Captured {
    type Writer = Captured;
    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// The lines written for one event inside one span, under `pairs`.
fn one_event(pairs: &[(&str, &str)]) -> Vec<String> {
    let env = Env::from_pairs(&REGISTRY, pairs.iter().copied()).unwrap();
    let out = Captured::default();
    let subscriber = tracing_subscriber::registry().with(logging::format_layer(
        logging::json_enabled(&env),
        out.clone(),
    ));
    tracing::subscriber::with_default(subscriber, || {
        let span = tracing::info_span!("execute", execution_id = "exec-1");
        let _entered = span.enter();
        tracing::info!(exit_code = 0, "script finished");
    });
    out.text().lines().map(str::to_owned).collect()
}

#[test]
fn with_the_switch_on_a_log_line_is_one_json_object() {
    let lines = one_event(&[("BRUTOR_LOG_JSON_ENABLED", "true")]);
    assert_eq!(lines.len(), 1, "one event, one line: {lines:?}");
    let line: serde_json::Value =
        serde_json::from_str(&lines[0]).unwrap_or_else(|e| panic!("not JSON ({e}): {}", lines[0]));
    assert!(line["timestamp"].is_string(), "{line}");
    assert_eq!(line["level"], "INFO");
    assert_eq!(line["target"], "log_format");
    assert_eq!(line["fields"]["message"], "script finished");
    assert_eq!(line["fields"]["exit_code"], 0);
    assert_eq!(line["span"]["name"], "execute");
    assert_eq!(line["span"]["execution_id"], "exec-1");
}

#[test]
fn by_default_a_log_line_is_text() {
    let lines = one_event(&[]);
    assert_eq!(lines.len(), 1, "one event, one line: {lines:?}");
    assert!(serde_json::from_str::<serde_json::Value>(&lines[0]).is_err());
    let line = without_colour(&lines[0]);
    for part in [
        "INFO",
        "log_format",
        "execute",
        "exec-1",
        "exit_code=0",
        "script finished",
    ] {
        assert!(line.contains(part), "{part} missing from: {line}");
    }
}

/// The time of a line, in both forms: RFC 3339 in UTC, six digits of
/// fraction and `Z` (`2026-03-14T09:26:53.589793Z`). A log pipeline parses it.
#[test]
fn a_log_line_carries_its_time_in_utc_to_the_microsecond() {
    let json = one_event(&[("BRUTOR_LOG_JSON_ENABLED", "true")]);
    let line: serde_json::Value = serde_json::from_str(&json[0]).unwrap();
    let time = line["timestamp"].as_str().unwrap();
    assert!(is_utc_micros(time), "{time}");

    let text = without_colour(&one_event(&[])[0]);
    let time = text.split_whitespace().next().unwrap();
    assert!(is_utc_micros(time), "{text}");
}

/// `YYYY-MM-DDTHH:MM:SS.ffffffZ`, digits where the pattern has `9`.
fn is_utc_micros(time: &str) -> bool {
    const PATTERN: &str = "9999-99-99T99:99:99.999999Z";
    time.len() == PATTERN.len()
        && time.bytes().zip(PATTERN.bytes()).all(|(c, p)| {
            if p == b'9' {
                c.is_ascii_digit()
            } else {
                c == p
            }
        })
}

/// `line` without its ANSI colour sequences (`ESC [ … m`).
fn without_colour(line: &str) -> String {
    let mut out = String::new();
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            chars.by_ref().find(|&c| c == 'm');
        } else {
            out.push(c);
        }
    }
    out
}
