// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The environment-variable registry: the one place this service reads its
//! environment.
//!
//! A variable is defined once, as an [`EnvVar`] (name, type, default, whether
//! it is a secret, edition, description, example and accepted values). The
//! service's table of them is `src/env_vars.rs`.
//! Everything else follows from that table:
//!
//! * configuration is read through [`Env`], with the variable's definition as
//!   the key, so a name that is not registered cannot be read;
//! * a malformed value, and a missing required variable, stop the service at
//!   start, one line per name and never the value ([`Problem::Invalid`],
//!   [`Problem::Missing`]);
//! * `.env.example`, the README's configuration table and the JSON printed by
//!   `--print-env` are generated from it ([`Registry::env_example`],
//!   [`Registry::markdown`], [`Registry::json`]).
//!
//! No value of any variable is ever written by this module: not in an error,
//! not in `Debug` output, not in the generated files.
//!
//! This file is the same in each Rust service of the product that reads its
//! environment through it (they share no crate). Change it in all of them.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;

/// Version of the JSON document [`Registry::json`] produces.
pub const SCHEMA_VERSION: u32 = 2;

/// The flag that makes the binary print the registry as JSON and exit.
pub const PRINT_FLAG: &str = "--print-env";

/// What a value must look like. Checked for every variable that is set,
/// before the service does anything else.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// Any text.
    String,
    /// A whole number written in digits only.
    Integer,
    /// `true`, `false`, `1`, `0`, `yes`, `no`, `on`, `off` (any case).
    Boolean,
    /// `scheme://…`.
    Url,
    /// `host:port`.
    SocketAddr,
    /// A file-system path.
    Path,
    /// Comma-separated items.
    List,
}

impl Kind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Kind::String => "string",
            Kind::Integer => "integer",
            Kind::Boolean => "boolean",
            Kind::Url => "url",
            Kind::SocketAddr => "socket_addr",
            Kind::Path => "path",
            Kind::List => "list",
        }
    }

    /// What a value of this kind must be, when `raw` is not one.
    fn mismatch(self, raw: &str) -> Option<&'static str> {
        let ok = match self {
            Kind::String | Kind::Path | Kind::List => true,
            Kind::Integer => {
                !raw.is_empty()
                    && raw.bytes().all(|b| b.is_ascii_digit())
                    && raw.parse::<u64>().is_ok()
            }
            Kind::Boolean => parse_bool(raw).is_some(),
            Kind::Url => looks_like_url(raw),
            Kind::SocketAddr => raw
                .rsplit_once(':')
                .is_some_and(|(host, port)| !host.is_empty() && port.parse::<u16>().is_ok()),
        };
        if ok {
            return None;
        }
        Some(match self {
            Kind::Integer => "a whole number written in digits only",
            Kind::Boolean => "one of true, false, 1, 0, yes, no, on, off",
            Kind::Url => "a URL (scheme://host…)",
            Kind::SocketAddr => "host:port",
            Kind::String | Kind::Path | Kind::List => "text",
        })
    }

    /// An empty value of these kinds is a mistake, not "unset".
    fn refuses_empty(self) -> bool {
        matches!(
            self,
            Kind::Integer | Kind::Boolean | Kind::Url | Kind::SocketAddr
        )
    }
}

/// The one spelling of a boolean every variable accepts.
pub fn parse_bool(raw: &str) -> Option<bool> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

fn looks_like_url(raw: &str) -> bool {
    let Some((scheme, rest)) = raw.split_once("://") else {
        return false;
    };
    let mut chars = scheme.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphabetic())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        && !rest.is_empty()
        && !raw.contains(char::is_whitespace)
}

/// Which edition of the product reads the variable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Edition {
    Community,
    Enterprise,
    Both,
}

impl Edition {
    pub const fn as_str(self) -> &'static str {
        match self {
            Edition::Community => "community",
            Edition::Enterprise => "enterprise",
            Edition::Both => "both",
        }
    }
}

/// One environment variable: its single definition.
#[derive(Debug)]
pub struct EnvVar {
    pub name: &'static str,
    pub kind: Kind,
    /// The value used when the variable is not set, written as it would be
    /// in the environment. `None`: no default (the description says what
    /// happens when it is unset).
    pub default: Option<&'static str>,
    /// The service refuses to start without it.
    pub required: bool,
    /// Never printed. A secret has no default and its example is a
    /// `<placeholder>`.
    pub secret: bool,
    pub edition: Edition,
    pub description: &'static str,
    pub example: &'static str,
    /// Accepted values, in words.
    pub accepted: &'static str,
}

/// A service's variables.
#[derive(Debug)]
pub struct Registry {
    /// Short name, as the packages call the service (`skill-runner`).
    pub service: &'static str,
    /// Heading of the generated `.env.example`.
    pub title: &'static str,
    pub vars: &'static [&'static EnvVar],
}

/// One reason the environment is refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Problem {
    /// A required variable is not set (or is empty).
    Missing { name: &'static str },
    /// A variable is set to something that is not of its kind.
    Invalid {
        name: &'static str,
        expected: String,
    },
}

impl fmt::Display for Problem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Problem::Missing { name } => write!(f, "{name} is required and is not set"),
            Problem::Invalid { name, expected } => {
                write!(f, "{name} is not valid: it must be {expected}")
            }
        }
    }
}

/// Why the service does not start: every problem found, one per line. Holds
/// names only, never a value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnvError(pub Vec<Problem>);

impl EnvError {
    fn one(problem: Problem) -> Self {
        Self(vec![problem])
    }
}

impl fmt::Display for EnvError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the environment is refused:")?;
        for problem in &self.0 {
            write!(f, "\n  {problem}")?;
        }
        Ok(())
    }
}

impl std::error::Error for EnvError {}

/// The values of a registry's variables, taken once.
pub struct Env {
    registry: &'static Registry,
    values: BTreeMap<&'static str, String>,
}

impl fmt::Debug for Env {
    /// Names only.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Env")
            .field("service", &self.registry.service)
            .field("set", &self.values.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl Env {
    /// Read the process environment, refusing malformed values **and**
    /// missing required variables. What a binary calls at
    /// start.
    pub fn load(registry: &'static Registry) -> Result<Self, EnvError> {
        let (env, mut problems) = Self::build(registry, std::env::vars_os());
        problems.extend(env.missing());
        if problems.is_empty() {
            Ok(env)
        } else {
            Err(EnvError(problems))
        }
    }

    /// Read the process environment, refusing malformed values. A required variable that is missing is reported by the
    /// accessor that needs it, so a part of the service can read its own
    /// settings without the rest being configured.
    pub fn from_process(registry: &'static Registry) -> Result<Self, EnvError> {
        Self::checked(Self::build(registry, std::env::vars_os()))
    }

    /// The same rules as [`Env::from_process`] over the given pairs.
    pub fn from_pairs<K: AsRef<str>, V: AsRef<str>>(
        registry: &'static Registry,
        pairs: impl IntoIterator<Item = (K, V)>,
    ) -> Result<Self, EnvError> {
        Self::checked(Self::build(
            registry,
            pairs
                .into_iter()
                .map(|(k, v)| (OsString::from(k.as_ref()), OsString::from(v.as_ref()))),
        ))
    }

    fn checked((env, problems): (Self, Vec<Problem>)) -> Result<Self, EnvError> {
        if problems.is_empty() {
            Ok(env)
        } else {
            Err(EnvError(problems))
        }
    }

    fn build(
        registry: &'static Registry,
        pairs: impl Iterator<Item = (OsString, OsString)>,
    ) -> (Self, Vec<Problem>) {
        let mut values = BTreeMap::new();
        let mut problems = Vec::new();
        for (key, value) in pairs {
            let Some(key) = key.to_str() else { continue };
            let Some(var) = registry.find(key) else {
                continue;
            };
            let Some(value) = value.to_str() else {
                problems.push(Problem::Invalid {
                    name: var.name,
                    expected: "UTF-8 text".to_string(),
                });
                continue;
            };
            if value.is_empty() {
                if var.kind.refuses_empty() {
                    problems.push(Problem::Invalid {
                        name: var.name,
                        expected: format!(
                            "{} (it is set but empty)",
                            var.kind.mismatch("").unwrap_or("a value")
                        ),
                    });
                }
                continue;
            }
            if let Some(expected) = var.kind.mismatch(value) {
                problems.push(Problem::Invalid {
                    name: var.name,
                    expected: expected.to_string(),
                });
                continue;
            }
            values.insert(var.name, value.to_string());
        }
        problems.sort_by(|a, b| format!("{a}").cmp(&format!("{b}")));
        (Self { registry, values }, problems)
    }

    /// Required variables that are not set.
    fn missing(&self) -> Vec<Problem> {
        self.registry
            .vars
            .iter()
            .filter(|var| var.required && !self.values.contains_key(var.name))
            .map(|var| Problem::Missing { name: var.name })
            .collect()
    }

    /// The value, or the default. Panics on a variable that is not in this
    /// registry: nothing is read that is not registered.
    fn raw(&self, var: &EnvVar) -> Option<&str> {
        assert!(
            self.registry.find(var.name).is_some(),
            "{} is not registered for {}",
            var.name,
            self.registry.service
        );
        self.values
            .get(var.name)
            .map(String::as_str)
            .or(var.default)
    }

    /// Set in the environment (not merely defaulted).
    pub fn is_set(&self, var: &EnvVar) -> bool {
        self.raw(var).is_some() && self.values.contains_key(var.name)
    }

    pub fn string(&self, var: &EnvVar) -> Option<String> {
        self.raw(var).map(str::to_string)
    }

    /// For a required variable, or one with a default.
    pub fn required_string(&self, var: &'static EnvVar) -> Result<String, EnvError> {
        self.string(var)
            .ok_or_else(|| EnvError::one(Problem::Missing { name: var.name }))
    }

    /// The value parsed as `T`, `None` when unset with no default.
    pub fn parse<T: FromStr>(&self, var: &'static EnvVar) -> Result<Option<T>, EnvError> {
        let Some(raw) = self.raw(var) else {
            return Ok(None);
        };
        raw.trim().parse::<T>().map(Some).map_err(|_| {
            EnvError::one(Problem::Invalid {
                name: var.name,
                expected: format!("{} (in range)", var.accepted),
            })
        })
    }

    /// [`Env::parse`] for a required variable, or one with a default.
    pub fn number<T: FromStr>(&self, var: &'static EnvVar) -> Result<T, EnvError> {
        self.parse(var)?
            .ok_or_else(|| EnvError::one(Problem::Missing { name: var.name }))
    }

    /// A boolean; unset with no default is `false`.
    pub fn flag(&self, var: &EnvVar) -> bool {
        self.raw(var).and_then(parse_bool).unwrap_or(false)
    }

    /// Comma-separated items, trimmed, empty ones dropped.
    pub fn list(&self, var: &EnvVar) -> Vec<String> {
        self.raw(var)
            .map(|raw| {
                raw.split(',')
                    .map(|item| item.trim().to_string())
                    .filter(|item| !item.is_empty())
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn path(&self, var: &EnvVar) -> Option<PathBuf> {
        self.raw(var).map(PathBuf::from)
    }

    /// The directory for temporary files: `var` (the platform's `TMPDIR`)
    /// when set, otherwise the platform default.
    pub fn temp_dir(&self, var: &EnvVar) -> PathBuf {
        self.path(var).unwrap_or_else(std::env::temp_dir)
    }

    /// `(name, value)` of those of `vars` that are set, for handing to a
    /// child process unchanged.
    pub fn forwarded(&self, vars: &[&EnvVar]) -> Vec<(String, String)> {
        vars.iter()
            .filter(|var| self.is_set(var))
            .filter_map(|var| Some((var.name.to_string(), self.string(var)?)))
            .collect()
    }

    /// `(name, value)` of process variables an operator named in a setting
    /// (a list of names to hand to a child process). A name this registry
    /// knows is skipped: the service's own settings and secrets are handed
    /// on only where the service says so by [`Env::forwarded`].
    pub fn forwarded_by_name(&self, names: &[String]) -> Vec<(String, String)> {
        names
            .iter()
            .filter(|name| self.registry.find(name).is_none())
            .filter_map(|name| {
                let value = std::env::var_os(name)?.into_string().ok()?;
                Some((name.clone(), value))
            })
            .collect()
    }
}

/// True when the binary was started with [`PRINT_FLAG`].
pub fn print_requested() -> bool {
    std::env::args().skip(1).any(|arg| arg == PRINT_FLAG)
}

impl Registry {
    pub fn find(&self, name: &str) -> Option<&'static EnvVar> {
        self.vars.iter().copied().find(|var| var.name == name)
    }

    /// What is wrong with the registry itself. Empty for a sound one; a test
    /// in every service holds it empty.
    pub fn defects(&self) -> Vec<String> {
        let mut defects = Vec::new();
        let mut seen = BTreeMap::new();
        for var in self.vars {
            if seen.insert(var.name, ()).is_some() {
                defects.push(format!("{} is registered twice", var.name));
            }
            if var.description.trim().is_empty() || var.example.trim().is_empty() {
                defects.push(format!("{} has no description or no example", var.name));
            }
            if var.accepted.trim().is_empty() {
                defects.push(format!("{} does not say what it accepts", var.name));
            }
            if var.required && var.default.is_some() {
                defects.push(format!("{} is required and has a default", var.name));
            }
            if var.secret && var.default.is_some() {
                defects.push(format!("{} is a secret with a default", var.name));
            }
            if var.secret && !(var.example.contains('<') && var.example.contains('>')) {
                defects.push(format!(
                    "{} is a secret whose example is not a <placeholder>",
                    var.name
                ));
            }
            if let Some(default) = var.default
                && var.kind.mismatch(default).is_some()
            {
                defects.push(format!(
                    "the default of {} is not a {:?}",
                    var.name, var.kind
                ));
            }
            if !var.secret && var.kind.mismatch(var.example).is_some() {
                defects.push(format!(
                    "the example of {} is not a {:?}",
                    var.name, var.kind
                ));
            }
        }
        defects
    }

    /// The registry as JSON (what `--print-env` prints). Schema, version
    /// [`SCHEMA_VERSION`]:
    ///
    /// ```text
    /// {
    ///   "schema_version": 2,
    ///   "service": "<short service name>",
    ///   "variables": [{
    ///     "name": "…",
    ///     "type": "string|integer|boolean|url|socket_addr|path|list",
    ///     "default": "…" | null,
    ///     "required": true|false,
    ///     "secret": true|false,
    ///     "edition": "community|enterprise|both",
    ///     "description": "…",
    ///     "example": "…",
    ///     "accepted": "…"
    ///   }]
    /// }
    /// ```
    pub fn json(&self) -> serde_json::Value {
        let variables: Vec<serde_json::Value> = self
            .vars
            .iter()
            .map(|var| {
                serde_json::json!({
                    "name": var.name,
                    "type": var.kind.as_str(),
                    "default": var.default,
                    "required": var.required,
                    "secret": var.secret,
                    "edition": var.edition.as_str(),
                    "description": var.description,
                    "example": var.example,
                    "accepted": var.accepted,
                })
            })
            .collect();
        serde_json::json!({
            "schema_version": SCHEMA_VERSION,
            "service": self.service,
            "variables": variables,
        })
    }

    /// [`Registry::json`], pretty-printed, with a final newline.
    pub fn json_text(&self) -> String {
        let mut text = serde_json::to_string_pretty(&self.json()).unwrap_or_default();
        text.push('\n');
        text
    }

    /// The `.env.example` file. Required variables are written out (a secret
    /// with nothing after the `=`), the others commented out with their
    /// example.
    pub fn env_example(&self) -> String {
        let mut out = String::new();
        out.push_str(&format!("# {}: environment variables\n", self.title));
        out.push_str("#\n");
        out.push_str("# Generated from src/env_vars.rs; do not edit by hand. To regenerate:\n");
        out.push_str("#   UPDATE_ENV_DOCS=1 cargo test --test env_registry\n");
        out.push_str("#\n");
        out.push_str("# Copy to .env and fill in the required values: cp .env.example .env\n");
        out.push_str("# The service loads .env from its working directory at start.\n");
        for var in self.vars {
            out.push('\n');
            for line in wrap(var.description, 76) {
                out.push_str(&format!("# {line}\n"));
            }
            let default = match (var.required, var.default) {
                (true, _) => "required".to_string(),
                (false, Some(default)) => format!("default {default}"),
                (false, None) => "no default".to_string(),
            };
            let secret = if var.secret { "; secret" } else { "" };
            out.push_str(&format!(
                "# {}; {default}{secret}; edition: {}\n",
                var.kind.as_str(),
                var.edition.as_str()
            ));
            for line in wrap(&format!("Accepted: {}", var.accepted), 76) {
                out.push_str(&format!("# {line}\n"));
            }
            match (var.required, var.secret) {
                (true, true) => {
                    out.push_str(&format!("# Example: {}\n", var.example));
                    out.push_str(&format!("{}=\n", var.name));
                }
                (true, false) => out.push_str(&format!("{}={}\n", var.name, var.example)),
                (false, _) => out.push_str(&format!("# {}={}\n", var.name, var.example)),
            }
        }
        out
    }

    /// The README's configuration table.
    pub fn markdown(&self) -> String {
        // Prose cells: a `|` would end the cell, and `<…>` would be read as markup.
        let cell = |text: &str| {
            text.replace('|', "\\|")
                .replace('<', "&lt;")
                .replace('>', "&gt;")
        };
        let code = |text: &str| text.replace('|', "\\|");
        let mut out = String::new();
        out.push_str(
            "| Variable | Type | Default | Edition | Description | Accepted | Example |\n",
        );
        out.push_str("|---|---|---|---|---|---|---|\n");
        for var in self.vars {
            let default = match (var.required, var.default) {
                (true, _) => "**required**".to_string(),
                (false, Some(default)) => format!("`{default}`"),
                (false, None) => "none".to_string(),
            };
            let secret = if var.secret { " (secret)" } else { "" };
            out.push_str(&format!(
                "| `{}`{secret} | {} | {default} | {} | {} | {} | `{}` |\n",
                var.name,
                var.kind.as_str(),
                var.edition.as_str(),
                cell(var.description),
                cell(var.accepted),
                code(var.example),
            ));
        }
        out
    }
}

/// `text` broken at spaces into lines of at most `width` characters.
fn wrap(text: &str, width: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line = String::new();
    for word in text.split_whitespace() {
        if !line.is_empty() && line.len() + 1 + word.len() > width {
            lines.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line.push_str(word);
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}

// ── extension:begin ────────────────────────────────────────────────────────
// Added for the Core Gateway (`brutor-gateway-core`), which reports the
// `BRUTOR_*` variables that are set and that it does not read. Everything
// outside this block is the same in the gateway and in the three worker
// services; they take this block over as it is.

impl Registry {
    /// The names in the process environment that start with `prefix` and
    /// that this registry does not register, sorted. Names only.
    pub fn unregistered_in_process(&self, prefix: &str) -> Vec<String> {
        self.unregistered(prefix, std::env::vars_os().map(|(name, _)| name))
    }

    /// [`Registry::unregistered_in_process`] over the given names.
    pub fn unregistered(
        &self,
        prefix: &str,
        names: impl IntoIterator<Item = OsString>,
    ) -> Vec<String> {
        let mut out: Vec<String> = names
            .into_iter()
            .filter_map(|name| name.into_string().ok())
            .filter(|name| name.starts_with(prefix))
            .filter(|name| self.find(name).is_none())
            .collect();
        out.sort();
        out.dedup();
        out
    }
}

#[cfg(test)]
mod extension_tests {
    use super::*;

    static KNOWN: EnvVar = EnvVar {
        name: "BRUTOR_X_KNOWN",
        kind: Kind::String,
        default: None,
        required: false,
        secret: false,
        edition: Edition::Both,
        description: "Known.",
        example: "x",
        accepted: "any text",
    };
    static REGISTRY: Registry = Registry {
        service: "test",
        title: "Test",
        vars: &[&KNOWN],
    };

    #[test]
    fn only_names_with_the_prefix_that_the_registry_does_not_know_are_unregistered() {
        let names = [
            "BRUTOR_X_KNOWN",
            "BRUTOR_X_TYPO",
            "BRUTOR_X_TYPO",
            "BRUTOR_A_STRANGER",
            "PATH",
            "brutor_x_lowercase",
        ]
        .map(OsString::from);
        assert_eq!(
            REGISTRY.unregistered("BRUTOR_", names),
            vec!["BRUTOR_A_STRANGER".to_string(), "BRUTOR_X_TYPO".to_string()]
        );
    }
}

// ── extension:end ──────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    const SENTINEL: &str = "value-that-must-never-be-printed";

    static PORT: EnvVar = EnvVar {
        name: "BRUTOR_T_PORT",
        kind: Kind::Integer,
        default: Some("7"),
        required: false,
        secret: false,
        edition: Edition::Both,
        description: "A number.",
        example: "9",
        accepted: "1 to 65535",
    };
    static TOKEN: EnvVar = EnvVar {
        name: "BRUTOR_T_TOKEN",
        kind: Kind::String,
        default: None,
        required: true,
        secret: true,
        edition: Edition::Enterprise,
        description: "A secret.",
        example: "<a long random string>",
        accepted: "any text",
    };
    static SWITCH: EnvVar = EnvVar {
        name: "BRUTOR_T_SWITCH_ENABLED",
        kind: Kind::Boolean,
        default: Some("false"),
        required: false,
        secret: false,
        edition: Edition::Both,
        description: "A switch.",
        example: "true",
        accepted: "true or false",
    };
    static UPSTREAM: EnvVar = EnvVar {
        name: "BRUTOR_T_UPSTREAM_URL",
        kind: Kind::Url,
        default: None,
        required: false,
        secret: true,
        edition: Edition::Both,
        description: "A URL that may carry a password.",
        example: "https://<user>:<password>@host/",
        accepted: "a URL",
    };
    static UNREGISTERED: EnvVar = EnvVar {
        name: "BRUTOR_T_ELSEWHERE",
        kind: Kind::String,
        default: None,
        required: false,
        secret: false,
        edition: Edition::Both,
        description: "Not in the registry.",
        example: "x",
        accepted: "any text",
    };
    static REGISTRY: Registry = Registry {
        service: "test",
        title: "Test",
        vars: &[&PORT, &TOKEN, &SWITCH, &UPSTREAM],
    };

    fn env(pairs: &[(&str, &str)]) -> Result<Env, EnvError> {
        Env::from_pairs(&REGISTRY, pairs.iter().copied())
    }

    #[test]
    fn the_test_registry_is_sound() {
        assert_eq!(REGISTRY.defects(), Vec::<String>::new());
    }

    #[test]
    fn a_value_or_its_default_is_read_through_the_definition() {
        let e = env(&[("BRUTOR_T_TOKEN", "t"), ("UNRELATED", "x")]).unwrap();
        assert_eq!(e.number::<u16>(&PORT).unwrap(), 7);
        assert!(!e.is_set(&PORT));
        assert!(!e.flag(&SWITCH));
        assert_eq!(e.required_string(&TOKEN).unwrap(), "t");
        let e = env(&[("BRUTOR_T_PORT", "9"), ("BRUTOR_T_SWITCH_ENABLED", "ON")]).unwrap();
        assert_eq!(e.number::<u16>(&PORT).unwrap(), 9);
        assert!(e.is_set(&PORT));
        assert!(e.flag(&SWITCH));
    }

    #[test]
    #[should_panic(expected = "is not registered")]
    fn a_variable_outside_the_registry_cannot_be_read() {
        let _ = env(&[]).unwrap().string(&UNREGISTERED);
    }

    #[test]
    fn a_name_the_registry_does_not_know_is_not_read_and_not_refused() {
        // Unknown names are the binary's to report (by name); the registry
        // neither reads nor refuses them, and a name a variable had before
        // is just such a name.
        let e = env(&[("T_PORT", SENTINEL), ("BRUTOR_T_UNKNOWN", SENTINEL)]).unwrap();
        assert_eq!(e.number::<u16>(&PORT).unwrap(), 7);
        assert!(!format!("{e:?}").contains("T_UNKNOWN"));
    }

    #[test]
    fn a_malformed_value_is_refused_without_being_printed() {
        for (name, bad) in [
            ("BRUTOR_T_PORT", "many"),
            ("BRUTOR_T_PORT", "+5"),
            ("BRUTOR_T_PORT", " 5"),
            ("BRUTOR_T_PORT", ""),
            ("BRUTOR_T_SWITCH_ENABLED", "enabled"),
            ("BRUTOR_T_SWITCH_ENABLED", ""),
            ("BRUTOR_T_UPSTREAM_URL", SENTINEL),
        ] {
            let err = env(&[(name, bad)]).unwrap_err().to_string();
            assert!(err.contains(name), "{bad:?}: {err}");
            assert!(bad.is_empty() || !err.contains(bad), "{bad:?}: {err}");
        }
        // In range for the kind, out of range for the type asked for.
        let e = env(&[("BRUTOR_T_PORT", "70000")]).unwrap();
        let err = e.number::<u16>(&PORT).unwrap_err().to_string();
        assert!(
            err.contains("BRUTOR_T_PORT") && !err.contains("70000"),
            "{err}"
        );
    }

    #[test]
    fn booleans_have_one_spelling() {
        for yes in ["true", "TRUE", "1", "yes", "on", " On "] {
            assert_eq!(parse_bool(yes), Some(true), "{yes:?}");
        }
        for no in ["false", "0", "no", "off", "OFF"] {
            assert_eq!(parse_bool(no), Some(false), "{no:?}");
        }
        for neither in ["", "2", "enabled", "y", "t"] {
            assert_eq!(parse_bool(neither), None, "{neither:?}");
        }
    }

    #[test]
    fn a_missing_required_variable_is_named() {
        // An empty secret is not a secret.
        let e = env(&[("BRUTOR_T_TOKEN", "")]).unwrap();
        assert_eq!(
            e.missing(),
            vec![Problem::Missing {
                name: "BRUTOR_T_TOKEN"
            }]
        );
        let err = e.required_string(&TOKEN).unwrap_err().to_string();
        assert!(err.contains("BRUTOR_T_TOKEN is required"), "{err}");
    }

    #[test]
    fn nothing_the_registry_writes_holds_a_value() {
        let e = env(&[
            ("BRUTOR_T_TOKEN", SENTINEL),
            (
                "BRUTOR_T_UPSTREAM_URL",
                "https://u:value-that-must-never-be-printed@h/",
            ),
        ])
        .unwrap();
        for text in [
            format!("{e:?}"),
            REGISTRY.json_text(),
            REGISTRY.env_example(),
            REGISTRY.markdown(),
        ] {
            assert!(!text.contains(SENTINEL), "{text}");
        }
        assert!(format!("{e:?}").contains("BRUTOR_T_TOKEN"));
    }

    #[test]
    fn the_json_has_the_documented_shape() {
        let json = REGISTRY.json();
        assert_eq!(json["schema_version"], SCHEMA_VERSION);
        assert_eq!(json["service"], "test");
        assert_eq!(
            json["variables"][0],
            serde_json::json!({
                "name": "BRUTOR_T_PORT",
                "type": "integer",
                "default": "7",
                "required": false,
                "secret": false,
                "edition": "both",
                "description": "A number.",
                "example": "9",
                "accepted": "1 to 65535",
            })
        );
        assert_eq!(json["variables"][1]["default"], serde_json::Value::Null);
        assert!(json.get("removed").is_none());
    }

    #[test]
    fn the_template_writes_out_what_is_required_and_comments_out_the_rest() {
        let template = REGISTRY.env_example();
        assert!(template.contains("\nBRUTOR_T_TOKEN=\n"), "{template}");
        assert!(template.contains("\n# BRUTOR_T_PORT=9\n"), "{template}");
        assert!(template.contains("# integer; default 7; edition: both"));
        assert!(template.contains("# string; required; secret; edition: enterprise"));
        let table = REGISTRY.markdown();
        assert!(
            table.contains("| `BRUTOR_T_TOKEN` (secret) | string | **required** | enterprise |")
        );
        assert!(!table.contains("Retired"));
    }

    #[test]
    fn a_defective_registry_is_reported() {
        static BAD: EnvVar = EnvVar {
            name: "BRUTOR_T_PORT",
            kind: Kind::Integer,
            default: Some("seven"),
            required: true,
            secret: true,
            edition: Edition::Both,
            description: "",
            example: "plain",
            accepted: "",
        };
        static REG: Registry = Registry {
            service: "bad",
            title: "Bad",
            vars: &[&BAD, &BAD],
        };
        let defects = REG.defects().join("\n");
        for expected in [
            "registered twice",
            "no description",
            "does not say what it accepts",
            "required and has a default",
            "secret with a default",
            "not a <placeholder>",
            "default of BRUTOR_T_PORT is not",
        ] {
            assert!(defects.contains(expected), "{expected}: {defects}");
        }
    }

    #[test]
    fn only_set_variables_are_forwarded_and_registered_names_are_not_forwarded_by_name() {
        let e = env(&[("BRUTOR_T_PORT", "9"), ("BRUTOR_T_TOKEN", SENTINEL)]).unwrap();
        assert_eq!(
            e.forwarded(&[&PORT, &SWITCH]),
            vec![("BRUTOR_T_PORT".to_string(), "9".to_string())]
        );
        let named = e.forwarded_by_name(&[
            "BRUTOR_T_TOKEN".to_string(),
            "BRUTOR_T_NOT_SET_ANYWHERE".to_string(),
        ]);
        assert!(named.is_empty(), "{named:?}");
    }
}
