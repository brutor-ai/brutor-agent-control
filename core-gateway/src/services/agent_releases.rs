// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Agent releases in the run ledger
//!
//! The per-call release ([`crate::services::agent_impl`]) rolls up here:
//!
//! * **the run's release** — `(name, version, build, trust)` of the run's
//!   first action that carries one, among the run actor's own actions when
//!   the actor is an agent (the same "first action" rule as the actor
//!   itself). A delegate's declaration never becomes its caller's release.
//! * **the implementation fingerprint** — over the first User-Agent,
//!   the first MCP `clientInfo` name+version, and the first instruction
//!   fingerprint among those same actions
//!   ([`agent_impl::implementation_fingerprint`] pins the encoding). The MCP
//!   inputs are read from rows whose release came from `clientInfo`: when a
//!   higher-trust carrier won, the row records that one instead.
//! * **each segment's release** — the delegate's, from the first carrying
//!   action in the segment's groups that is NOT the caller agent's own (the
//!   entering-side rows).
//! * **`agent_implementation_releases`** — one row per `(tenant, agent,
//!   name, version, build)`, upserted in the run's own transaction. A run
//!   counts toward the release of EVERY agent that acted in it: its own
//!   actor, and each segment's delegate (the agent whose declaration the
//!   segment carries, `ai_system_run_segments.delegate_agent_id`). Without
//!   the delegates, an agent that only ever acts inside other systems' runs
//!   — a shared service such as a fraud screener — never got a release row.
//!   One contribution per (run, agent): the actor's is the run's release; a
//!   delegate's is that of its earliest segment.
//!
//! ## Idempotent per (run, agent)
//!
//! A run is re-finalised whenever a late step reopens it. `run_count` must
//! count RUNS, so the accounting reads what the run contributed last time
//! (the stored run row and its stored segments, under a per-run advisory
//! lock taken before that read) and moves each agent's count only when its
//! release changed: unchanged → no count change; changed → −1 on the old
//! release, +1 on the new one; a new agent → +1; an agent no longer in the
//! run → −1. Replay runs contribute nothing.

use chrono::{DateTime, Utc};
use sqlx::{FromRow, PgConnection, Postgres, Transaction};

use crate::constants::agent_impl as wire;
use crate::services::agent_impl::{implementation_fingerprint, trust_for_source};

/// Most distinct fingerprints kept per release.
pub const MAX_FINGERPRINTS: i32 = 16;

/// A release as stored on a run or segment.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RunRelease {
    pub name: Option<String>,
    pub version: Option<String>,
    pub build: Option<String>,
    /// `verified` | `declared`; `None` when nothing was declared.
    pub trust: Option<&'static str>,
}

impl RunRelease {
    fn from_parts(
        name: Option<String>,
        version: Option<String>,
        build: Option<String>,
        source: Option<&str>,
    ) -> Self {
        match source {
            Some(s) => RunRelease {
                name,
                version,
                build,
                trust: Some(trust_for_source(s)),
            },
            None => RunRelease::default(),
        }
    }
}

/// The run-level release plus the fingerprint inputs, from the actor's
/// actions. `$1` tenant, `$2` root, `$3` actor agent id (NULL = every
/// action), `$4` the `mcp_client_info` source value.
const RUN_RELEASE_SQL: &str = r#"
SELECT
    (ARRAY_AGG(pl.agent_impl_name ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.agent_impl_source IS NOT NULL))[1]        AS name,
    (ARRAY_AGG(pl.agent_impl_version ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.agent_impl_source IS NOT NULL))[1]        AS version,
    (ARRAY_AGG(pl.agent_impl_build ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.agent_impl_source IS NOT NULL))[1]        AS build,
    (ARRAY_AGG(pl.agent_impl_source ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.agent_impl_source IS NOT NULL))[1]        AS source,
    (ARRAY_AGG(pl.user_agent ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.user_agent IS NOT NULL))[1]               AS user_agent,
    (ARRAY_AGG(pl.agent_impl_name ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.agent_impl_source = $4::varchar))[1]      AS mcp_name,
    (ARRAY_AGG(pl.agent_impl_version ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.agent_impl_source = $4::varchar))[1]      AS mcp_version,
    (ARRAY_AGG(pl.prompt_fingerprint ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.prompt_fingerprint IS NOT NULL))[1]       AS prompt_fingerprint
FROM proxy_logs pl
WHERE pl.tenant_id = $1 AND pl.root_task_id = $2
  AND ($3::varchar IS NULL OR pl.actor_agent_id = $3::varchar)
"#;

#[derive(Debug, FromRow)]
struct RunReleaseRow {
    name: Option<String>,
    version: Option<String>,
    build: Option<String>,
    source: Option<String>,
    user_agent: Option<String>,
    mcp_name: Option<String>,
    mcp_version: Option<String>,
    prompt_fingerprint: Option<String>,
}

/// The run's release and implementation fingerprint. Read inside the run
/// aggregate's snapshot so it sees the same steps.
pub async fn load_run_release(
    conn: &mut PgConnection,
    tenant_id: &str,
    root_task_id: &str,
    actor_agent_id: Option<&str>,
) -> Result<(RunRelease, Option<String>), sqlx::Error> {
    let row = sqlx::query_as::<_, RunReleaseRow>(RUN_RELEASE_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(actor_agent_id)
        .bind(wire::source::MCP_CLIENT_INFO)
        .fetch_one(conn)
        .await?;
    let fingerprint = implementation_fingerprint(
        row.user_agent.as_deref(),
        row.mcp_name.as_deref(),
        row.mcp_version.as_deref(),
        row.prompt_fingerprint.as_deref(),
    );
    Ok((
        RunRelease::from_parts(row.name, row.version, row.build, row.source.as_deref()),
        fingerprint,
    ))
}

/// The first carrying action per `(group, actor)` in a run — the input to
/// segment release inheritance.
#[derive(Debug, Clone, Default, FromRow, PartialEq)]
pub struct PartRelease {
    pub group_id: Option<String>,
    pub actor_agent_id: Option<String>,
    pub at: DateTime<Utc>,
    pub id: String,
    pub name: Option<String>,
    pub version: Option<String>,
    pub build: Option<String>,
    pub source: String,
}

const PART_RELEASES_SQL: &str = r#"
SELECT DISTINCT ON (pl.resource_group_id, pl.actor_agent_id)
       pl.resource_group_id  AS group_id,
       pl.actor_agent_id,
       pl.timestamp          AS at,
       pl.id,
       pl.agent_impl_name    AS name,
       pl.agent_impl_version AS version,
       pl.agent_impl_build   AS build,
       pl.agent_impl_source  AS source
  FROM proxy_logs pl
 WHERE pl.tenant_id = $1 AND pl.root_task_id = $2
   AND pl.agent_impl_source IS NOT NULL
 ORDER BY pl.resource_group_id, pl.actor_agent_id, pl.timestamp, pl.id
"#;

pub async fn load_part_releases(
    conn: &mut PgConnection,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<Vec<PartRelease>, sqlx::Error> {
    sqlx::query_as::<_, PartRelease>(PART_RELEASES_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .fetch_all(conn)
        .await
}

/// A segment's release and the agent that declared it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SegmentRelease {
    pub release: RunRelease,
    /// The delegate agent identity whose declaration this is (`None` when
    /// the carrying action was not an agent's).
    pub delegate_agent_id: Option<String>,
}

/// A segment inherits the DELEGATE's release: the earliest carrying action
/// in the segment's groups whose actor is neither the agent that called into
/// the segment nor the run's own actor — their declarations are theirs, even
/// on a row filed under the service's group. Pure.
pub fn segment_release(
    group_ids: &[String],
    caller_agent_id: Option<&str>,
    run_actor_agent_id: Option<&str>,
    releases: &[PartRelease],
) -> SegmentRelease {
    let not_the_callers = |actor: Option<&str>| {
        actor.is_none_or(|a| Some(a) != caller_agent_id && Some(a) != run_actor_agent_id)
    };
    releases
        .iter()
        .filter(|r| r.group_id.as_ref().is_some_and(|g| group_ids.contains(g)))
        .filter(|r| not_the_callers(r.actor_agent_id.as_deref()))
        .min_by(|a, b| (a.at, &a.id).cmp(&(b.at, &b.id)))
        .map(|r| SegmentRelease {
            release: RunRelease::from_parts(
                r.name.clone(),
                r.version.clone(),
                r.build.clone(),
                Some(&r.source),
            ),
            delegate_agent_id: r.actor_agent_id.clone(),
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// agent_implementation_releases — idempotent per run
// ---------------------------------------------------------------------------

/// A release a run counts toward: `(agent, name, version, build)`, build ''
/// when absent (the sentinel of the unique key).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseKey {
    pub agent_id: String,
    pub name: String,
    pub version: String,
    pub build: String,
}

impl ReleaseKey {
    /// Only an agent-actor run that declared BOTH a name and a version
    /// counts toward a release (name and version are NOT NULL).
    pub fn of(agent_id: Option<&str>, release: &RunRelease) -> Option<ReleaseKey> {
        Some(ReleaseKey {
            agent_id: agent_id?.to_string(),
            name: release.name.clone()?,
            version: release.version.clone()?,
            build: release.build.clone().unwrap_or_default(),
        })
    }
}

/// One agent's contribution of one run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Contribution {
    pub key: ReleaseKey,
    /// `verified` | `declared` — the trust of the release's name/version.
    pub trust: Option<&'static str>,
    /// The run's implementation fingerprint (the actor's only: a delegate's
    /// software surface is not fingerprinted).
    pub fingerprint: Option<String>,
    pub first_seen: DateTime<Utc>,
    pub last_seen: DateTime<Utc>,
}

/// What a run contributes: the actor's contribution first, then each
/// segment's in segment order — one per agent (the first wins). Pure.
pub fn contributions(
    actor: Option<Contribution>,
    segments: impl IntoIterator<Item = Option<Contribution>>,
) -> Vec<Contribution> {
    let mut out: Vec<Contribution> = Vec::new();
    for c in actor.into_iter().chain(segments.into_iter().flatten()) {
        if !out.iter().any(|o| o.key.agent_id == c.key.agent_id) {
            out.push(c);
        }
    }
    out
}

/// One count move of a finalisation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CountMove {
    /// Same release as last time: refresh the row, do not count again.
    Touch(ReleaseKey),
    /// A release this run now counts toward (+1).
    Add(ReleaseKey),
    /// A release this run no longer counts toward (−1).
    Withdraw(ReleaseKey),
}

/// The moves from what the run contributed last time to what it contributes
/// now, per agent. Pure, so the idempotence rule is testable without a
/// database.
pub fn count_moves(prior: &[ReleaseKey], now: &[ReleaseKey]) -> Vec<CountMove> {
    let mut moves = Vec::new();
    for n in now {
        moves.push(if prior.contains(n) {
            CountMove::Touch(n.clone())
        } else {
            CountMove::Add(n.clone())
        });
    }
    for p in prior {
        if !now.contains(p) {
            moves.push(CountMove::Withdraw(p.clone()));
        }
    }
    moves
}

/// The earliest COUNTING segment per delegate agent (segment order is
/// `(first_action_at, segment_system_id)`, as `RunPartition` orders them),
/// excluding the run's actor — the same rule [`contributions`] applies to
/// the segments it is given.
const PRIOR_SEGMENTS_SQL: &str = r#"
SELECT DISTINCT ON (s.delegate_agent_id)
       s.delegate_agent_id, s.agent_impl_name, s.agent_impl_version, s.agent_impl_build
  FROM ai_system_run_segments s
 WHERE s.tenant_id = $1 AND s.root_task_id = $2
   AND s.delegate_agent_id IS NOT NULL
   AND s.delegate_agent_id IS DISTINCT FROM $3::varchar
   -- Only segments that counted: `ReleaseKey::of` needs name and version.
   AND s.agent_impl_name IS NOT NULL AND s.agent_impl_version IS NOT NULL
 ORDER BY s.delegate_agent_id, s.first_action_at, s.segment_system_id
"#;

/// Serialise every finalisation of ONE run, and read what it contributed
/// last time (the actor's release and each delegate's). Must run first in
/// the run's write transaction, before the run row and its segments are
/// rewritten.
pub async fn lock_and_read_prior(
    tx: &mut Transaction<'_, Postgres>,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<Vec<ReleaseKey>, sqlx::Error> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("agent-release:run:{tenant_id}:{root_task_id}"))
        .execute(&mut **tx)
        .await?;
    let run: Option<(
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        bool,
    )> = sqlx::query_as(
        "SELECT agent_id, agent_impl_name, agent_impl_version, agent_impl_build, is_replay \
               FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_optional(&mut **tx)
    .await?;
    let Some((agent, name, version, build, is_replay)) = run else {
        return Ok(Vec::new());
    };
    if is_replay {
        return Ok(Vec::new());
    }
    let mut prior: Vec<ReleaseKey> = ReleaseKey::of(
        agent.as_deref(),
        &RunRelease {
            name,
            version,
            build,
            trust: None,
        },
    )
    .into_iter()
    .collect();
    let segments: Vec<(
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    )> = sqlx::query_as(PRIOR_SEGMENTS_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(agent.as_deref())
        .fetch_all(&mut **tx)
        .await?;
    for (delegate, name, version, build) in segments {
        if let Some(k) = ReleaseKey::of(
            delegate.as_deref(),
            &RunRelease {
                name,
                version,
                build,
                trust: None,
            },
        ) {
            prior.push(k);
        }
    }
    Ok(prior)
}

const UPSERT_RELEASE_SQL: &str = r#"
INSERT INTO agent_implementation_releases (
    id, tenant_id, agent_id, name, version, build, trust,
    first_seen_at, last_seen_at, run_count, fingerprints
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    CASE WHEN $11::varchar IS NULL THEN '[]'::jsonb ELSE jsonb_build_array($11::varchar) END
)
ON CONFLICT (tenant_id, agent_id, name, version, build) DO UPDATE SET
    first_seen_at = LEAST(agent_implementation_releases.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at = GREATEST(agent_implementation_releases.last_seen_at, EXCLUDED.last_seen_at),
    run_count = agent_implementation_releases.run_count + EXCLUDED.run_count,
    -- The strongest trust ever seen for this release.
    trust = CASE WHEN agent_implementation_releases.trust = $12::varchar
                   OR EXCLUDED.trust = $12::varchar
                 THEN $12::varchar ELSE EXCLUDED.trust END,
    -- Distinct fingerprints, capped: a release that behaves like 16
    -- different programs has said everything a 17th could.
    fingerprints = CASE
        WHEN $11::varchar IS NULL
          OR agent_implementation_releases.fingerprints ? $11::varchar
          OR jsonb_array_length(agent_implementation_releases.fingerprints) >= $13
        THEN agent_implementation_releases.fingerprints
        ELSE agent_implementation_releases.fingerprints || jsonb_build_array($11::varchar)
    END
"#;

/// Apply this run's contributions to `agent_implementation_releases`, in
/// the run's own transaction.
pub async fn account(
    tx: &mut Transaction<'_, Postgres>,
    tenant_id: &str,
    prior: &[ReleaseKey],
    now: &[Contribution],
) -> Result<(), sqlx::Error> {
    let now_keys: Vec<ReleaseKey> = now.iter().map(|c| c.key.clone()).collect();
    for mv in count_moves(prior, &now_keys) {
        let (key, add) = match mv {
            // Counted by an earlier finalisation: refresh seen-times, trust
            // and fingerprints without counting the run again.
            CountMove::Touch(k) => (k, 0),
            CountMove::Add(k) => (k, 1),
            CountMove::Withdraw(old) => {
                sqlx::query(
                    "UPDATE agent_implementation_releases \
                        SET run_count = GREATEST(run_count - 1, 0) \
                      WHERE tenant_id = $1 AND agent_id = $2 AND name = $3 \
                        AND version = $4 AND build = $5",
                )
                .bind(tenant_id)
                .bind(&old.agent_id)
                .bind(&old.name)
                .bind(&old.version)
                .bind(&old.build)
                .execute(&mut **tx)
                .await?;
                continue;
            }
        };
        let c = now
            .iter()
            .find(|c| c.key == key)
            .expect("a touched or added key is one of this run's contributions");
        sqlx::query(UPSERT_RELEASE_SQL)
            .bind(format!("rel-{}", ulid::Ulid::new()))
            .bind(tenant_id)
            .bind(&key.agent_id)
            .bind(&key.name)
            .bind(&key.version)
            .bind(&key.build)
            .bind(c.trust.unwrap_or(wire::trust::DECLARED))
            .bind(c.first_seen)
            .bind(c.last_seen)
            .bind(add)
            .bind(c.fingerprint.as_deref())
            .bind(wire::trust::VERIFIED)
            .bind(MAX_FINGERPRINTS)
            .execute(&mut **tx)
            .await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(v: &str) -> ReleaseKey {
        ReleaseKey {
            agent_id: "agent-1".into(),
            name: "screening-agent".into(),
            version: v.into(),
            build: String::new(),
        }
    }

    fn key_of(agent: &str, v: &str) -> ReleaseKey {
        ReleaseKey {
            agent_id: agent.into(),
            ..key(v)
        }
    }

    fn contrib(k: ReleaseKey) -> Contribution {
        Contribution {
            key: k,
            trust: Some("declared"),
            fingerprint: None,
            first_seen: DateTime::from_timestamp(1, 0).unwrap(),
            last_seen: DateTime::from_timestamp(2, 0).unwrap(),
        }
    }

    #[test]
    fn a_reopen_with_the_same_releases_never_counts_twice() {
        let both = [key("1.0"), key_of("screener", "2.0")];
        assert_eq!(
            count_moves(&both, &both),
            vec![
                CountMove::Touch(key("1.0")),
                CountMove::Touch(key_of("screener", "2.0"))
            ]
        );
    }

    #[test]
    fn first_finalisation_counts_each_agent_once() {
        assert_eq!(
            count_moves(&[], &[key("1.0"), key_of("screener", "2.0")]),
            vec![
                CountMove::Add(key("1.0")),
                CountMove::Add(key_of("screener", "2.0"))
            ]
        );
    }

    #[test]
    fn a_changed_release_moves_that_agents_count_only() {
        let moves = count_moves(
            &[key("1.0"), key_of("screener", "2.0")],
            &[key("1.0"), key_of("screener", "2.1")],
        );
        assert_eq!(
            moves,
            vec![
                CountMove::Touch(key("1.0")),
                CountMove::Add(key_of("screener", "2.1")),
                CountMove::Withdraw(key_of("screener", "2.0")),
            ]
        );
        assert_eq!(
            count_moves(&[key("1.0")], &[]),
            vec![CountMove::Withdraw(key("1.0"))],
            "a run that no longer counts (e.g. now a replay) withdraws"
        );
    }

    #[test]
    fn one_contribution_per_agent_the_actor_first() {
        let c = contributions(
            Some(contrib(key("1.0"))),
            [
                Some(contrib(key_of("screener", "2.0"))),
                None,                                     // a segment with no delegate release
                Some(contrib(key_of("screener", "2.1"))), // a later segment
                Some(contrib(key("9.9"))),                // the actor again, as a delegate
            ],
        );
        let keys: Vec<_> = c.into_iter().map(|c| c.key).collect();
        assert_eq!(keys, vec![key("1.0"), key_of("screener", "2.0")]);
    }

    #[test]
    fn only_agent_runs_with_name_and_version_count() {
        let full = RunRelease {
            name: Some("a".into()),
            version: Some("1".into()),
            build: None,
            trust: Some("declared"),
        };
        assert!(ReleaseKey::of(None, &full).is_none(), "no agent actor");
        assert!(
            ReleaseKey::of(
                Some("ag"),
                &RunRelease {
                    version: None,
                    ..full.clone()
                }
            )
            .is_none()
        );
        assert!(
            ReleaseKey::of(
                Some("ag"),
                &RunRelease {
                    name: None,
                    ..full.clone()
                }
            )
            .is_none()
        );
        assert_eq!(
            ReleaseKey::of(Some("ag"), &full).unwrap().build,
            "",
            "'' = no build"
        );
    }

    fn part(group: &str, actor: Option<&str>, at_s: i64, name: &str) -> PartRelease {
        PartRelease {
            group_id: Some(group.into()),
            actor_agent_id: actor.map(str::to_string),
            at: DateTime::from_timestamp(at_s, 0).unwrap(),
            id: format!("pl-{at_s}"),
            name: Some(name.into()),
            version: Some("1".into()),
            build: None,
            source: "header".into(),
        }
    }

    #[test]
    fn a_segment_inherits_the_delegates_release_not_the_callers() {
        let releases = vec![
            // The caller's own declaration on an entering row in the
            // delegate's group comes first in time…
            part("svc-g", Some("caller"), 10, "caller-agent"),
            // …but the segment is the delegate's.
            part("svc-g", Some("delegate"), 11, "fraud-screener"),
            part("other-g", Some("x"), 1, "unrelated"),
        ];
        let r = segment_release(&["svc-g".into()], Some("caller"), None, &releases);
        assert_eq!(r.release.name.as_deref(), Some("fraud-screener"));
        assert_eq!(r.release.trust, Some("declared"));
        assert_eq!(r.delegate_agent_id.as_deref(), Some("delegate"));
        // No entering call recorded: the run's actor is excluded instead.
        let r = segment_release(&["svc-g".into()], None, Some("caller"), &releases);
        assert_eq!(r.release.name.as_deref(), Some("fraud-screener"));
        assert_eq!(
            segment_release(&["none".into()], Some("caller"), None, &releases),
            SegmentRelease::default()
        );
    }
}
