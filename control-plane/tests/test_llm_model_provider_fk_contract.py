# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression suite for the LLModel.provider_id FK contract.

Background — the bug this exists to lock in:
    `LLModel.provider` was a free-form string column; migration `a027_llm_providers_table` promoted it
    to a real entity (`llm_providers` row) with a `provider_id`
    FK and a `provider` SQLAlchemy *relationship* attribute. After
    that change, code that still passed `LLModel(provider="…")`
    (a bare string) tried to assign a `str` to the relationship
    slot and crashed at flush time with the cryptic

        AttributeError: 'str' object has no attribute
                        '_sa_instance_state'

    surfacing as a 500 in three places before this suite landed:
      * `LLMRouterService.create_routing_group` (admin UI: a 500
        every time an operator hit "Save" on Add Routing Group)
      * `kserve_status_poller._poll_machine`
      * `llm_models_runner.deploy_local_model`

Two complementary layers of protection here:

  1. **Static AST scan** of every `app/**/*.py` file. Fails if any
     `LLModel(...)` call passes `provider=` as a kwarg — the
     legacy column is gone, so any such call is by definition
     wrong. Cheap, runs in milliseconds, catches the bug shape
     even on code paths not exercised by other tests.

  2. **Runtime contract assertions** against a real SQLAlchemy
     session. Confirms that `LLModel(provider="…")` raises and
     `LLModel(provider_id=<id>)` succeeds and round-trips. Locks
     in the SQLAlchemy-level invariant — if anyone ever
     "softens" the relationship to accept strings (don't), the
     runtime test will break and force them to acknowledge the
     change explicitly.

Run: pytest tests/test_llm_model_provider_fk_contract.py -v
"""

import ast
import os
import uuid
from pathlib import Path
from typing import Iterator

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.tenant import Tenant
from tests.source_trees import python_sources


# ────────────────────────────────────────────────────────────────────
# Layer 1 — static AST scan
# ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"


def _python_files_under(root: Path) -> Iterator[Path]:
    """Yield every `*.py` file under `root`, skipping the tests
    tree (the tests themselves intentionally exercise the bad
    pattern in a few negative-assertion fixtures) and any
    cache / vendor directories."""
    skip_parts = {"__pycache__", ".venv", "venv", "alembic", ".pytest_cache"}
    for path in python_sources(root):
        if any(part in skip_parts for part in path.parts):
            continue
        yield path


def _llmodel_call_uses_provider_kwarg(node: ast.AST) -> bool:
    """True iff `node` is a `Call` to something named `LLModel`
    (either `LLModel(...)` or `something.LLModel(...)`) that
    passes `provider=` as a kwarg.

    We don't try to evaluate the argument value — even
    `provider=some_var` is wrong (the relationship expects an
    `LlmProvider` instance, not a string variable; if the value
    really is an instance, the codebase convention is to set
    `provider_id` and let SQLAlchemy resolve the relationship
    on read, which is uniformly the pattern everywhere else).
    """
    if not isinstance(node, ast.Call):
        return False

    # Match both `LLModel(...)` and `module.LLModel(...)`. We
    # accept any plausible spelling because import aliases are
    # uncommon in this codebase but not impossible.
    func = node.func
    name: str | None = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name != "LLModel":
        return False

    return any(kw.arg == "provider" for kw in node.keywords)


def test_no_llmodel_constructor_passes_provider_kwarg():
    """No source file under `app/` should construct `LLModel(...)`
    with a `provider=` kwarg. The legacy column is gone — anyone
    passing `provider=…` is reaching for the relationship slot
    with a string and will crash at flush time. The fix is to
    pass `provider_id=…` (typically resolved via
    `upsert_provider_for_key`).
    """
    offenders: list[tuple[Path, int, str]] = []

    for py_file in _python_files_under(APP_ROOT):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            # An unparseable file is a bigger problem than this
            # check — leave it for the broader test suite to
            # surface and skip here.
            continue

        for node in ast.walk(tree):
            if _llmodel_call_uses_provider_kwarg(node):
                offenders.append(
                    (
                        py_file.relative_to(REPO_ROOT),
                        node.lineno,
                        ast.unparse(node).split("\n")[0][:120],
                    )
                )

    assert not offenders, (
        "Found `LLModel(...)` calls passing `provider=` (legacy column, "
        "now a SQLAlchemy relationship — assigning a string raises "
        "`'str' object has no attribute '_sa_instance_state'` at flush "
        "time). Use `provider_id=<llm_providers.id>` instead, resolved "
        "via `app.database.init_llm_providers.upsert_provider_for_key` "
        "if you don't already hold an LlmProvider row.\n\nOffenders:\n"
        + "\n".join(f"  {p}:{ln}\n    {snippet}" for p, ln, snippet in offenders)
    )


# ────────────────────────────────────────────────────────────────────
# Layer 2 — runtime contract assertions
# ────────────────────────────────────────────────────────────────────

TENANT_ID = "t-provider-fk"

REQUIRED_TABLES = [
    Tenant.__table__,
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LLModel.__table__,
]


@pytest.fixture()
def db() -> Session:
    """Minimal in-memory DB with the three tables this contract
    touches. Smaller than `test_llm_access_service.py`'s fixture
    because we don't need resource groups or memberships here."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        s.add(
            Tenant(
                id=TENANT_ID,
                name=TENANT_ID,
                display_name="Provider FK Test",
                organization_name="x",
                primary_contact_email="x@x",
                is_active=True,
                is_default=False,
                is_trial=False,
                suspended=False,
                billing_plan="self_hosted",
            )
        )
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _mk_provider(db: Session, key: str = "openai") -> LlmProvider:
    p = LlmProvider(
        id=f"prov-{uuid.uuid4().hex[:10]}",
        tenant_id=TENANT_ID,
        key=key,
        display_name=key.title(),
        enabled=True,
        is_system=True,
    )
    db.add(p)
    db.flush()
    return p


def test_llmodel_provider_string_assignment_raises(db):
    """Assigning a string to `LLModel.provider` (the relationship)
    must raise an AttributeError. If this test starts failing,
    SQLAlchemy's behaviour or the ORM mapping has changed — read
    the file header and decide whether the change is intentional
    before relaxing this assertion.
    """
    with pytest.raises(AttributeError, match="_sa_instance_state"):
        # Pass `provider="openai"` — exactly the bug the three
        # production sites were carrying. The constructor itself
        # accepts kwargs verbatim and assigns them; the relationship
        # rejects the string when SQLAlchemy tries to read its ORM
        # state, which is what produces the cryptic error.
        LLModel(
            id="llm-bug-bait",
            tenant_id=TENANT_ID,
            name="Bug bait",
            provider="openai",
            model_name="gpt-4o",
            mode="chat",
        )


def test_llmodel_provider_id_assignment_succeeds(db):
    """The canonical pattern: pass `provider_id=<llm_providers.id>`,
    flush, and read the model back through the relationship. This
    is the success path the three production fixes adopt.
    """
    provider = _mk_provider(db, key="openai")

    model = LLModel(
        id="llm-fk-happy-path",
        tenant_id=TENANT_ID,
        name="Happy path",
        provider_id=provider.id,
        model_name="gpt-4o",
        mode="chat",
        enabled=True,
        api_url_auto_discovered=True,
    )
    db.add(model)
    db.flush()
    db.refresh(model)

    # Round-trip: the joined-row read returns the LlmProvider
    # instance, not a string — confirming the relationship is
    # wired correctly and the FK resolves.
    assert model.provider is not None
    assert model.provider.id == provider.id
    assert model.provider.key == "openai"


def test_upsert_provider_for_key_idempotent_per_tenant(db):
    """The router-service / KServe / runner fixes all rely on
    `upsert_provider_for_key` being idempotent — calling it twice
    in a row for the same `(tenant_id, key)` pair must return the
    same row, not insert a duplicate. The DB has a UNIQUE on
    `(tenant_id, key)` so a non-idempotent helper would crash on
    the second call.
    """
    from app.database.init_llm_providers import upsert_provider_for_key

    first = upsert_provider_for_key(
        db, TENANT_ID, key="routing_group",
        display_name="Routing Group", category="virtual", is_system=True,
    )
    db.flush()

    second = upsert_provider_for_key(
        db, TENANT_ID, key="routing_group",
        display_name="Routing Group", category="virtual", is_system=True,
    )
    db.flush()

    assert first.id == second.id, (
        "upsert_provider_for_key must return the existing row on the "
        "second call. The router-service create_routing_group flow "
        "relies on this — running it twice in the same session (or "
        "across two routing-group creates that share a tenant) would "
        "otherwise hit a UNIQUE-constraint violation on (tenant_id, "
        "key) and surface a confusing 500."
    )
