// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Which samples may be judged.
//!
//! A human resolution is a fact the detector must honour. Closing a drift
//! finding records *which* fact it is (`ai_system_drift_events.resolution_kind`,
//! written by the control plane), and each kind has one defined effect on
//! every signal that grades the affected samples:
//!
//! | Kind | Durable fact | Effect here |
//! |---|---|---|
//! | `accepted_change` | a **baseline epoch** at the finding's change point | samples before it are not that metric's baseline nor its observation window; it re-learns (`learning` until the minimum is met on post-epoch samples) |
//! | `false_positive` | a **baseline epoch** at the finding's detection time | the same, from the moment the detector was wrong |
//! | `cause_fixed` | an **incident window** `[change point, fixed)` for the system (or tenant) | samples inside it are judged by nothing — not baselines, not observation windows, not liveness shares |
//!
//! ## The rule (pinned with the control plane)
//!
//! A sample is excluded when its timestamp lies in `[starts_at, ends_at)` of
//! an incident window scoped to one of its systems or to the whole tenant
//! (`ai_system_id IS NULL`), or — for a metric — before the latest epoch of
//! (system, metric, basis) on its caller or on `'all'`. A run's timestamp is
//! `started_at` and its system `ai_system_id`; a segment's timestamp is
//! `first_action_at` and its systems the service (`segment_system_id`) and
//! the run's root (`root_system_id`).
//!
//! ## One rule, one place
//!
//! * [`incident_window_predicate`] is the ONE SQL fragment every core query
//!   that grades run or segment samples embeds (the drift sample queries and
//!   the liveness share detectors) — textually the control plane's
//!   `ai_system_judgement_exclusion.judged_sql`. The static guard in this
//!   module's tests fails when a grading query reads the run ledger
//!   without it.
//! * [`Epochs`] is per METRIC, so it cannot live in the sample query without
//!   one query per metric. It is applied to the (oldest-first) sample set as
//!   a suffix slice by [`MetricFloors::slice`], which the baseline
//!   statistics, the observation window and the change-point trend all go
//!   through — so the statistics code is not duplicated and detection cannot
//!   disagree with the baseline it is judged against.
//! * [`Closures`] is the second line: the sweeper does not record a finding
//!   for a key whose latest event was closed with a kind when the new
//!   finding's evidence is evidence that closure settled.
//!
//! Excluded samples are never deleted or hidden: runs in a window are still
//! recorded, sealed and exportable. They are only not *judged*.

use std::borrow::Borrow;
use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use sqlx::PgPool;

pub use crate::constants::drift_resolution::{
    ALL_CALLERS as EPOCH_ALL_CALLERS, BASELINE_EPOCHS_TABLE, INCIDENT_WINDOWS_TABLE,
    cause_category, kind,
};

use super::baseline::Sample;

/// The incident-window half of the exclusion rule, as a SQL boolean: TRUE
/// when the sample at `ts` still counts — it lies in no `[starts_at,
/// ends_at)` window declared for any of `systems` or for the whole tenant
/// (a provider outage).
///
/// Textually identical to the control plane's
/// `ai_system_judgement_exclusion.judged_sql(tenant, ts, *systems)`, pinned
/// by `the_predicate_is_the_control_planes`. Arguments are SQL expressions
/// in the embedding query (bind placeholders or column references), never
/// values.
pub fn incident_window_predicate(tenant: &str, ts: &str, systems: &[&str]) -> String {
    let scope = std::iter::once("iw.ai_system_id IS NULL".to_string())
        .chain(systems.iter().map(|s| format!("iw.ai_system_id = {s}")))
        .collect::<Vec<_>>()
        .join(" OR ");
    format!(
        "NOT EXISTS (SELECT 1 FROM {INCIDENT_WINDOWS_TABLE} iw \
         WHERE iw.tenant_id = {tenant} AND ({scope}) \
         AND {ts} >= iw.starts_at AND {ts} < iw.ends_at)"
    )
}

/// The latest baseline epoch per `(metric, caller)` for one system and
/// basis, whatever kind wrote it. An epoch keyed on [`EPOCH_ALL_CALLERS`]
/// applies to the aggregate AND every per-caller sub-baseline; a caller's
/// own epoch only to it.
///
/// Epochs also carry a `bucket`: one keyed `all`
/// ([`EPOCH_ALL_BUCKETS`]) covers every bucket of the metric (the aggregate
/// and each outcome bucket); one keyed on an outcome id — a promoted
/// catalogue change — re-learns only that bucket.
#[derive(Debug, Clone, Default)]
pub struct Epochs {
    by_key: BTreeMap<(String, String, String), DateTime<Utc>>,
}

/// The `bucket` of an epoch that applies to every bucket.
pub const EPOCH_ALL_BUCKETS: &str = "all";

const EPOCHS_SQL: &str = "SELECT metric_key, caller_system_id, bucket, MAX(starts_at) \
       FROM ai_system_baseline_epochs \
      WHERE tenant_id = $1 AND ai_system_id = $2 AND basis = $3 \
      GROUP BY metric_key, caller_system_id, bucket";

impl Epochs {
    /// No epochs — every sample is eligible.
    pub fn none() -> Self {
        Self::default()
    }

    /// Rows without a bucket: every one applies to all buckets.
    pub fn from_rows(rows: impl IntoIterator<Item = (String, String, DateTime<Utc>)>) -> Self {
        Self::from_bucket_rows(
            rows.into_iter()
                .map(|(m, c, at)| (m, c, EPOCH_ALL_BUCKETS.to_string(), at)),
        )
    }

    pub fn from_bucket_rows(
        rows: impl IntoIterator<Item = (String, String, String, DateTime<Utc>)>,
    ) -> Self {
        let mut by_key = BTreeMap::new();
        for (m, c, b, at) in rows {
            by_key
                .entry((m, c, b))
                .and_modify(|v: &mut DateTime<Utc>| *v = (*v).max(at))
                .or_insert(at);
        }
        Self { by_key }
    }

    /// An error is returned, never swallowed into "no epochs": without its
    /// epochs a re-learning baseline would be judged on pre-epoch data again
    /// and marked ready. The sweep skips the basis instead
    /// (`sweeper::sweep_system`).
    pub async fn load(
        db: &PgPool,
        tenant_id: &str,
        ai_system_id: &str,
        basis: &str,
    ) -> Result<Self, sqlx::Error> {
        let rows = sqlx::query_as::<_, (String, String, String, DateTime<Utc>)>(EPOCHS_SQL)
            .bind(tenant_id)
            .bind(ai_system_id)
            .bind(basis)
            .fetch_all(db)
            .await?;
        Ok(Self::from_bucket_rows(rows))
    }

    /// The epoch that governs `metric` for `caller` (the cohort's
    /// `caller_system_id`, [`EPOCH_ALL_CALLERS`] for the aggregate): the
    /// later of the metric's all-callers epoch and the caller's own.
    pub fn floor(&self, metric_key: &str, caller: &str) -> Option<DateTime<Utc>> {
        self.floor_in(metric_key, caller, EPOCH_ALL_BUCKETS)
    }

    /// The epoch that governs `metric` for `caller` in `bucket`: the latest
    /// of the all-callers and the caller's own epochs, each either for every
    /// bucket or for this one.
    pub fn floor_in(&self, metric_key: &str, caller: &str, bucket: &str) -> Option<DateTime<Utc>> {
        let mut callers = vec![EPOCH_ALL_CALLERS];
        if caller != EPOCH_ALL_CALLERS {
            callers.push(caller);
        }
        let mut buckets = vec![EPOCH_ALL_BUCKETS];
        if bucket != EPOCH_ALL_BUCKETS {
            buckets.push(bucket);
        }
        callers
            .iter()
            .flat_map(|c| buckets.iter().map(move |b| (*c, *b)))
            .filter_map(|(c, b)| {
                self.by_key
                    .get(&(metric_key.to_string(), c.to_string(), b.to_string()))
                    .copied()
            })
            .max()
    }

    /// The per-metric floors one cohort is judged under (bucket `all`).
    pub fn for_cohort(&self, caller: &str) -> MetricFloors {
        self.for_cohort_bucket(caller, EPOCH_ALL_BUCKETS)
    }

    /// The per-metric floors one cohort's bucket is judged under.
    pub fn for_cohort_bucket(&self, caller: &str, bucket: &str) -> MetricFloors {
        let mut floors = BTreeMap::new();
        for (m, _, _) in self.by_key.keys() {
            if let Some(f) = self.floor_in(m, caller, bucket) {
                floors.insert(m.clone(), f);
            }
        }
        MetricFloors { floors }
    }
}

/// Per-metric sample floors for one cohort (its baseline epochs).
#[derive(Debug, Clone, Default)]
pub struct MetricFloors {
    floors: BTreeMap<String, DateTime<Utc>>,
}

impl MetricFloors {
    pub fn none() -> Self {
        Self::default()
    }

    pub fn floor(&self, metric_key: &str) -> Option<DateTime<Utc>> {
        self.floors.get(metric_key).copied()
    }

    /// The samples `metric_key` may be judged on: the suffix of the
    /// oldest-first `samples` at or after the metric's epoch. A borrowed
    /// slice, so the same statistics code runs over it unchanged.
    pub fn slice<'a, S: Borrow<Sample>>(&self, samples: &'a [S], metric_key: &str) -> &'a [S] {
        match self.floor(metric_key) {
            Some(f) => &samples[samples.partition_point(|s| s.borrow().started_at < f)..],
            None => samples,
        }
    }
}

/// What a finding's evidence spans — the samples it was judged on.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Evidence {
    /// The first sample of the metric's baseline set.
    pub baseline_from: Option<DateTime<Utc>>,
    /// The first and last samples of the observation window.
    pub observed_from: Option<DateTime<Utc>>,
    pub observed_to: Option<DateTime<Utc>>,
    /// Where the detector located the change, if it did.
    pub change_point: Option<DateTime<Utc>>,
}

/// The latest event of one `(metric, caller)` key, when it was closed with a
/// kind that has an effect on detection — with the durable fact its closure
/// wrote (the epoch or the window, linked by `drift_event_id`).
#[derive(Debug, Clone, PartialEq)]
pub struct Closure {
    pub metric_key: String,
    /// NULL for the aggregate, as on the event.
    pub caller_system_id: Option<String>,
    pub kind: String,
    pub change_point_at: Option<DateTime<Utc>>,
    pub detected_at: Option<DateTime<Utc>>,
    pub resolved_at: Option<DateTime<Utc>>,
    /// The epoch the closure wrote (`accepted_change` / `false_positive`).
    pub epoch_starts_at: Option<DateTime<Utc>>,
    /// The incident window the closure declared (`cause_fixed`).
    pub window_starts_at: Option<DateTime<Utc>>,
    pub window_ends_at: Option<DateTime<Utc>>,
}

impl Closure {
    /// The moment from which the closure's effect holds: the epoch it wrote
    /// (else where the kind puts one — the change point for an accepted
    /// change, the detection time for a false positive), or the end of the
    /// window it declared (else the time it was closed).
    pub fn effect_point(&self) -> Option<DateTime<Utc>> {
        match self.kind.as_str() {
            kind::ACCEPTED_CHANGE => self
                .epoch_starts_at
                .or(self.change_point_at)
                .or(self.detected_at),
            kind::FALSE_POSITIVE => self.epoch_starts_at.or(self.detected_at),
            kind::CAUSE_FIXED => self.window_ends_at.or(self.resolved_at),
            _ => None,
        }
    }

    /// Whether a new finding on the same key would re-fire from evidence the
    /// closure already settled:
    ///
    /// * an epoch kind — the finding was judged against a baseline reaching
    ///   back before the epoch;
    /// * `cause_fixed` — its change lies inside the fixed period (a finding
    ///   with no change point: every observation does).
    pub fn settles(&self, e: &Evidence) -> bool {
        let Some(at) = self.effect_point() else {
            return false;
        };
        match self.kind.as_str() {
            kind::ACCEPTED_CHANGE | kind::FALSE_POSITIVE => e.baseline_from.is_some_and(|b| b < at),
            kind::CAUSE_FIXED => {
                let from = self.window_starts_at.or(self.change_point_at);
                match e.change_point.or(e.observed_to) {
                    Some(t) => t < at && from.is_none_or(|f| t >= f),
                    None => false,
                }
            }
            _ => false,
        }
    }
}

/// The latest closed-with-kind event per key for one system and basis.
#[derive(Debug, Clone, Default)]
pub struct Closures {
    rows: Vec<Closure>,
}

/// The LATEST event per `(metric, caller)` — an open event after a closed
/// one means the closure no longer speaks for the key — kept only when a
/// human closed it with a kind, joined to the epoch / window it wrote.
const CLOSURES_SQL: &str = "SELECT l.metric_key, l.caller_system_id, l.resolution_kind, \
            l.change_point_at, l.detected_at, l.resolved_at, \
            (SELECT MAX(ep.starts_at) FROM ai_system_baseline_epochs ep \
              WHERE ep.drift_event_id = l.id) AS epoch_starts_at, \
            w.starts_at AS window_starts_at, w.ends_at AS window_ends_at \
       FROM ( \
         SELECT DISTINCT ON (metric_key, caller_system_id) \
                id, metric_key, caller_system_id, resolution_kind, change_point_at, \
                detected_at, resolved_at \
           FROM ai_system_drift_events \
          WHERE tenant_id = $1 AND ai_system_id = $2 AND basis = $3 \
          ORDER BY metric_key, caller_system_id, detected_at DESC, id DESC \
       ) l \
       LEFT JOIN LATERAL ( \
         SELECT iw.starts_at, iw.ends_at FROM ai_system_incident_windows iw \
          WHERE iw.drift_event_id = l.id ORDER BY iw.ends_at DESC LIMIT 1 \
       ) w ON TRUE \
      WHERE l.resolution_kind = ANY($4)";

type ClosureRow = (
    String,
    Option<String>,
    String,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
);

impl Closures {
    pub fn from_rows(rows: Vec<Closure>) -> Self {
        Self { rows }
    }

    /// Like [`Epochs::load`], an error is returned rather than read as "no
    /// closures", which would let settled evidence re-fire.
    pub async fn load(
        db: &PgPool,
        tenant_id: &str,
        ai_system_id: &str,
        basis: &str,
    ) -> Result<Self, sqlx::Error> {
        let rows = sqlx::query_as::<_, ClosureRow>(CLOSURES_SQL)
            .bind(tenant_id)
            .bind(ai_system_id)
            .bind(basis)
            .bind(kind::HUMAN)
            .fetch_all(db)
            .await?;
        Ok(Self {
            rows: rows
                .into_iter()
                .map(|(m, c, k, cp, det, res, ep, ws, we)| Closure {
                    metric_key: m,
                    caller_system_id: c,
                    kind: k,
                    change_point_at: cp,
                    detected_at: det,
                    resolved_at: res,
                    epoch_starts_at: ep,
                    window_starts_at: ws,
                    window_ends_at: we,
                })
                .collect(),
        })
    }

    pub fn get(&self, metric_key: &str, caller: Option<&str>) -> Option<&Closure> {
        self.rows
            .iter()
            .find(|c| c.metric_key == metric_key && c.caller_system_id.as_deref() == caller)
    }

    /// Whether a finding on this key would re-fire from settled evidence.
    pub fn settles(&self, metric_key: &str, caller: Option<&str>, e: &Evidence) -> bool {
        self.get(metric_key, caller).is_some_and(|c| c.settles(e))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn t(h: i64) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-09-20T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
            + Duration::hours(h)
    }

    fn sample_at(at: DateTime<Utc>) -> Sample {
        Sample {
            started_at: at,
            action_count: 1,
            active_duration_ms: 1,
            tool_call_count: 1,
            llm_call_count: None,
            total_tokens: None,
            error_count: None,
            max_delegation_depth: None,
            cost_usd: 0.0,
            tool_error_count: 0,
            terminal_state: "completed".into(),
            tool_set_signature: None,
            first_prompt_tokens: None,
            input_bytes: None,
            peak_context_tokens: None,
            context_growth: None,
            client_label: None,
            models_used: None,
            caller_system_id: None,
            is_run: true,
            outcome_id: None,
            outcome_conformance: None,
            outcome_counts_toward_baseline: None,
            error_cause: None,
            run_id: None,
            tools_used: None,
        }
    }

    /// The control plane's `judged_sql("$1", "started_at", "$2")` and
    /// `judged_sql("$1", "s.first_action_at", "$2", "s.root_system_id")`,
    /// verbatim — one rule, two planes, the same text.
    #[test]
    fn the_predicate_is_the_control_planes() {
        assert_eq!(
            incident_window_predicate("$1", "started_at", &["$2"]),
            "NOT EXISTS (SELECT 1 FROM ai_system_incident_windows iw \
             WHERE iw.tenant_id = $1 AND (iw.ai_system_id IS NULL OR iw.ai_system_id = $2) \
             AND started_at >= iw.starts_at AND started_at < iw.ends_at)"
        );
        assert_eq!(
            incident_window_predicate("$1", "s.first_action_at", &["$2", "s.root_system_id"]),
            "NOT EXISTS (SELECT 1 FROM ai_system_incident_windows iw \
             WHERE iw.tenant_id = $1 AND (iw.ai_system_id IS NULL OR iw.ai_system_id = $2 \
             OR iw.ai_system_id = s.root_system_id) \
             AND s.first_action_at >= iw.starts_at AND s.first_action_at < iw.ends_at)"
        );
    }

    #[test]
    fn an_all_callers_epoch_governs_every_cohort_and_a_caller_epoch_only_its_own() {
        let e = Epochs::from_rows([
            ("action_count".into(), "all".into(), t(10)),
            ("action_count".into(), "sys-a".into(), t(20)),
            ("action_count".into(), "sys-b".into(), t(5)),
            ("cost_per_run".into(), "sys-a".into(), t(30)),
        ]);
        assert_eq!(e.floor("action_count", "all"), Some(t(10)));
        assert_eq!(
            e.floor("action_count", "sys-a"),
            Some(t(20)),
            "the later wins"
        );
        assert_eq!(
            e.floor("action_count", "sys-b"),
            Some(t(10)),
            "the later wins"
        );
        assert_eq!(e.floor("action_count", "sys-c"), Some(t(10)));
        assert_eq!(e.floor("cost_per_run", "all"), None);
        assert_eq!(e.floor("cost_per_run", "sys-a"), Some(t(30)));
        assert_eq!(e.floor("duration_ms", "sys-a"), None);
    }

    #[test]
    fn the_latest_epoch_wins() {
        let e = Epochs::from_rows([
            ("action_count".into(), "all".into(), t(10)),
            ("action_count".into(), "all".into(), t(40)),
            ("action_count".into(), "all".into(), t(20)),
        ]);
        assert_eq!(e.floor("action_count", "all"), Some(t(40)));
    }

    #[test]
    fn a_metric_slice_is_the_suffix_at_or_after_its_epoch() {
        let samples: Vec<Sample> = (0..10).map(|h| sample_at(t(h))).collect();
        let floors =
            Epochs::from_rows([("action_count".into(), "all".into(), t(6))]).for_cohort("all");
        let s = floors.slice(&samples, "action_count");
        assert_eq!(s.len(), 4, "hours 6..=9");
        assert_eq!(s[0].started_at, t(6), "the epoch sample itself counts");
        assert_eq!(
            floors.slice(&samples, "cost_per_run").len(),
            10,
            "an epoch is per metric"
        );
        assert_eq!(
            MetricFloors::none().slice(&samples, "action_count").len(),
            10
        );
    }

    fn closure(k: &str, cp: Option<i64>, detected: i64, resolved: i64) -> Closure {
        Closure {
            metric_key: "action_count".into(),
            caller_system_id: None,
            kind: k.into(),
            change_point_at: cp.map(t),
            detected_at: Some(t(detected)),
            resolved_at: Some(t(resolved)),
            epoch_starts_at: None,
            window_starts_at: None,
            window_ends_at: None,
        }
    }

    fn ev(base: i64, obs: (i64, i64), cp: Option<i64>) -> Evidence {
        Evidence {
            baseline_from: Some(t(base)),
            observed_from: Some(t(obs.0)),
            observed_to: Some(t(obs.1)),
            change_point: cp.map(t),
        }
    }

    #[test]
    fn epochs_are_loaded_whatever_their_kind() {
        // A `measurement_change` epoch (written by the control plane
        // when a platform upgrade changes how a metric is measured) must
        // re-learn the baseline exactly like a human's. The load is
        // deliberately kind-blind; a kind filter here would silently drop
        // every epoch kind it does not list.
        assert!(
            !EPOCHS_SQL.contains("kind"),
            "EPOCHS_SQL must not filter by kind: {EPOCHS_SQL}"
        );
        let at = chrono::DateTime::parse_from_rfc3339("2026-09-29T09:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let e = Epochs::from_rows([("client_mix".to_string(), EPOCH_ALL_CALLERS.to_string(), at)]);
        assert_eq!(e.floor("client_mix", EPOCH_ALL_CALLERS), Some(at));
        assert_eq!(e.floor("client_mix", "caller-x"), Some(at));
    }

    #[test]
    fn an_accepted_change_settles_findings_judged_against_pre_epoch_normal() {
        let mut c = closure(kind::ACCEPTED_CHANGE, Some(100), 140, 150);
        assert_eq!(
            c.effect_point(),
            Some(t(100)),
            "the change point, by default"
        );
        assert!(c.settles(&ev(0, (170, 190), Some(100))), "the same shift");
        assert!(
            !c.settles(&ev(100, (170, 190), Some(160))),
            "a new change judged on post-epoch normal is new evidence"
        );
        c.epoch_starts_at = Some(t(120));
        assert_eq!(
            c.effect_point(),
            Some(t(120)),
            "the epoch actually written wins"
        );
        assert!(c.settles(&ev(100, (170, 190), Some(160))));
    }

    #[test]
    fn a_false_positive_settles_findings_judged_against_pre_detection_normal() {
        let c = closure(kind::FALSE_POSITIVE, Some(100), 140, 150);
        assert_eq!(
            c.effect_point(),
            Some(t(140)),
            "its epoch is the detection time"
        );
        assert!(c.settles(&ev(0, (130, 160), Some(100))));
        assert!(!c.settles(&ev(140, (150, 160), Some(145))));
    }

    #[test]
    fn a_fixed_cause_settles_changes_inside_the_fixed_period_only() {
        let mut c = closure(kind::CAUSE_FIXED, Some(100), 105, 120);
        assert_eq!(c.effect_point(), Some(t(120)), "closed at, by default");
        assert!(c.settles(&ev(0, (110, 130), Some(105))));
        assert!(
            !c.settles(&ev(0, (110, 130), Some(125))),
            "new bad behaviour"
        );
        assert!(
            !c.settles(&ev(0, (110, 130), Some(90))),
            "an earlier, other change"
        );
        assert!(
            c.settles(&ev(0, (110, 115), None)),
            "all observed before the fix"
        );
        assert!(!c.settles(&ev(0, (110, 130), None)));
        c.window_starts_at = Some(t(95));
        c.window_ends_at = Some(t(126));
        assert_eq!(c.effect_point(), Some(t(126)), "the declared window wins");
        assert!(c.settles(&ev(0, (110, 130), Some(125))));
        assert!(c.settles(&ev(0, (110, 130), Some(96))));
    }

    #[test]
    fn auto_cleared_and_unknown_kinds_settle_nothing() {
        for k in [kind::AUTO_CLEARED, "whatever"] {
            let c = closure(k, Some(100), 110, 120);
            assert_eq!(c.effect_point(), None);
            assert!(!c.settles(&ev(0, (0, 1), Some(0))));
        }
    }

    #[test]
    fn a_closure_speaks_only_for_its_own_key() {
        let cs = Closures::from_rows(vec![closure(kind::ACCEPTED_CHANGE, Some(100), 110, 120)]);
        let e = ev(0, (170, 190), Some(100));
        assert!(cs.settles("action_count", None, &e));
        assert!(!cs.settles("action_count", Some("sys-a"), &e));
        assert!(!cs.settles("cost_per_run", None, &e));
    }

    #[test]
    fn the_closure_query_reads_only_the_latest_event_and_human_kinds() {
        assert!(CLOSURES_SQL.contains("DISTINCT ON (metric_key, caller_system_id)"));
        assert!(CLOSURES_SQL.contains("detected_at DESC"));
        assert!(CLOSURES_SQL.contains("l.resolution_kind = ANY($4)"));
        assert!(CLOSURES_SQL.contains("ep.drift_event_id = l.id"));
        assert!(CLOSURES_SQL.contains("iw.drift_event_id = l.id"));
        assert!(!kind::HUMAN.contains(&kind::AUTO_CLEARED));
    }

    // -- the static guard ------------------------------------------------------

    /// Every string literal in `src` that reads the run ledger — `FROM`/`JOIN`
    /// `ai_system_runs` or `ai_system_run_segments` — with its first line
    /// number. Skips `#[cfg(test)]` modules (everything after the first one).
    fn ledger_reading_literals(src: &str) -> Vec<(usize, String)> {
        let src = src.split("#[cfg(test)]").next().unwrap_or(src);
        let bytes = src.as_bytes();
        let mut out = Vec::new();
        let mut i = 0;
        while i < bytes.len() {
            // Line comments may quote SQL; skip them.
            if bytes[i] == b'/' && bytes.get(i + 1) == Some(&b'/') {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
                continue;
            }
            let (start, end) = if bytes[i] == b'r'
                && (bytes.get(i + 1) == Some(&b'#') || bytes.get(i + 1) == Some(&b'"'))
                && (i == 0 || !(bytes[i - 1].is_ascii_alphanumeric() || bytes[i - 1] == b'_'))
            {
                let mut j = i + 1;
                let mut hashes = 0;
                while bytes.get(j) == Some(&b'#') {
                    hashes += 1;
                    j += 1;
                }
                if bytes.get(j) != Some(&b'"') {
                    i += 1;
                    continue;
                }
                let close = format!("\"{}", "#".repeat(hashes));
                let body = j + 1;
                match src[body..].find(&close) {
                    Some(k) => (body, body + k),
                    None => break,
                }
            } else if bytes[i] == b'"' {
                let mut j = i + 1;
                while j < bytes.len() && bytes[j] != b'"' {
                    if bytes[j] == b'\\' {
                        j += 1;
                    }
                    j += 1;
                }
                (i + 1, j)
            } else if bytes[i] == b'\'' {
                // A char literal (e.g. '"') must not open a string.
                if bytes.get(i + 2) == Some(&b'\'') {
                    i += 3;
                } else if bytes.get(i + 1) == Some(&b'\\') && bytes.get(i + 3) == Some(&b'\'') {
                    i += 4;
                } else {
                    i += 1;
                }
                continue;
            } else {
                i += 1;
                continue;
            };
            let lit = &src[start..end];
            let norm = lit.split_whitespace().collect::<Vec<_>>().join(" ");
            if [
                "FROM ai_system_runs",
                "JOIN ai_system_runs",
                "FROM ai_system_run_segments",
                "JOIN ai_system_run_segments",
            ]
            .iter()
            .any(|p| norm.contains(p))
            {
                let line = src[..start].matches('\n').count() + 1;
                out.push((line, norm));
            }
            i = end + 1;
        }
        out
    }

    /// Ledger reads that deliberately do NOT grade behaviour, each with the
    /// reason. Matched on a distinctive substring of the literal.
    const NOT_GRADING: &[(&str, &str, &str)] = &[
        (
            "sweeper.rs",
            "COUNT(*) AS eligible_runs",
            "decides which systems are swept, grades nothing",
        ),
        (
            "cause.rs",
            "LAG(",
            "cause facts: a transition in the ledger (contract, release, fingerprint) \
             is a dated change, not a graded sample",
        ),
        (
            "cause.rs",
            "WITH before AS",
            "cause facts: the dominant prompt / corpus either side of the change point",
        ),
        (
            "cause.rs",
            "HAVING MIN(first_action_at)",
            "cause facts: when a caller first appeared",
        ),
    ];

    /// Every core query that grades run or segment samples
    /// embeds [`incident_window_predicate`]. Scans the drift module and the
    /// liveness detector; a new ledger read must either take the predicate
    /// (the `{excl}` placeholder the queries are built with) or be listed in
    /// [`NOT_GRADING`] with its reason. Unresolvable reads FAIL.
    #[test]
    fn every_grading_query_embeds_the_shared_exclusion() {
        let root = concat!(env!("CARGO_MANIFEST_DIR"), "/src/services");
        let files = [
            "drift/baseline.rs",
            "drift/sweeper.rs",
            "drift/cause.rs",
            "drift/detect.rs",
            "drift/profiles.rs",
            "drift/stats.rs",
            "drift/corpus.rs",
            "drift/mod.rs",
            "drift/exclusion.rs",
            // Names the metric definitions; holds no query of its own.
            "drift/measurement.rs",
            // First-seen shapes: pure, over samples the sweeper already loaded.
            "drift/new_shape.rs",
            "liveness.rs",
        ];
        // Every file in the module must be scanned — a new file is a new
        // place a grading query could hide.
        let listed: std::collections::BTreeSet<String> = files
            .iter()
            .filter_map(|f| f.strip_prefix("drift/"))
            .map(String::from)
            .collect();
        for entry in std::fs::read_dir(format!("{root}/drift")).unwrap() {
            let name = entry.unwrap().file_name().into_string().unwrap();
            assert!(
                listed.contains(&name),
                "drift/{name} is not scanned by this guard — add it to `files`"
            );
        }

        let mut graded = 0;
        let mut offenders = Vec::new();
        for f in files {
            let src = std::fs::read_to_string(format!("{root}/{f}")).unwrap();
            for (line, lit) in ledger_reading_literals(&src) {
                if lit.contains("{excl}") {
                    graded += 1;
                    continue;
                }
                let base = f.rsplit('/').next().unwrap();
                if NOT_GRADING
                    .iter()
                    .any(|(file, marker, _)| *file == base && lit.contains(marker))
                {
                    continue;
                }
                offenders.push(format!("{f}:{line}: {}", &lit[..lit.len().min(120)]));
            }
        }
        assert!(
            offenders.is_empty(),
            "run-ledger reads that neither embed the shared incident-window \
             exclusion nor are declared non-grading:\n{}",
            offenders.join("\n")
        );
        assert!(
            graded >= 3,
            "the two drift sample queries and the liveness share window must \
             embed the exclusion (found {graded})"
        );
    }

    /// The scanner itself must see through both literal forms, or the guard
    /// above passes by seeing nothing.
    #[test]
    fn the_scanner_finds_ledger_reads_in_both_literal_forms() {
        let src = "const A: &str = r#\"SELECT 1\n FROM ai_system_runs r\"#;\n\
                   let b = \"SELECT 1 FROM   ai_system_run_segments s\";\n\
                   // FROM ai_system_runs in a comment\n\
                   let c = '\"'; let d = \"JOIN ai_system_runs x {excl}\";";
        let found = ledger_reading_literals(src);
        assert_eq!(found.len(), 3, "{found:?}");
        assert!(found[2].1.contains("{excl}"));
    }

    #[test]
    fn a_bucket_epoch_re_learns_only_its_bucket() {
        // A promoted catalogue change on `app.loan_approved` puts that
        // bucket back to learning; the aggregate and other buckets keep
        // their data. An `all` epoch still covers every bucket.
        let e = Epochs::from_bucket_rows([
            (
                "action_count".into(),
                "all".into(),
                "app.loan_approved".into(),
                t(20),
            ),
            ("cost_per_run".into(), "all".into(), "all".into(), t(5)),
        ]);
        assert_eq!(
            e.floor_in("action_count", "all", "app.loan_approved"),
            Some(t(20))
        );
        assert_eq!(e.floor_in("action_count", "all", "app.loan_rejected"), None);
        assert_eq!(e.floor_in("action_count", "all", "all"), None);
        assert_eq!(
            e.floor_in("cost_per_run", "all", "app.loan_approved"),
            Some(t(5))
        );
        assert_eq!(e.floor_in("cost_per_run", "sys-a", "x"), Some(t(5)));
        let f = e.for_cohort_bucket("all", "app.loan_approved");
        assert_eq!(f.floor("action_count"), Some(t(20)));
        assert_eq!(e.for_cohort("all").floor("action_count"), None);
        assert!(EPOCHS_SQL.contains("bucket"));
    }
}
