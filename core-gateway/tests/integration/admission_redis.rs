// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The admission engine's Lua against a REAL Redis.
//!
//! Every limit a request touches is decided and reserved by one script
//! (`usage_state::admission`). These tests run that script, not the
//! in-process twin, because only a real Redis is evidence that concurrent
//! requests — on one replica or several — cannot all pass a limit
//! (`README.md`, "Engineering rules", rule 8).
//!
//! * the twin and the script agree, step for step, on a long random sequence;
//! * N concurrent admissions over two independent connections (two replicas)
//!   admit exactly the limit, for counts and for worst-case token caps;
//! * a replica that dies leaves holds whose lease lapses: another replica's
//!   reaper frees their in-flight slots and keeps their spend;
//! * a counter the script cannot operate on refuses (fail closed).
//!
//! Needs `TEST_REDIS_URL` (CI provides a service container).

use std::sync::Arc;

use brutor_gateway_core::services::cache::usage_state::{
    Actual, Admission, Dim, Finish, Measure, Shape, UsageState, Verdict,
};

fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
}

async fn conn() -> redis::aio::ConnectionManager {
    let url = redis_url();
    let client = redis::Client::open(url.as_str()).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

/// One replica: its own connection, its own live-hold registry.
async fn replica() -> Arc<UsageState> {
    Arc::new(UsageState::new(Some(conn().await)))
}

fn ns() -> String {
    format!("adm-test-{}", uuid::Uuid::new_v4().simple())
}

fn count(key: &str, limit: i64) -> Dim {
    Dim::counter(key.to_string(), Some(limit), 3600, Measure::One)
}

fn tokens(in_t: i64, out: i64) -> Shape {
    Shape {
        in_tokens: in_t,
        in_estimate: in_t / 4,
        out_tokens: out,
        min_out: 1,
        choices: 1,
        in_micros_per_token: 2.0,
        out_micros_per_token: 8.0,
    }
}

/// Tiny deterministic generator: the sequence must be reproducible.
struct Lcg(u64);
impl Lcg {
    fn next(&mut self, n: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) % n
    }
}

#[tokio::test]
async fn the_twin_and_the_script_agree_step_for_step() {
    let redis = replica().await;
    let mem = Arc::new(UsageState::new(None));
    let ns = ns();
    let keys: Vec<String> = (0..4).map(|i| format!("{ns}:c{i}")).collect();
    let mut rng = Lcg(0x5eed);
    let mut open: Vec<String> = Vec::new();

    for step in 0..300 {
        let roll = rng.next(10);
        if roll < 6 || open.is_empty() {
            // An admission over 1–3 counters with mixed measures.
            let n = 1 + rng.next(3) as usize;
            let start = rng.next(4) as usize;
            let mut dims = Vec::new();
            for j in 0..n {
                // Distinct keys within one admission (one key twice is a
                // programming error the engine refuses).
                let k = &keys[(start + j) % 4];
                let (m, lim) = match rng.next(3) {
                    0 => (Measure::One, 1 + rng.next(40) as i64),
                    1 => (Measure::TotalTokens, 500 + rng.next(20_000) as i64),
                    _ => (Measure::CostMicros, 1_000 + rng.next(200_000) as i64),
                };
                dims.push(Dim::counter(format!("{k}:{m:?}"), Some(lim), 3600, m));
            }
            let id = format!("{ns}-h{step}");
            let adm = Admission {
                hold_id: Some(id.clone()),
                dims,
                shape: Shape {
                    min_out: 1 + rng.next(64) as i64,
                    ..tokens(rng.next(400) as i64, 1 + rng.next(3000) as i64)
                },
            };
            let a = redis.admit(&adm).await.expect("redis admit");
            let b = mem.admit(&adm).await.expect("mem admit");
            let same = match (&a, &b) {
                (Verdict::Admitted { out_tokens: x }, Verdict::Admitted { out_tokens: y }) => {
                    x == y
                }
                (Verdict::Refused { dim: x, .. }, Verdict::Refused { dim: y, .. }) => x == y,
                _ => false,
            };
            assert!(same, "step {step}: redis {a:?} vs twin {b:?}");
            if matches!(a, Verdict::Admitted { .. }) {
                open.push(id);
            }
        } else {
            let id = open.remove(rng.next(open.len() as u64) as usize);
            let how = match rng.next(3) {
                0 => Finish::Release,
                1 => Finish::Abandon,
                _ => Finish::Settle(Actual {
                    in_tokens: rng.next(300) as i64,
                    out_tokens: rng.next(800) as i64,
                    cost_micros: rng.next(5_000) as i64,
                }),
            };
            let a = redis
                .finish_hold(&id, how, &[])
                .await
                .expect("redis finish");
            let b = mem.finish_hold(&id, how, &[]).await.expect("mem finish");
            assert_eq!(a, b, "step {step}: finish {how:?}");
        }
        for k in &keys {
            for m in [Measure::One, Measure::TotalTokens, Measure::CostMicros] {
                let key = format!("{k}:{m:?}");
                assert_eq!(
                    redis.counter_value(&key).await.unwrap(),
                    mem.counter_value(&key).await.unwrap(),
                    "step {step}: counter {key}"
                );
            }
        }
    }
}

#[tokio::test]
async fn concurrent_admissions_across_two_replicas_admit_exactly_the_limit() {
    let (a, b) = (replica().await, replica().await);
    let key = format!("{}:calls", ns());
    let tasks: Vec<_> = (0..60)
        .map(|i| {
            let us = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            tokio::spawn(async move {
                let adm = Admission {
                    hold_id: Some(format!("{key}-{i}")),
                    dims: vec![count(&key, 7)],
                    shape: Shape::count(),
                };
                matches!(us.admit(&adm).await.unwrap(), Verdict::Admitted { .. })
            })
        })
        .collect();
    let mut admitted = 0;
    for t in tasks {
        admitted += t.await.unwrap() as i32;
    }
    assert_eq!(admitted, 7);
    assert_eq!(a.counter_value(&key).await.unwrap(), 7);
}

#[tokio::test]
async fn a_token_cap_cannot_be_overshot_by_concurrent_worst_cases() {
    // 5,000-token cap; each call: 100 in + up to 900 out. Whatever the
    // interleaving, what was reserved never exceeds the cap, and settling
    // every call at its reserved worst case lands at or under it.
    let (a, b) = (replica().await, replica().await);
    let key = format!("{}:tok", ns());
    let tasks: Vec<_> = (0..40)
        .map(|i| {
            let us = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            tokio::spawn(async move {
                let id = format!("{key}-{i}");
                let adm = Admission {
                    hold_id: Some(id.clone()),
                    dims: vec![Dim::counter(
                        key.clone(),
                        Some(5_000),
                        3600,
                        Measure::TotalTokens,
                    )],
                    shape: Shape {
                        min_out: 50,
                        ..tokens(100, 900)
                    },
                };
                match us.admit(&adm).await.unwrap() {
                    Verdict::Admitted { out_tokens } => {
                        // The call uses everything it was allowed.
                        us.finish_hold(
                            &id,
                            Finish::Settle(Actual {
                                in_tokens: 100,
                                out_tokens,
                                cost_micros: 0,
                            }),
                            &[],
                        )
                        .await
                        .unwrap();
                        100 + out_tokens
                    }
                    Verdict::Refused { .. } => 0,
                }
            })
        })
        .collect();
    let mut used = 0;
    for t in tasks {
        used += t.await.unwrap();
    }
    let counted = a.counter_value(&key).await.unwrap();
    assert_eq!(counted, used, "the counter is exactly what was used");
    assert!(counted <= 5_000, "a cap of 5000 reached {counted}");
    assert!(
        counted > 4_900,
        "and the squeeze used what was left: {counted}"
    );
}

#[tokio::test]
async fn a_dead_replicas_slot_is_freed_by_another_replicas_reaper() {
    let (dead, alive) = (replica().await, replica().await);
    let slot = format!("{}:slot", ns());
    let adm = |id: &str| Admission {
        hold_id: Some(id.to_string()),
        dims: vec![
            Dim::slot(slot.clone(), 1),
            Dim::counter(format!("{slot}:spend"), None, 3600, Measure::TotalTokens),
        ],
        shape: tokens(10, 90),
    };
    let held = format!("{slot}-dead");
    assert!(matches!(
        dead.admit(&adm(&held)).await.unwrap(),
        Verdict::Admitted { .. }
    ));
    assert!(matches!(
        alive.admit(&adm(&format!("{slot}-blocked"))).await.unwrap(),
        Verdict::Refused { dim: 0, .. }
    ));

    // The holder dies: its lease is never renewed. Age it out.
    let mut c = conn().await;
    let past = crate::common::db_now().timestamp_millis() - 1;
    let _: () = redis::cmd("ZADD")
        .arg("brutor:holds")
        .arg(past)
        .arg(&held)
        .query_async(&mut c)
        .await
        .unwrap();

    assert!(alive.reap_expired_holds().await.unwrap() >= 1);
    // Its spend is kept (abandoned = spent) and its slot is free.
    assert_eq!(
        alive.counter_value(&format!("{slot}:spend")).await.unwrap(),
        100
    );
    assert!(matches!(
        alive.admit(&adm(&format!("{slot}-next"))).await.unwrap(),
        Verdict::Admitted { .. }
    ));
}

#[tokio::test]
async fn a_live_replica_keeps_its_lease_by_renewing() {
    let us = replica().await;
    let slot = format!("{}:renew", ns());
    let id = format!("{slot}-h");
    let adm = Admission {
        hold_id: Some(id.clone()),
        dims: vec![Dim::slot(slot.clone(), 1)],
        shape: Shape::count(),
    };
    us.admit(&adm).await.unwrap();
    let _g = us.adopt_hold(brutor_gateway_core::services::limits::Held::for_test(&id));
    // Push the lease to the edge, then renew: the reaper must not take it.
    let mut c = conn().await;
    let soon = crate::common::db_now().timestamp_millis() + 50;
    let _: () = redis::cmd("ZADD")
        .arg("brutor:holds")
        .arg(soon)
        .arg(&id)
        .query_async(&mut c)
        .await
        .unwrap();
    assert!(us.renew_live_holds().await.unwrap() >= 1);
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    us.reap_expired_holds().await.unwrap();
    let again = Admission {
        hold_id: Some(format!("{slot}-other")),
        ..adm.clone()
    };
    assert!(
        matches!(us.admit(&again).await.unwrap(), Verdict::Refused { .. }),
        "the renewed hold still owns the slot"
    );
}

#[tokio::test]
async fn a_counter_the_script_cannot_use_refuses() {
    let us = replica().await;
    let key = format!("{}:broken", ns());
    // Fault injection: the counter key holds the wrong type.
    let mut c = conn().await;
    let _: () = redis::cmd("HSET")
        .arg(&key)
        .arg("x")
        .arg("1")
        .query_async(&mut c)
        .await
        .unwrap();
    let adm = Admission {
        hold_id: None,
        dims: vec![count(&key, 100)],
        shape: Shape::count(),
    };
    assert!(
        us.admit(&adm).await.is_err(),
        "an unusable counter is an error, never an admit"
    );
}

#[tokio::test]
async fn a_settle_lands_in_the_window_the_call_was_admitted_in() {
    // The hold records full keys (window index included), so a call
    // admitted on day N and settled on day N+1 corrects day N.
    let us = replica().await;
    let day_n = format!("{}:tok:d:20000", ns());
    let id = format!("{day_n}-h");
    let adm = Admission {
        hold_id: Some(id.clone()),
        dims: vec![Dim::counter(
            day_n.clone(),
            Some(10_000),
            3600,
            Measure::TotalTokens,
        )],
        shape: tokens(100, 900),
    };
    us.admit(&adm).await.unwrap();
    us.finish_hold(
        &id,
        Finish::Settle(Actual {
            in_tokens: 100,
            out_tokens: 20,
            cost_micros: 0,
        }),
        &[],
    )
    .await
    .unwrap();
    assert_eq!(us.counter_value(&day_n).await.unwrap(), 120);
}

// ---------------------------------------------------------------------------
// A set of distinct members with a maximum size (`DimKind::Members`)
// ---------------------------------------------------------------------------

fn member(key: &str, member: &str, max: i64) -> Admission {
    Admission {
        hold_id: None,
        dims: vec![Dim::members(key.to_string(), member.to_string(), max, 3600)],
        shape: Shape::count(),
    }
}

fn is_admitted(v: &Verdict) -> bool {
    matches!(v, Verdict::Admitted { .. })
}

async fn scard(key: &str) -> i64 {
    let mut c = conn().await;
    redis::cmd("SCARD")
        .arg(key)
        .query_async(&mut c)
        .await
        .unwrap()
}

/// Sixty admissions of sixty DIFFERENT members arrive together on two
/// replicas. "Is this member new" is decided by the script that adds it, so
/// exactly the maximum get a place: none is admitted on a size another
/// request was about to change.
#[tokio::test]
async fn distinct_members_across_two_replicas_never_exceed_the_maximum() {
    let (a, b) = (replica().await, replica().await);
    let key = format!("{}:runs", ns());
    let tasks: Vec<_> = (0..60)
        .map(|i| {
            let us = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            tokio::spawn(async move {
                is_admitted(
                    &us.admit(&member(&key, &format!("run-{i}"), 9))
                        .await
                        .unwrap(),
                )
            })
        })
        .collect();
    let mut admitted = 0;
    for t in tasks {
        if t.await.unwrap() {
            admitted += 1;
        }
    }
    assert_eq!(
        admitted, 9,
        "a set of at most 9 members admitted {admitted}"
    );
    assert_eq!(scard(&key).await, 9);
}

/// The same member, sixty times at once on two replicas, takes ONE place and
/// every admission passes; the set then refuses a second member.
#[tokio::test]
async fn one_member_arriving_many_times_at_once_takes_one_place() {
    let (a, b) = (replica().await, replica().await);
    let key = format!("{}:runs", ns());
    let tasks: Vec<_> = (0..60)
        .map(|i| {
            let us = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            tokio::spawn(async move {
                is_admitted(&us.admit(&member(&key, "the-run", 1)).await.unwrap())
            })
        })
        .collect();
    for t in tasks {
        assert!(t.await.unwrap(), "a member of the set was refused");
    }
    assert_eq!(scard(&key).await, 1);
    assert_eq!(
        a.admit(&member(&key, "another-run", 1)).await.unwrap(),
        Verdict::Refused {
            dim: 0,
            current: 1.0,
            retry_after_ms: 0
        }
    );
    assert_eq!(scard(&key).await, 1, "a refused member was added");
}

/// A member is added with the admission's other dimensions or not at all,
/// and a full set leaves the counter beside it uncharged.
#[tokio::test]
async fn a_member_and_a_counter_are_admitted_together_or_not_at_all() {
    let us = replica().await;
    let ns = ns();
    let (set, full, beside) = (
        format!("{ns}:runs"),
        format!("{ns}:full"),
        format!("{ns}:beside"),
    );
    let both = |a: Dim, b: Dim| Admission {
        hold_id: None,
        dims: vec![a, b],
        shape: Shape::count(),
    };
    assert!(is_admitted(
        &us.admit(&Admission {
            hold_id: None,
            dims: vec![count(&full, 1)],
            shape: Shape::count(),
        })
        .await
        .unwrap()
    ));
    for adm in [
        both(
            Dim::members(set.clone(), "r1".into(), 5, 3600),
            count(&full, 1),
        ),
        both(
            count(&full, 1),
            Dim::members(set.clone(), "r1".into(), 5, 3600),
        ),
    ] {
        assert!(!is_admitted(&us.admit(&adm).await.unwrap()));
    }
    assert_eq!(scard(&set).await, 0, "a refused admission added a member");

    assert!(is_admitted(
        &us.admit(&member(&set, "r1", 1)).await.unwrap()
    ));
    let v = us
        .admit(&both(
            count(&beside, 10),
            Dim::members(set.clone(), "r2".into(), 1, 3600),
        ))
        .await
        .unwrap();
    assert_eq!(
        v,
        Verdict::Refused {
            dim: 1,
            current: 1.0,
            retry_after_ms: 0
        }
    );
    assert_eq!(us.counter_value(&beside).await.unwrap(), 0);
}

/// Membership is final (ending the hold it was admitted under leaves it) and
/// the set lapses with its window: it carries the dimension's TTL.
#[tokio::test]
async fn membership_outlives_its_hold_and_lapses_with_its_window() {
    let us = replica().await;
    let ns = ns();
    let (set, counter) = (format!("{ns}:runs"), format!("{ns}:c"));
    for (i, how) in [Finish::Release, Finish::Abandon, Finish::Commit]
        .into_iter()
        .enumerate()
    {
        let set = format!("{set}-{i}");
        let hold = format!("{ns}-h{i}");
        let adm = Admission {
            hold_id: Some(hold.clone()),
            dims: vec![
                Dim::members(set.clone(), "r1".into(), 1, 120),
                count(&format!("{counter}-{i}"), 10),
            ],
            shape: Shape::count(),
        };
        assert!(is_admitted(&us.admit(&adm).await.unwrap()));
        us.finish_hold(&hold, how, &[]).await.unwrap();
        assert_eq!(scard(&set).await, 1, "{how:?} took the member back");
        assert!(!is_admitted(
            &us.admit(&member(&set, "r2", 1)).await.unwrap()
        ));
        let mut c = conn().await;
        let ttl: i64 = redis::cmd("TTL")
            .arg(&set)
            .query_async(&mut c)
            .await
            .unwrap();
        assert!((1..=120).contains(&ttl), "the set's TTL is {ttl}");
    }
    // A set whose window is over is empty again.
    let short = format!("{set}-short");
    let adm = |m: &str| Admission {
        hold_id: None,
        dims: vec![Dim::members(short.clone(), m.to_string(), 1, 1)],
        shape: Shape::count(),
    };
    assert!(is_admitted(&us.admit(&adm("r1")).await.unwrap()));
    assert!(!is_admitted(&us.admit(&adm("r2")).await.unwrap()));
    tokio::time::sleep(std::time::Duration::from_millis(1_300)).await;
    assert!(is_admitted(&us.admit(&adm("r2")).await.unwrap()));
    assert_eq!(scard(&short).await, 1);
}

/// The twin and the script agree on a long random sequence of members and
/// maxima, mixed with a counter that sometimes refuses.
#[tokio::test]
async fn the_twin_and_the_script_agree_on_member_sets() {
    let redis = replica().await;
    let mem = Arc::new(UsageState::new(None));
    let ns = ns();
    let mut rng = Lcg(0xfeed);
    for step in 0..300 {
        let set = format!("{ns}:set{}", rng.next(3));
        let m = format!("run-{}", rng.next(12));
        let max = 1 + rng.next(6) as i64;
        let mut dims = vec![Dim::members(set.clone(), m, max, 3600)];
        if rng.next(3) == 0 {
            dims.push(count(
                &format!("{ns}:c{}", rng.next(2)),
                1 + rng.next(30) as i64,
            ));
        }
        if rng.next(2) == 0 {
            dims.reverse();
        }
        let adm = Admission {
            hold_id: None,
            dims,
            shape: Shape::count(),
        };
        let a = redis.admit(&adm).await.expect("redis admit");
        let b = mem.admit(&adm).await.expect("mem admit");
        assert_eq!(a, b, "step {step}: {adm:?}");
    }
}

/// A set the script cannot operate on refuses (fail closed).
#[tokio::test]
async fn a_member_set_that_is_not_a_set_fails_closed() {
    let us = replica().await;
    let key = format!("{}:runs", ns());
    let mut c = conn().await;
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg("not a set")
        .arg("EX")
        .arg(60)
        .query_async(&mut c)
        .await
        .unwrap();
    assert!(us.admit(&member(&key, "r1", 5)).await.is_err());
}
