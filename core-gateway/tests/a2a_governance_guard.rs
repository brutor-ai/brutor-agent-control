// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: every A2A route is judged by the A2A policy and by the caller's
//! own plane (an agent's grants, a system's autonomy), by the one evaluation
//! there is of each.
//!
//! The bug class: the policy a resource group or an agent card states for
//! A2A (`default_access`, `deny_classifications`, `allowed_globs`) was
//! evaluated by a function private to one route file. The routes of the
//! other protocol version never called it, so the same call was refused on
//! one route and passed on another; and the copy that did run was handed a
//! placeholder where a route had no value for one of its inputs.
//!
//! The same happened to the caller's own plane: the decision from an agent's
//! `a2a_call` grants and the autonomy gate of a caller that is not an agent
//! were written out in two routes and missing from the third, which
//! dispatched work for an agent with no grant and for a suspended system.
//!
//! The evaluations are `services::a2a_governance::enforce` (the policy) and
//! `services::a2a_governance::authorize_actor` (the caller's plane). This
//! guard pins:
//!
//! 1. every handler an A2A router registers is in `ROUTES`, with how it
//!    reaches EACH of the two (it calls it, it goes through the v1.0 prelude
//!    that calls it, it hands over to listed handlers) or why it is exempt.
//!    A new route fails until it is listed, and a listed route that stops
//!    reaching an evaluation fails; where a function calls both, the
//!    caller's plane comes first (the policy is told what it approved);
//! 2. no A2A route file decides from an agent's grants or from autonomy
//!    itself: a second copy is one that the next route will not have;
//! 3. the A2A routers are the ones named here: a new A2A route file, or an
//!    A2A path mounted from anywhere else, fails until it is listed;
//! 4. the policy documents are read in one place: the group chain's
//!    documents (`a2a_governance_levels`) only by the evaluation, and a
//!    card's own policy only to hand it to the evaluation.
//!
//! Behaviour is pinned by `tests/integration/a2a_governance.rs`.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

const ENFORCE: &str = "a2a_governance::enforce(";
const AUTHORIZE: &str = "a2a_governance::authorize_actor(";

/// One of the two evaluations a route must reach.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Evaluation {
    /// Its call, as a route writes it.
    call: &'static str,
    /// What a problem calls it.
    name: &'static str,
}

/// The A2A policy of the caller's group chain and of the card.
const POLICY: Evaluation = Evaluation {
    call: ENFORCE,
    name: "the evaluation",
};
/// The caller's own plane: the agent-grant decision (`a2a_call`) and the
/// autonomy gate.
const ACTOR: Evaluation = Evaluation {
    call: AUTHORIZE,
    name: "the caller's grant and autonomy gate",
};

/// What a route file must not call itself: the two gates
/// `a2a_governance::authorize_actor` is made of.
const ACTOR_GATES: [&str; 4] = [
    "decide_for_agent(",
    "decide_for_agent_with(",
    "enforce_for_group(",
    "enforce_for_groups(",
];
const PRELUDE: &str = "resolve_request";
const LEGACY: &str = "src/routes/a2a.rs";
const V1: &str = "src/routes/a2a_v1.rs";

/// How a route handler reaches the evaluation.
#[derive(Debug, Clone, Copy, PartialEq)]
enum Reach {
    /// The handler calls `enforce` itself.
    Direct,
    /// The handler goes through the v1.0 prelude (`resolve_request`), which
    /// calls `enforce` for every verb.
    Prelude,
    /// The handler only picks among these listed handlers.
    HandsOver(&'static [&'static str]),
    /// Not judged, and why.
    Exempt(&'static str),
}

/// `(file, the function that builds the router, handler, how it reaches the
/// A2A policy, how it reaches the caller's grant and autonomy gate)`.
const ROUTES: [(&str, &str, &str, Reach, Reach); 14] = [
    (LEGACY, "router", "outbound", Reach::Direct, Reach::Direct),
    (LEGACY, "router", "inbound", Reach::Direct, Reach::Direct),
    (
        LEGACY,
        "discovery_router",
        "discover_card",
        Reach::Exempt(
            "public discovery of a card document: no caller is authenticated, so there is no \
             group whose policy could judge it, and nothing is invoked",
        ),
        Reach::Exempt(
            "public discovery of a card document: no caller is authenticated, so there is no \
             agent whose grants and no system whose autonomy could judge it",
        ),
    ),
    (
        LEGACY,
        "discovery_router",
        "discover_jwks",
        Reach::Exempt(
            "the tenant's public signing keys: no caller is authenticated and no agent is \
             addressed",
        ),
        Reach::Exempt(
            "the tenant's public signing keys: no caller is authenticated and no agent is \
             addressed",
        ),
    ),
    (V1, "router", "send_message", Reach::Prelude, Reach::Prelude),
    (
        V1,
        "router",
        "send_streaming_message",
        Reach::Prelude,
        Reach::Prelude,
    ),
    (V1, "router", "list_tasks", Reach::Prelude, Reach::Prelude),
    (
        V1,
        "router",
        "tasks_get_dispatch",
        Reach::HandsOver(&["get_task", "subscribe_to_task"]),
        Reach::HandsOver(&["get_task", "subscribe_to_task"]),
    ),
    (
        V1,
        "router",
        "tasks_post_dispatch",
        Reach::HandsOver(&["cancel_task"]),
        Reach::HandsOver(&["cancel_task"]),
    ),
    (
        V1,
        "router",
        "get_extended_agent_card",
        Reach::Prelude,
        Reach::Prelude,
    ),
    (
        V1,
        "router",
        "create_push_config",
        Reach::Prelude,
        Reach::Prelude,
    ),
    (
        V1,
        "router",
        "list_push_configs",
        Reach::Prelude,
        Reach::Prelude,
    ),
    (
        V1,
        "router",
        "get_push_config",
        Reach::Prelude,
        Reach::Prelude,
    ),
    (
        V1,
        "router",
        "delete_push_config",
        Reach::Prelude,
        Reach::Prelude,
    ),
];

/// Handlers a dispatcher hands over to and no router names: listed so that
/// what a dispatcher hands over to is itself checked.
const HANDED_OVER: [(&str, &str, Reach, Reach); 3] = [
    (V1, "get_task", Reach::Prelude, Reach::Prelude),
    (V1, "subscribe_to_task", Reach::Prelude, Reach::Prelude),
    (V1, "cancel_task", Reach::Prelude, Reach::Prelude),
];

/// Production files that may mount or define an A2A route.
const A2A_ROUTE_FILES: [&str; 3] = [LEGACY, V1, "src/app.rs"];

/// Production files that may read the group chain's policy documents.
const LEVEL_READERS: [&str; 2] = [
    "src/services/cache/group_cache.rs",
    "src/services/a2a_governance.rs",
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// Production code only: everything before the file's test module, without
/// comment lines.
fn production(src: &str) -> String {
    let mut out = String::new();
    let lines: Vec<&str> = src.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        if line.starts_with("#[cfg(test)]")
            && lines
                .get(i + 1)
                .is_some_and(|next| next.trim_start().starts_with("mod "))
        {
            break;
        }
        if line.trim_start().starts_with("//") {
            continue;
        }
        out.push_str(line);
        out.push('\n');
    }
    out
}

fn is_ident(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The body of `fn name(..) {..}` in `code`: the text between the braces.
fn fn_body<'a>(code: &'a str, name: &str) -> Option<&'a str> {
    let needle = format!("fn {name}(");
    let mut from = 0;
    let at = loop {
        let i = from + code[from..].find(&needle)?;
        // `fn name(` itself, not `fn other_name(`.
        if !code[..i].ends_with(is_ident) {
            break i;
        }
        from = i + needle.len();
    };
    // The parameter list, then the body's opening brace.
    let mut depth = 0usize;
    let mut open = None;
    for (i, c) in code[at..].char_indices() {
        match c {
            '(' => depth += 1,
            ')' => depth = depth.saturating_sub(1),
            '{' if depth == 0 => {
                open = Some(at + i);
                break;
            }
            _ => {}
        }
    }
    let open = open?;
    let mut depth = 0usize;
    for (i, c) in code[open..].char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&code[open + 1..open + i]);
                }
            }
            _ => {}
        }
    }
    None
}

/// Whether `body` calls the function `name`.
fn calls(body: &str, name: &str) -> bool {
    let needle = format!("{name}(");
    let mut from = 0;
    while let Some(i) = body[from..].find(&needle) {
        let at = from + i;
        if !body[..at].ends_with(is_ident) {
            return true;
        }
        from = at + needle.len();
    }
    false
}

/// The handlers a router-building function registers: the name inside each
/// `get(..)`, `post(..)`, `put(..)`, `patch(..)` and `delete(..)`.
fn registered(router_body: &str) -> Vec<String> {
    let mut out = Vec::new();
    for method in ["get", "post", "put", "patch", "delete"] {
        let needle = format!("{method}(");
        let mut from = 0;
        while let Some(i) = router_body[from..].find(&needle) {
            let at = from + i;
            from = at + needle.len();
            if router_body[..at].ends_with(is_ident) {
                continue;
            }
            let name: String = router_body[from..]
                .chars()
                .take_while(|c| is_ident(*c))
                .collect();
            if !name.is_empty() && !out.contains(&name) {
                out.push(name);
            }
        }
    }
    out
}

/// The functions of `code` that build a router (`fn x() -> Router<..>`).
fn router_builders(code: &str) -> Vec<String> {
    let mut out = Vec::new();
    for line in code.lines() {
        let line = line.trim_start();
        if line.contains("-> Router<")
            && let Some(rest) = line.split("fn ").nth(1)
        {
            let name: String = rest.chars().take_while(|c| is_ident(*c)).collect();
            if !name.is_empty() {
                out.push(name);
            }
        }
    }
    out
}

/// How the listed `handler` reaches the policy and the caller's plane.
fn listed(file: &str, handler: &str) -> Option<(Reach, Reach)> {
    ROUTES
        .iter()
        .find(|(f, _, h, _, _)| *f == file && *h == handler)
        .map(|(_, _, _, policy, actor)| (*policy, *actor))
        .or_else(|| {
            HANDED_OVER
                .iter()
                .find(|(f, h, _, _)| *f == file && *h == handler)
                .map(|(_, _, policy, actor)| (*policy, *actor))
        })
}

fn reach_of(file: &str, handler: &str) -> Option<Reach> {
    listed(file, handler).map(|(policy, _)| policy)
}

fn actor_reach_of(file: &str, handler: &str) -> Option<Reach> {
    listed(file, handler).map(|(_, actor)| actor)
}

/// What is wrong with the A2A routes of one route file, given what the
/// list says about how they reach the A2A policy.
fn problems_in(file: &str, code: &str, reach: &dyn Fn(&str, &str) -> Option<Reach>) -> Vec<String> {
    problems_with(file, code, POLICY, reach)
}

/// Where `code` calls the caller's plane after the policy in one function:
/// the policy must be told what the caller's plane approved.
fn out_of_order(file: &str, code: &str) -> Vec<String> {
    let mut problems = Vec::new();
    let mut rest = code;
    while let Some(at) = rest.find("fn ") {
        rest = &rest[at + 3..];
        let name: String = rest.chars().take_while(|c| is_ident(*c)).collect();
        if name.is_empty() {
            continue;
        }
        let Some(body) = fn_body(code, &name) else {
            continue;
        };
        if let (Some(policy), Some(actor)) = (body.find(ENFORCE), body.find(AUTHORIZE))
            && actor > policy
        {
            let problem = format!(
                "{file}: `{name}` judges the A2A policy before the caller's grant and autonomy \
                 gate"
            );
            if !problems.contains(&problem) {
                problems.push(problem);
            }
        }
    }
    problems
}

/// Where a route file decides from an agent's grants or from autonomy
/// itself.
fn own_actor_gates(file: &str, code: &str) -> Vec<String> {
    let mut problems = Vec::new();
    for gate in ACTOR_GATES {
        let name = gate.trim_end_matches('(');
        if calls(code, name) {
            problems.push(format!(
                "{file}: calls `{name}` itself; an A2A route reaches the caller's grants and \
                 autonomy through `a2a_governance::authorize_actor`"
            ));
        }
    }
    problems
}

/// What is wrong with the A2A routes of one route file, given what the
/// list says about how they reach `evaluation`.
fn problems_with(
    file: &str,
    code: &str,
    evaluation: Evaluation,
    reach: &dyn Fn(&str, &str) -> Option<Reach>,
) -> Vec<String> {
    let mut problems = Vec::new();
    let mut handlers: Vec<String> = Vec::new();
    for builder in router_builders(code) {
        let Some(body) = fn_body(code, &builder) else {
            problems.push(format!("{file}: cannot read the body of `{builder}`"));
            continue;
        };
        for handler in registered(body) {
            if !handlers.contains(&handler) {
                handlers.push(handler);
            }
        }
    }
    let mut i = 0;
    while i < handlers.len() {
        let handler = handlers[i].clone();
        i += 1;
        let Some(how) = reach(file, &handler) else {
            problems.push(format!(
                "{file}: the route handler `{handler}` is not in the list"
            ));
            continue;
        };
        let Some(body) = fn_body(code, &handler) else {
            problems.push(format!(
                "{file}: the listed handler `{handler}` is not there"
            ));
            continue;
        };
        match how {
            Reach::Direct => {
                if !body.contains(evaluation.call) {
                    problems.push(format!(
                        "{file}: `{handler}` is listed as calling {} and does not",
                        evaluation.name
                    ));
                }
            }
            Reach::Prelude => {
                if !calls(body, PRELUDE) {
                    problems.push(format!(
                        "{file}: `{handler}` does not go through `{PRELUDE}`, which is where a \
                         v1.0 verb is judged"
                    ));
                }
                if !fn_body(code, PRELUDE).is_some_and(|b| b.contains(evaluation.call)) {
                    problems.push(format!(
                        "{file}: `{PRELUDE}` does not call {}",
                        evaluation.name
                    ));
                }
            }
            Reach::HandsOver(targets) => {
                for target in targets {
                    if !calls(body, target) {
                        problems.push(format!(
                            "{file}: `{handler}` is listed as handing over to `{target}` and \
                             does not"
                        ));
                    }
                    if !handlers.iter().any(|h| h == target) {
                        handlers.push((*target).to_string());
                    }
                }
                if body.contains("task_store::") || body.contains("skill_orchestrator::") {
                    problems.push(format!(
                        "{file}: `{handler}` is listed as only handing over, and does work of \
                         its own"
                    ));
                }
            }
            Reach::Exempt(_) => {}
        }
    }
    problems
}

fn read(root: &Path, file: &str) -> String {
    production(&fs::read_to_string(root.join(file)).unwrap_or_else(|e| panic!("read {file}: {e}")))
}

/// `(path as the lists name it, production code)` of every source file of
/// both editions.
fn all_production() -> Vec<(String, String)> {
    let mut out = Vec::new();
    let community = roots::tree("community").expect("the Community tree");
    let mut trees = vec![(community, String::new())];
    if let Some(enterprise) = roots::tree("enterprise") {
        trees.push((enterprise, "enterprise/".to_string()));
    }
    for (root, prefix) in trees {
        let mut files = Vec::new();
        rust_files(&root.join("src"), &mut files);
        files.sort();
        for path in files {
            let rel = path.strip_prefix(&root).unwrap().to_string_lossy();
            out.push((
                format!("{prefix}{rel}"),
                production(&fs::read_to_string(&path).unwrap()),
            ));
        }
    }
    out
}

#[test]
fn every_a2a_route_is_judged_by_the_one_evaluation() {
    let community = roots::tree("community").expect("the Community tree");
    let mut problems = Vec::new();
    for file in [LEGACY, V1] {
        let code = read(&community, file);
        problems.extend(problems_in(file, &code, &reach_of));
        for problem in problems_with(file, &code, ACTOR, &actor_reach_of)
            .into_iter()
            .chain(out_of_order(file, &code))
            .chain(own_actor_gates(file, &code))
        {
            if !problems.contains(&problem) {
                problems.push(problem);
            }
        }
    }
    // Nothing is listed that is not there.
    for (file, builder, handler, _, _) in ROUTES {
        let code = read(&community, file);
        if fn_body(&code, handler).is_none() {
            problems.push(format!(
                "{file}: the listed handler `{handler}` is not there"
            ));
        }
        if !fn_body(&code, builder).is_some_and(|b| registered(b).iter().any(|h| h == handler)) {
            problems.push(format!(
                "{file}: `{builder}` does not register the listed handler `{handler}`"
            ));
        }
    }
    assert!(
        problems.is_empty(),
        "an A2A route must be judged by the A2A policy of the caller's group chain and of the \
         card (`services::a2a_governance::enforce`) and by the caller's own grants and autonomy \
         (`services::a2a_governance::authorize_actor`), and tests/a2a_governance_guard.rs must \
         say how it reaches each:\n{}",
        problems.join("\n")
    );
}

#[test]
fn every_exemption_carries_its_reason() {
    for (file, _, handler, policy, actor) in ROUTES {
        for how in [policy, actor] {
            if let Reach::Exempt(why) = how {
                assert!(
                    why.len() > 40,
                    "{file}: `{handler}` is exempt with no reason"
                );
            }
        }
    }
}

#[test]
fn the_a2a_routes_live_where_this_guard_looks() {
    let mut problems = Vec::new();
    for (file, code) in all_production() {
        let name = file.rsplit('/').next().unwrap_or_default();
        let in_routes = file.contains("src/routes/");
        if in_routes && name.starts_with("a2a") && !A2A_ROUTE_FILES.contains(&file.as_str()) {
            problems.push(format!(
                "{file}: an A2A route file this guard does not read"
            ));
        }
        if A2A_ROUTE_FILES.contains(&file.as_str()) {
            continue;
        }
        for line in code.lines() {
            let mounts = line.contains(".route(\"") || line.contains(".nest(\"");
            if mounts && line.to_lowercase().contains("a2a") {
                problems.push(format!(
                    "{file}: mounts an A2A path outside the A2A route files: `{}`",
                    line.trim()
                ));
            }
            if line.contains(ENFORCE) || line.contains(AUTHORIZE) {
                problems.push(format!(
                    "{file}: calls the A2A evaluation from outside an A2A route"
                ));
            }
        }
    }
    assert!(
        problems.is_empty(),
        "A2A routes are defined in {LEGACY} and {V1} and mounted in src/app.rs; a route \
         anywhere else is one this guard cannot see:\n{}",
        problems.join("\n")
    );
}

#[test]
fn the_policy_documents_are_read_in_one_place() {
    let mut problems = Vec::new();
    for (file, code) in all_production() {
        if code.contains("a2a_governance_levels") && !LEVEL_READERS.contains(&file.as_str()) {
            problems.push(format!(
                "{file}: reads the group chain's A2A policy documents; only the evaluation does"
            ));
        }
        // A card's own policy is handed to the evaluation, nothing else.
        if [LEGACY, V1].contains(&file.as_str()) {
            let reads = code.matches("card.tenant_governance").count();
            let handed = code.matches("card.tenant_governance.as_ref(),").count();
            if reads != handed {
                problems.push(format!(
                    "{file}: reads a card's own A2A policy other than to hand it to \
                     `a2a_governance::policy_for`"
                ));
            }
            if code.matches("a2a_governance::policy_for(").count() != code.matches(ENFORCE).count()
            {
                problems.push(format!(
                    "{file}: a policy is loaded and not enforced, or enforced without being \
                     loaded"
                ));
            }
        }
        // The group row's own column: its declaration and a literal that
        // fills it are no read.
        let reads = code
            .lines()
            .filter(|l| l.contains("a2a_global_governance"))
            .filter(|l| {
                let l = l.trim();
                !l.starts_with("pub a2a_global_governance:")
                    && !l.starts_with("a2a_global_governance: None")
            })
            .count();
        if reads > 0
            && file != "src/services/a2a_governance.rs"
            && file != "src/services/cache/group_cache.rs"
        {
            problems.push(format!(
                "{file}: reads `a2a_global_governance`; only the group cache and the \
                 evaluation do"
            ));
        }
    }
    assert!(
        problems.is_empty(),
        "the A2A policy has one evaluation (`services::a2a_governance`):\n{}",
        problems.join("\n")
    );
}

// ---------------------------------------------------------------------------
// The guard against the shapes it forbids
// ---------------------------------------------------------------------------

const SAMPLE: &str = r#"
pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/{t}/{c}/message:send", post(send_message))
        .route("/{t}/{c}/tasks/{task_ref}", get(tasks_get_dispatch).post(new_verb))
}

async fn resolve_request(state: &SharedState) -> Result<Ctx, A2aError> {
    match a2a_governance::enforce(state, &policy, call).await {
        _ => {}
    }
}

pub(crate) async fn send_message(State(state): State<SharedState>) -> Response {
    let ctx = match resolve_request(&state).await {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };
}

pub(crate) async fn tasks_get_dispatch(State(state): State<SharedState>) -> Response {
    get_task(State(state)).await
}

pub(crate) async fn get_task(State(state): State<SharedState>) -> Response {
    let task = task_store::get(&state.db).await;
}

pub(crate) async fn new_verb(State(state): State<SharedState>) -> Response {
    let ctx = resolve_request(&state).await;
}
"#;

fn sample_reach(_: &str, handler: &str) -> Option<Reach> {
    match handler {
        "send_message" | "get_task" => Some(Reach::Prelude),
        "tasks_get_dispatch" => Some(Reach::HandsOver(&["get_task"])),
        _ => None,
    }
}

#[test]
fn the_guard_catches_a_route_that_is_not_listed_and_one_that_skips_the_evaluation() {
    let problems = problems_in("sample.rs", SAMPLE, &sample_reach);
    assert_eq!(
        problems,
        [
            // A route added to the router and not to the list.
            "sample.rs: the route handler `new_verb` is not in the list".to_string(),
            // A handler a dispatcher hands over to that skips the prelude.
            "sample.rs: `get_task` does not go through `resolve_request`, which is where a \
             v1.0 verb is judged"
                .to_string(),
        ]
    );
}

#[test]
fn the_guard_catches_a_prelude_that_stops_calling_the_evaluation() {
    let gutted = SAMPLE.replace("a2a_governance::enforce(", "a2a_governance::judge_only(");
    let problems = problems_in("sample.rs", &gutted, &sample_reach);
    assert!(
        problems
            .iter()
            .any(|p| p == "sample.rs: `resolve_request` does not call the evaluation"),
        "{problems:?}"
    );
}

#[test]
fn the_guard_catches_a_direct_route_without_the_call_and_a_dispatcher_that_works() {
    let code = r#"
pub fn router() -> Router<SharedState> {
    Router::new().route("/outbound", post(outbound)).route("/x", post(pick))
}
pub(crate) async fn outbound(State(state): State<SharedState>) -> Response {
    let policy = evaluate_locally(&state);
}
pub(crate) async fn pick(State(state): State<SharedState>) -> Response {
    let t = task_store::get(&state.db).await;
    outbound(State(state)).await
}
"#;
    let reach = |_: &str, h: &str| match h {
        "outbound" => Some(Reach::Direct),
        "pick" => Some(Reach::HandsOver(&["outbound"])),
        _ => None,
    };
    assert_eq!(
        problems_in("sample.rs", code, &reach),
        [
            "sample.rs: `outbound` is listed as calling the evaluation and does not".to_string(),
            "sample.rs: `pick` is listed as only handing over, and does work of its own"
                .to_string(),
        ]
    );
}

#[test]
fn the_scan_reads_routers_handlers_and_bodies() {
    assert_eq!(router_builders(SAMPLE), ["router"]);
    let body = fn_body(SAMPLE, "router").expect("router body");
    assert_eq!(
        registered(body),
        ["tasks_get_dispatch", "send_message", "new_verb"]
    );
    assert!(fn_body(SAMPLE, "get_task").is_some_and(|b| b.contains("task_store::get")));
    // `fn get_task(` is not found inside `fn tasks_get_task(`-like names.
    assert!(fn_body("async fn my_get_task() { x }", "get_task").is_none());
    assert!(calls(
        "let c = resolve_request(&s).await;",
        "resolve_request"
    ));
    assert!(!calls(
        "let c = try_resolve_request(&s).await;",
        "resolve_request"
    ));
}

// The caller's plane, against the shapes that shipped.

/// The legacy inbound route as it was: the policy, and no word of the
/// caller's grants or autonomy.
const INBOUND_WITHOUT_ITS_ACTOR_GATE: &str = r#"
pub fn router() -> Router<SharedState> {
    Router::new().route("/outbound", post(outbound)).route("/inbound", post(inbound))
}
pub(crate) async fn outbound(State(state): State<SharedState>) -> Response {
    let approved = match a2a_governance::authorize_actor(&state, actor).await {
        _ => false,
    };
    match a2a_governance::enforce(&state, &policy, call).await {
        _ => {}
    }
}
pub(crate) async fn inbound(State(state): State<SharedState>) -> Response {
    match a2a_governance::enforce(&state, &policy, call).await {
        _ => {}
    }
}
"#;

fn both_direct(_: &str, handler: &str) -> Option<Reach> {
    matches!(handler, "outbound" | "inbound").then_some(Reach::Direct)
}

#[test]
fn the_guard_catches_a_route_that_skips_the_callers_grant_and_autonomy_gate() {
    assert!(problems_in("sample.rs", INBOUND_WITHOUT_ITS_ACTOR_GATE, &both_direct).is_empty());
    assert_eq!(
        problems_with(
            "sample.rs",
            INBOUND_WITHOUT_ITS_ACTOR_GATE,
            ACTOR,
            &both_direct
        ),
        [
            "sample.rs: `inbound` is listed as calling the caller's grant and autonomy gate and \
             does not"
        ]
    );
}

#[test]
fn the_guard_catches_a_prelude_that_stops_calling_the_callers_gate() {
    let with_gate = SAMPLE.replace(
        "    match a2a_governance::enforce(state, &policy, call).await {",
        "    let approved = a2a_governance::authorize_actor(state, actor).await;\n    \
         match a2a_governance::enforce(state, &policy, call).await {",
    );
    assert_ne!(with_gate, SAMPLE, "the sample prelude calls the evaluation");
    // The problem is stated once for each handler that goes through the
    // prelude.
    let quiet = |problems: Vec<String>| -> Vec<String> {
        let mut said: Vec<String> = problems
            .into_iter()
            .filter(|p| p.contains("resolve_request` does not call"))
            .collect();
        said.dedup();
        said
    };
    assert!(quiet(problems_with("sample.rs", &with_gate, ACTOR, &sample_reach)).is_empty());
    assert_eq!(
        quiet(problems_with("sample.rs", SAMPLE, ACTOR, &sample_reach)),
        ["sample.rs: `resolve_request` does not call the caller's grant and autonomy gate"]
    );
}

#[test]
fn the_guard_catches_the_policy_judged_before_the_callers_gate() {
    assert!(out_of_order("sample.rs", INBOUND_WITHOUT_ITS_ACTOR_GATE).is_empty());
    let swapped = r#"
pub(crate) async fn inbound(State(state): State<SharedState>) -> Response {
    match a2a_governance::enforce(&state, &policy, call).await {
        _ => {}
    }
    let approved = a2a_governance::authorize_actor(&state, actor).await;
}
"#;
    assert_eq!(
        out_of_order("sample.rs", swapped),
        ["sample.rs: `inbound` judges the A2A policy before the caller's grant and autonomy gate"]
    );
}

#[test]
fn the_guard_catches_a_route_that_decides_from_grants_or_autonomy_itself() {
    // The two routes that had the gate each wrote it out.
    let written_out = r#"
pub(crate) async fn outbound(State(state): State<SharedState>) -> Response {
    if let Some(agent_id) = auth_ctx.actor_agent_id.clone() {
        use crate::services::agent_authz::{Decision, decide_for_agent};
        match decide_for_agent(&state.db, &tenant_id, &agent_id, "a2a_call", &card.id).await {
            _ => {}
        }
    } else if let Some((crate::services::autonomy::Verdict::Deny(reason), resolution)) =
        crate::services::autonomy::enforce_for_group(&state.db, &tenant_id, group_id).await
    {
        return refuse(reason);
    }
}
"#;
    assert_eq!(
        own_actor_gates("sample.rs", written_out),
        [
            "sample.rs: calls `decide_for_agent` itself; an A2A route reaches the caller's \
             grants and autonomy through `a2a_governance::authorize_actor`",
            "sample.rs: calls `enforce_for_group` itself; an A2A route reaches the caller's \
             grants and autonomy through `a2a_governance::authorize_actor`",
        ]
    );
    assert!(own_actor_gates("sample.rs", INBOUND_WITHOUT_ITS_ACTOR_GATE).is_empty());
}

// ---------------------------------------------------------------------------
// The legacy inbound route judges before it charges
// ---------------------------------------------------------------------------

/// The steps of the inbound route that decide and the ones that cost, in the
/// order they must run: the caller's own plane, the policy, the rate
/// limits (under a hold), the input guardrail, and only then the commit.
const INBOUND_ORDER: [&str; 5] = [
    "a2a_governance::authorize_actor(",
    "a2a_governance::enforce(",
    "usage::admit_a2a_limits(",
    "a2a_guardrail::check(",
    "step.commit().await",
];

/// What is wrong with the order of an inbound handler's body.
fn inbound_order_problems(body: &str) -> Vec<String> {
    let mut problems = Vec::new();
    let mut at = Vec::new();
    for step in INBOUND_ORDER {
        match body.find(step) {
            Some(i) => at.push(i),
            None => problems.push(format!("`{step}` is not called")),
        }
    }
    if problems.is_empty() && !at.windows(2).all(|w| w[0] < w[1]) {
        problems.push(format!(
            "the steps are out of order (authorisation, policy, limits, guardrail, commit): {at:?}"
        ));
    }
    if body.contains("usage::enforce_a2a_limits(") {
        problems.push(
            "the limits are admitted for good (`enforce_a2a_limits`): a call the guardrail \
             blocks would keep what it was charged"
                .to_string(),
        );
    }
    problems
}

#[test]
fn the_legacy_inbound_route_judges_before_it_charges() {
    let code = production(&read(Path::new("."), "src/routes/a2a.rs"));
    let body = fn_body(&code, "inbound").expect("the inbound handler");
    let problems = inbound_order_problems(body);
    assert!(
        problems.is_empty(),
        "src/routes/a2a.rs inbound: a refused or held call must spend no rate allowance, and a \
         blocked one must give it back:\n{}",
        problems.join("\n")
    );
}

/// The outbound route has the same steps in the same order: it admitted its
/// limits for good before the message guardrail, so a blocked call spent
/// allowance.
#[test]
fn the_outbound_route_judges_before_it_charges() {
    let code = production(&read(Path::new("."), "src/routes/a2a.rs"));
    let body = fn_body(&code, "outbound").expect("the outbound handler");
    let problems = inbound_order_problems(body);
    assert!(
        problems.is_empty(),
        "src/routes/a2a.rs outbound: a refused or held call must spend no rate allowance, and a \
         blocked one must give it back:\n{}",
        problems.join("\n")
    );
}

/// The steps of a v1.0 message verb, in the order they must run. The
/// caller's plane and the policy are judged in the prelude
/// (`resolve_request`); the limits are admitted under a hold, the message
/// guardrail judges, and only a dispatched message keeps its step and its
/// allowance.
const V1_VERB_ORDER: [&str; 5] = [
    "resolve_request(",
    "admit_v1_a2a_limits(",
    "a2a_guardrail::check(",
    "ctx.step_dispatched().await",
    "a2a_hold.dispatched().await",
];

/// What is wrong with the v1.0 route's order of judging and charging.
fn v1_order_problems(code: &str) -> Vec<String> {
    let mut problems = Vec::new();
    if code.contains("usage::enforce_a2a_limits(") {
        problems.push(
            "the limits are admitted for good (`enforce_a2a_limits`): a message the guardrail \
             blocks would keep what it was charged"
                .to_string(),
        );
    }
    match fn_body(code, "resolve_request") {
        None => problems.push("`resolve_request` is gone".to_string()),
        Some(prelude) => {
            match (
                prelude.find("a2a_governance::authorize_actor("),
                prelude.find("a2a_governance::enforce("),
            ) {
                (Some(actor), Some(policy)) if actor < policy => {}
                _ => problems.push(
                    "`resolve_request` does not judge the caller's plane and then the policy"
                        .to_string(),
                ),
            }
            if prelude.contains("admit_a2a_limits(") {
                problems.push("`resolve_request` admits the limits before a verb asks".to_string());
            }
        }
    }
    match fn_body(code, "admit_v1_a2a_limits") {
        Some(admit) if admit.contains("usage::admit_a2a_limits(") => {}
        _ => problems.push(
            "`admit_v1_a2a_limits` does not admit under a hold (`usage::admit_a2a_limits`)"
                .to_string(),
        ),
    }
    for verb in ["send_message", "send_streaming_message"] {
        let Some(body) = fn_body(code, verb) else {
            problems.push(format!("`{verb}` is gone"));
            continue;
        };
        let at: Vec<Option<usize>> = V1_VERB_ORDER.iter().map(|step| body.find(step)).collect();
        for (step, found) in V1_VERB_ORDER.iter().zip(&at) {
            if found.is_none() {
                problems.push(format!("`{verb}`: `{step}` is not called"));
            }
        }
        let at: Vec<usize> = at.into_iter().flatten().collect();
        if at.len() == V1_VERB_ORDER.len() && !at.windows(2).all(|w| w[0] < w[1]) {
            problems.push(format!(
                "`{verb}`: the steps are out of order (prelude, limits, guardrail, dispatch, \
                 keep the allowance): {at:?}"
            ));
        }
    }
    problems
}

#[test]
fn the_v1_message_verbs_judge_before_they_charge() {
    let code = production(&read(Path::new("."), "src/routes/a2a_v1.rs"));
    let problems = v1_order_problems(&code);
    assert!(
        problems.is_empty(),
        "src/routes/a2a_v1.rs: a message a gate refuses must spend no rate allowance:\n{}",
        problems.join("\n")
    );
}

#[test]
fn the_v1_order_guard_catches_the_orders_that_charged_first() {
    let prelude = "async fn resolve_request() {\n    a2a_governance::authorize_actor(x);\n    \
                   a2a_governance::enforce(x);\n}\n";
    let admit = "async fn admit_v1_a2a_limits() {\n    usage::admit_a2a_limits(x);\n}\n";
    let verb = |name: &str, body: &str| format!("async fn {name}() {{\n    {body}\n}}\n");
    let judged = "resolve_request(x); admit_v1_a2a_limits(x); a2a_guardrail::check(x); \
                  ctx.step_dispatched().await; a2a_hold.dispatched().await;";
    let good = format!(
        "{prelude}{admit}{}{}",
        verb("send_message", judged),
        verb("send_streaming_message", judged)
    );
    assert_eq!(v1_order_problems(&good), Vec::<String>::new());

    // The shape that shipped: admitted for good, before the guardrail.
    let for_good = "async fn enforce_v1_a2a_quota() {\n    usage::enforce_a2a_limits(x);\n}\n";
    let charged = "resolve_request(x); enforce_v1_a2a_quota(x); a2a_guardrail::check(x); \
                   ctx.step_dispatched().await;";
    let shipped = format!(
        "{prelude}{for_good}{}{}",
        verb("send_message", charged),
        verb("send_streaming_message", charged)
    );
    assert!(
        v1_order_problems(&shipped).len() >= 4,
        "{:?}",
        v1_order_problems(&shipped)
    );

    // Kept before the guardrail has judged.
    let kept_early = "resolve_request(x); admit_v1_a2a_limits(x); a2a_hold.dispatched().await; \
                      a2a_guardrail::check(x); ctx.step_dispatched().await;";
    let early = format!(
        "{prelude}{admit}{}{}",
        verb("send_message", judged),
        verb("send_streaming_message", kept_early)
    );
    assert_eq!(v1_order_problems(&early).len(), 1);

    // The outbound shape that shipped.
    let outbound = "a2a_governance::authorize_actor(x); a2a_governance::enforce(x); \
                    usage::enforce_a2a_limits(x); a2a_guardrail::check(x); step.commit().await;";
    assert_eq!(inbound_order_problems(outbound).len(), 2);
}

#[test]
fn the_inbound_order_guard_catches_the_orders_that_charged_first() {
    let judged_first = "a2a_governance::authorize_actor(x); a2a_governance::enforce(x); \
                        usage::admit_a2a_limits(x); a2a_guardrail::check(x); step.commit().await;";
    assert!(inbound_order_problems(judged_first).is_empty());
    let charged_first = "usage::admit_a2a_limits(x); a2a_guardrail::check(x); \
                         a2a_governance::authorize_actor(x); a2a_governance::enforce(x); \
                         step.commit().await;";
    assert_eq!(inbound_order_problems(charged_first).len(), 1);
    let for_good = "a2a_governance::authorize_actor(x); a2a_governance::enforce(x); \
                    usage::enforce_a2a_limits(x); a2a_guardrail::check(x); step.commit().await;";
    assert_eq!(inbound_order_problems(for_good).len(), 2);
}

// ---------------------------------------------------------------------------
// The agent card cache reads one schema
// ---------------------------------------------------------------------------

/// What in `code` retries a card or capability load with fewer columns when
/// the database says a column is missing.
fn narrower_projections(code: &str) -> Vec<&'static str> {
    [
        "_legacy(",
        "is_schema_mismatch",
        "\"42703\"",
        "\"42P01\"",
        "LegacyRow",
        "LegacyCapRow",
    ]
    .into_iter()
    .filter(|shape| code.contains(shape))
    .collect()
}

/// There is one schema. A load that falls back to a narrower projection
/// when a column is missing answers with a card that has no limits, no
/// policy and no handlers, and the routes then enforce nothing: a missing
/// column is an error, and the call is refused.
#[test]
fn the_card_cache_has_no_projection_for_another_schema() {
    let code = read(Path::new("."), "src/services/cache/agent_card_cache.rs");
    let found = narrower_projections(&code);
    assert!(
        found.is_empty(),
        "src/services/cache/agent_card_cache.rs loads a card with fewer columns when the \
         database lacks one: {found:?}"
    );
}

#[test]
fn the_projection_guard_catches_a_fallback() {
    assert_eq!(
        narrower_projections(
            "Err(e) if is_schema_mismatch(&e) => self.load_by_id_legacy(tenant_id, card_id).await,"
        ),
        ["_legacy(", "is_schema_mismatch"]
    );
    assert!(narrower_projections("Err(e) => Err(e),").is_empty());
}
