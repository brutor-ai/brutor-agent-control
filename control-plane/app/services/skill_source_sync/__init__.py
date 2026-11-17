# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Skill source sync — in-process control-plane scheduler that walks
SkillSource rows (typically pointing at GitHub repos), discovers
SKILL.md files + sibling resources/scripts/assets, and reconciles
them against `agent_skills` via the existing `_ingest_discovered_skills`
helper.

# Why this lives in the control plane, not the proxy

The Rust proxy is a read-only runtime: it forwards requests, enforces
guardrails, logs telemetry, and reads config. **Writing config rows
into the DB is the control plane's job.** Skill source sync is pure
config import — it creates/updates `agent_skills`, `agent_skill_scripts`,
`agent_skill_resources`, `agent_skill_assets`. A previous
implementation lived in `brutor-gateway-core/src/services/skill_source_sync.rs`
but kept hitting ORM/schema impedance mismatches (enum casts, NOT NULL
defaults, varchar overruns) because it had to re-derive every column
constraint by hand. Moving here lets us reuse the SQLAlchemy ORM that
already owns the schema knowledge.

# Public API

`start_skill_source_sync_scheduler()` — call once at FastAPI lifespan
startup; spawns the async loop.

`skill_source_sync_loop(tick_interval_seconds)` — the loop itself,
exposed for tests that want to drive a single tick.

Internal helpers live in `scheduler.py`; the GitHub walker is
`github_walker.py`; the SKILL.md parser is `frontmatter.py`.
"""

from app.services.skill_source_sync.scheduler import (
    skill_source_sync_loop,
    start_skill_source_sync_scheduler,
)

__all__ = [
    "skill_source_sync_loop",
    "start_skill_source_sync_scheduler",
]
