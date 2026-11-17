# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a187_agent_implementation — migration drift suite

Fresh installs build every table from the ORM (`create_all` + stamp);
upgrades apply a187_agent_implementation. Most of these columns are WRITTEN BY THE RUST CORE
(`proxy_logs.agent_impl_*` / `client_label`, the run and segment release,
`agent_implementation_releases` upserted by the run finaliser) — a column,
default or CHECK present in one deployment shape and not the other fails in
the core's INSERT, not at deploy time. So every added column, the new table,
every CHECK and every index is compared with the ORM here, parsed with
`ast` (formatting cannot hide a column from the scan).
"""

from __future__ import annotations



import app.database.models  # noqa: F401 — registers every model


#: (table, column) → the migration's contract for it.


def test_to_dicts_expose_the_new_fields():
    from datetime import datetime, timezone

    from app.database.models.agent_identity import AgentIdentity
    from app.database.models.agent_implementation_release import AgentImplementationRelease
    from app.database.models.ai_system_lifecycle_event import AISystemLifecycleEvent
    from app.database.models.ai_system_run import AISystemRun
    from app.database.models.ai_system_run_segment import AISystemRunSegment
    from app.database.models.proxy_log import ProxyLog

    impl = {"agent_impl_name": "n", "agent_impl_version": "1", "agent_impl_build": "b"}
    assert {**impl, "agent_impl_source": "header", "client_label": "portal"}.items() <= ProxyLog(
        **impl, agent_impl_source="header", client_label="portal").to_dict().items()
    run = AISystemRun(**impl, agent_impl_trust="declared", implementation_fingerprint="sha256:x")
    assert run.to_dict()["agent_impl_trust"] == "declared"
    assert run.to_dict()["implementation_fingerprint"] == "sha256:x"
    seg = AISystemRunSegment(**impl, agent_impl_trust="verified").to_dict()
    assert seg["agent_impl_trust"] == "verified" and seg["agent_impl_build"] == "b"
    a = AgentIdentity(implementation_name="n", approved_versions=["1.*"], require_release=True)
    assert (a.to_dict()["implementation_name"], a.to_dict()["approved_versions"],
            a.to_dict()["require_release"]) == ("n", ["1.*"], True)
    now = datetime.now(timezone.utc)
    rel = AgentImplementationRelease(id="rel-1", tenant_id="t", agent_id="a", name="n",
                                     version="1", build="", trust="declared", first_seen_at=now,
                                     last_seen_at=now, run_count=3, fingerprints=["x", "y"])
    d = rel.to_dict()
    assert d["build"] is None and d["fingerprint_count"] == 2 and d["run_count"] == 3
    ev = AISystemLifecycleEvent(event_type="agent.new_release", subject_id="rel-1",
                                details={"version": "1"}).to_dict()
    assert (ev["event_type"], ev["subject_id"], ev["details"]) == (
        "agent.new_release", "rel-1", {"version": "1"})
