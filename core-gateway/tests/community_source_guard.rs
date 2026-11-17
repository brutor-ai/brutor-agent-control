// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the Community crate names no Enterprise module and no
//! Enterprise-only table.
//!
//! Every Enterprise feature is an extension and lives in the Enterprise
//! workspace (`enterprise/`), which depends on this crate; this crate cannot
//! depend on it, so the compiler already refuses a `use` of an Enterprise
//! module. This scan holds what the compiler does not: a module coming back
//! under `src/` (a declaration, a copy), a type or helper of an Enterprise
//! feature re-created here, and SQL over a table only the Enterprise Edition
//! writes. It is strict: every file under `src/`, production code and unit
//! tests alike, with no file allowed to declare or re-export such a module.
//!
//! The one allow-listed file is `src/services/edition_guard.rs`, for the
//! table rule only: it is the Enterprise-data check, which must name the tables to count
//! their rows.
//!
//! Proven against the shapes it forbids
//! ([`the_scan_catches_the_shapes_it_forbids`]).

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

use brutor_gateway_core::services::edition_guard::ENTERPRISE_ONLY_TABLES;

/// The one Community file that names Enterprise-only tables, and why.
const TABLE_ALLOW_LIST: &[(&str, &str)] = &[(
    "src/services/edition_guard.rs",
    "the Enterprise-data check: counts the rows of each Enterprise-only table before a Community \
     gateway starts on a database",
)];

/// Ways of naming one of those modules, or a type only they define.
const NAMES: &[&str] = &[
    "semantic_cache::",
    "semantic_response_cache",
    "dispatch_exact_cache",
    "prompt_cache::",
    "anthropic_cache::",
    "openai_cache::",
    "gemini_cache::",
    "knowledge_base::",
    "bm25::",
    "qdrant_auth",
    "routes::batch",
    "services::batch",
    "services::portal",
    "portal_session",
    "system_admin_session::",
    "portal_common",
    "routes::portal_",
    "routes::public_tenant_theme",
    "services::license",
    "brutor_license",
    "SemanticCacheService",
    "SemanticResponseCache",
    "PromptCacheService",
    "ProviderPromptCache",
    "KnowledgeBaseService",
    "KnowledgeBaseEnricher",
    "PortalSessionVerifier",
    "CacheGroupConfig",
    "KbGroupConfig",
    "DispatchStore",
    "RouterCacheGate",
    // The Enterprise crate itself.
    "brutor_gateway_enterprise",
];

/// The composition root of the Enterprise distribution.
const COMPOSITION_ROOT: &str = "enterprise/src/main.rs";

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

/// `line number: code` for every line of `text` that names an Enterprise
/// module: production code and unit tests, declarations and re-exports
/// included. Only prose (a comment line) may name one.
fn enterprise_names(text: &str) -> Vec<String> {
    text.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .filter(|(_, l)| NAMES.iter().any(|n| l.contains(n)))
        .map(|(i, l)| format!("{}: {}", i + 1, l.trim()))
        .collect()
}

/// Is `table` named in `line` as a whole identifier?
fn names_table(line: &str, table: &str) -> bool {
    let is_word = |c: char| c.is_ascii_alphanumeric() || c == '_';
    let mut from = 0;
    while let Some(i) = line[from..].find(table) {
        let at = from + i;
        let before = line[..at].chars().next_back();
        let after = line[at + table.len()..].chars().next();
        if !before.is_some_and(is_word) && !after.is_some_and(is_word) {
            return true;
        }
        from = at + table.len();
    }
    false
}

/// `line number: table: code` for every line of `text` that names an
/// Enterprise-only table (`edition_guard::ENTERPRISE_ONLY_TABLES`).
fn enterprise_tables(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim_start().starts_with("//") {
            continue;
        }
        for (table, _) in ENTERPRISE_ONLY_TABLES {
            if names_table(line, table) {
                out.push(format!("{}: {table}: {}", i + 1, line.trim()));
            }
        }
    }
    out
}

fn community_files() -> Vec<(String, String)> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    assert!(files.len() > 100, "the scan found {} files", files.len());
    files
        .into_iter()
        .map(|file| {
            (
                file.to_string_lossy().replace('\\', "/"),
                fs::read_to_string(&file).unwrap(),
            )
        })
        .collect()
}

#[test]
fn community_code_names_no_enterprise_module() {
    let mut offenders = Vec::new();
    for (rel, text) in community_files() {
        for found in enterprise_names(&text) {
            offenders.push(format!("{rel}:{found}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "Community code names an Enterprise module. A feature reaches the gateway as an \
         extension (`builder::Extension`) of the Enterprise workspace, through the hooks and \
         registries of the state:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn community_code_names_no_enterprise_only_table() {
    assert_eq!(
        ENTERPRISE_ONLY_TABLES.len(),
        37,
        "the Enterprise-only tables"
    );
    let mut offenders = Vec::new();
    let mut allowed = 0;
    for (rel, text) in community_files() {
        let found = enterprise_tables(&text);
        if TABLE_ALLOW_LIST.iter().any(|(file, _)| *file == rel) {
            allowed += found.len();
            continue;
        }
        for line in found {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "Community code names a table only the Enterprise Edition writes. An extension owns \
         its tables and loads them through the hooks (a group loader, an invalidation \
         handler); the Enterprise-data check (`services/edition_guard.rs`) is the only Community reader:\n{}",
        offenders.join("\n")
    );
    assert!(
        allowed >= ENTERPRISE_ONLY_TABLES.len(),
        "the allow-listed file names {allowed} tables: the list is stale"
    );
}

/// The lists are true: the names are found in the Enterprise workspace where
/// this repository carries it, no Community `mod.rs` declares an Enterprise
/// module, and the composition root composes the extensions and names
/// nothing else.
#[test]
fn the_lists_are_true() {
    for (file, _) in TABLE_ALLOW_LIST {
        assert!(Path::new(file).exists(), "{file} is allow-listed and gone");
    }
    // The four files that could declare Enterprise modules
    // declare none.
    for file in [
        "src/routes/mod.rs",
        "src/services/mod.rs",
        "src/services/cache/mod.rs",
        "src/services/llm_adapter/mod.rs",
    ] {
        let text = fs::read_to_string(file).unwrap();
        for module in [
            "portal",
            "portal_session",
            "portal_common",
            "portal_auth",
            "portal_kb",
            "public_tenant_theme",
            "knowledge_base",
            "bm25",
            "qdrant_auth",
            "prompt_cache",
            "semantic_cache",
            "semantic_response_cache",
            "dispatch_exact_cache",
            "anthropic_cache",
            "openai_cache",
            "gemini_cache",
            "batch",
            "license",
            "licence",
        ] {
            assert!(
                !text
                    .lines()
                    .any(|l| l.trim() == format!("pub mod {module};")
                        || l.trim() == format!("mod {module};")),
                "{file} declares the Enterprise module `{module}`"
            );
        }
    }
    let Some(enterprise) = roots::tree("enterprise") else {
        // A Community tree alone (`tests/support/roots.rs`): nothing more to
        // compare with.
        return;
    };
    let mut listed = Vec::new();
    rust_files(&enterprise.join("src"), &mut listed);
    let mut seen = 0;
    for file in &listed {
        let text = fs::read_to_string(file).unwrap();
        seen += NAMES.iter().filter(|n| text.contains(*n)).count();
    }
    assert!(seen >= 40, "the names are found {seen} times: stale");
    // Each name is one something of the Enterprise workspace bears.
    let all: String = listed
        .iter()
        .map(|f| fs::read_to_string(f).unwrap())
        .collect();
    for name in NAMES {
        let bare = name.trim_end_matches("::");
        let bare = bare.rsplit("::").next().unwrap_or(bare);
        assert!(
            all.contains(bare)
                || *name == "brutor_gateway_enterprise"
                || *name == "services::license",
            "`{name}` names nothing of the Enterprise workspace: stale"
        );
    }
    // The composition root composes these extensions, in this order (found
    // by a planted bug: an extension dropped from `main` was noticed by the
    // editions checker only). The knowledge base after the Portal.
    let main = fs::read_to_string(COMPOSITION_ROOT).unwrap();
    let composed: Vec<&str> = main
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with(".extension("))
        .collect();
    assert_eq!(
        composed,
        [
            ".extension(LicenceGate::from_env())",
            ".extension(BatchJobs::new())",
            ".extension(Portal::new())",
            ".extension(PromptCaching::new())",
            ".extension(ResponseCaching::new())",
            ".extension(KnowledgeBase::new())",
            ".extension(SystemAdministrators::new())",
            // What lets the distribution run as several instances.
            ".extension(Clustering::new())",
        ],
        "the extensions this distribution composes"
    );
    // The composition root composes extensions and names nothing else: each
    // of its lines that names a feature is the import of its extension or
    // the composition of it.
    for line in main
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .filter(|l| NAMES.iter().any(|n| l.contains(n)))
    {
        let line = line.trim();
        assert!(
            line.starts_with("use brutor_gateway_enterprise::") || line.starts_with(".extension("),
            "{COMPOSITION_ROOT}: {line}"
        );
    }
    // The Community binary composes none.
    let community_main = fs::read_to_string("src/main.rs").unwrap();
    assert!(
        !community_main
            .lines()
            .any(|l| l.trim_start().starts_with(".extension(")),
        "src/main.rs composes an extension: the Community gateway has none"
    );
}

/// The registries the built-in gateway fills hold nothing of an Enterprise
/// feature: no built-in task and no built-in invalidation handler is tagged.
#[test]
fn nothing_built_in_is_tagged_with_an_enterprise_feature() {
    for file in ["src/tasks.rs", "src/services/cache/invalidation.rs"] {
        let text = fs::read_to_string(file).unwrap();
        let tagged: Vec<&str> = text
            .lines()
            .filter(|l| !l.trim_start().starts_with("//"))
            .filter(|l| l.contains("feature: Some("))
            .collect();
        assert!(tagged.is_empty(), "{file}: {tagged:?}");
        assert!(
            text.matches("feature: None,").count() >= 10,
            "{file}: the scan reads nothing"
        );
    }
}

#[test]
fn the_scan_catches_the_shapes_it_forbids() {
    // What the builder, AppState, the invalidator, app.rs and the group
    // cache must not hold.
    for shape in [
        "        services::portal_session::PortalSessionVerifier::install(&mut app_state)?;",
        "    pub semantic_cache: Option<std::sync::Arc<SemanticCacheService>>,",
        "    pub prompt_cache: std::sync::Arc<prompt_cache::PromptCacheService>,",
        "    pub knowledge_base: Option<std::sync::Arc<knowledge_base::KnowledgeBaseService>>,",
        "        .nest(\"/v1/portal/auth\", routes::portal_auth::router())",
        "    pub kb_config: Option<crate::services::knowledge_base::KbGroupConfig>,",
        "pub use crate::services::cache::semantic_cache::CacheResult as CacheHit;",
        "use crate::routes::portal_common;",
        "    response_cache_gates: DashMap<String, RouterCacheGate>,",
        "            crate::services::prompt_cache::clear_all_sticky_pins_for_group(",
        // A declaration or a re-export of an Enterprise module, which
        // the four `mod.rs` files were allowed until the modules left.
        "pub mod portal_session;",
        "pub use semantic_cache::SemanticCacheService;",
        // A unit test that reaches an Enterprise service.
        "        crate::services::prompt_cache::ProviderPromptCache::install(&mut state)",
        // The Enterprise crate itself.
        "use brutor_gateway_enterprise::portal::Portal;",
    ] {
        assert_eq!(enterprise_names(shape).len(), 1, "not caught: {shape}");
    }
    // Unit tests are held to the rule too: nothing after `#[cfg(test)]` is
    // exempt.
    assert_eq!(
        enterprise_names(
            "fn real() {}\n#[cfg(test)]\nmod tests {\n    use crate::services::prompt_cache::PromptCacheService;\n}\n"
        )
        .len(),
        1
    );
    // Prose, and the Community names that only look alike.
    for fine in [
        "    // services::portal_session in a comment",
        "    pub response_cache: std::sync::Arc<dyn response_cache::ResponseCache>,",
        "    pub prompt_cache_policy: std::sync::Arc<dyn prompt_caching::PromptCachePolicy>,",
        "        auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)?;",
        "    let usage = prompt_cache_usage::read(&body);",
    ] {
        assert!(enterprise_names(fine).is_empty(), "{fine}");
    }

    // Tables: SQL, a constant, a format string; a whole identifier only.
    for shape in [
        "            \"SELECT enabled FROM semantic_cache_configs WHERE tenant_id = $1\",",
        "    sqlx::query(\"DELETE FROM kb_documents WHERE id = $1\")",
        "const TABLE: &str = \"system_admin_sessions\";",
        "        let sql = format!(\"SELECT 1 FROM {} JOIN batch_jobs b ON true\", t);",
    ] {
        assert_eq!(enterprise_tables(shape).len(), 1, "not caught: {shape}");
    }
    for fine in [
        "    // semantic_cache_configs in a comment",
        "    let my_semantic_cache_configs_copy = 1;",
        "    \"SELECT 1 FROM resource_groups\"",
        "    let kb_documents_seen = 0;",
    ] {
        assert!(enterprise_tables(fine).is_empty(), "{fine}");
    }
}

// ---------------------------------------------------------------------------
// The trial is the Enterprise Edition's
// ---------------------------------------------------------------------------

/// What Community code must not carry: the trial columns it used to read
/// (`tenants.trial_ends_at`; `is_trial` anywhere but in an `INSERT` that has
/// to give the NOT NULL column a value), the wire error of an expired trial,
/// and the sales contact its message named.
fn trial_mentions(text: &str) -> Vec<String> {
    let mut hits = Vec::new();
    for (n, line) in text.lines().enumerate() {
        let code = line.trim_start();
        if code.starts_with("//") {
            continue;
        }
        // A column list of an INSERT (a test seeding a tenant row) names the
        // column without reading it.
        let seeds_the_column = code.contains("is_trial, suspended")
            || code.contains("is_default, is_trial")
            || code.contains("is_trial BOOLEAN");
        let reads_trial = code.contains("trial_ends_at")
            || code.contains("trial_expired")
            || code.contains("sales@")
            || (code.contains("is_trial") && !seeds_the_column);
        if reads_trial {
            hits.push(format!("{}: {}", n + 1, code));
        }
    }
    hits
}

/// A Community gateway ignores the trial columns entirely: it selects
/// neither, blocks no tenant for a trial, and has no "contact sales"
/// message. The Enterprise Edition's trial gate is in
/// `enterprise/src/trial.rs`, behind `services::tenant_gate`.
#[test]
fn community_code_reads_no_trial_column_and_has_no_trial_message() {
    let mut bad = Vec::new();
    for (name, text) in community_files() {
        for hit in trial_mentions(&text) {
            bad.push(format!("{name}:{hit}"));
        }
    }
    assert!(
        bad.is_empty(),
        "Community code names the trial (the Enterprise trial gate owns it):\n{}",
        bad.join("\n")
    );
    // The shapes that were there.
    for forbidden in [
        r#""SELECT suspended, is_trial, suspension_reason, trial_ends_at FROM tenants WHERE id = $1""#,
        "    pub trial_ends_at: Option<DateTime<Utc>>,",
        "        if self.is_trial {",
        r#"                    "error": "trial_expired","#,
        // The message that was there, its address left out (no address at
        // the company's domain is in the Community tree).
        r#"    "Your trial has expired. Contact sales@ to upgrade.".to_string()"#,
    ] {
        assert_eq!(trial_mentions(forbidden).len(), 1, "{forbidden}");
    }
    // What is allowed: a comment, and an INSERT's column list.
    assert!(trial_mentions("    // the trial bundle's is_trial flag").is_empty());
    assert!(
        trial_mentions(r#"  "is_default, is_trial, suspended, features, settings) \"#).is_empty()
    );
    // Where the Enterprise workspace is present, the gate is there.
    if roots::tree("enterprise").is_some() {
        let gate = fs::read_to_string("enterprise/src/trial.rs").expect("the trial gate");
        assert!(gate.contains("impl TenantGate for TrialGate"));
        assert!(gate.contains("pub const TRIAL_EXPIRED_MESSAGE"));
    }
}
