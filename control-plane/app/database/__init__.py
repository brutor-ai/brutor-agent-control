# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Database configuration and initialization - Multi-Tenant Version
PostgreSQL-optimized with environment-driven configuration
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from starlette.requests import Request
from typing import Generator
import os
import uuid
from datetime import datetime, timezone
from ulid import ULID

from app.database.database_config import db_config, get_database_info
from app.constants.tenant import TenantDefaultTheme, TenantDefaultRole
from app.core.logging_system import BrutorLogger
from app.env_vars import BRUTOR_GATEWAY_SERVICE_API_KEY, BRUTOR_VERSION

logger = BrutorLogger("database")

# =============================================================================
# Database Configuration - Loaded from Environment Variables
# =============================================================================

db_info = get_database_info(db_config)

# Create the database engine with configuration from the environment
engine = create_engine(db_config.url, **db_config.get_engine_config())

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db(request: Request = None) -> Generator[Session, None, None]:  # type: ignore[assignment]
    """Database session with automatic transaction management,
       used by FastAPI dependency injection in endpoints.

    FastAPI injects `request`; the session is bound to it so that
    `CommitBeforeResponseMiddleware` commits its writes before the response
    starts (the teardown commit below runs only after the response is sent
    in this FastAPI, so it is now the backstop) and publishes proxy-cache
    invalidations only after that commit (`app/services/cache_invalidation.py`).
    Direct callers (middleware) pass nothing and get an unbound session."""
    from app.services.cache_invalidation import bind_request_session

    db = SessionLocal()
    bind_request_session(request, db)
    try:
        yield db
        db.commit()     # ← Auto-commit after successful request
    except Exception:
        db.rollback()   # ← Auto-rollback on error
        raise
    finally:
        db.close()      # Always close the session


def _init_sentinel_path() -> str:
    """
    Per-master-PID marker file used to skip duplicate init in sibling
    Gunicorn workers. Keying on the master's PID (= each worker's
    `os.getppid()`) means:

      * Sibling workers under the same master see the same path → the
        second / third / Nth worker finds the marker after the first
        completes and exits early.
      * A container restart starts a fresh master with a new PID, so
        the next first-worker still runs full init (apply pending
        migrations + run idempotent seeds).

    Lives under `/tmp` so it shares the worker filesystem and disappears
    on container restart. Falls back to `tempfile.gettempdir()` for
    portability (CI, local dev on macOS).
    """
    import os
    import tempfile

    return os.path.join(tempfile.gettempdir(), f"brutor-init-{os.getppid()}.done")


def init_database(enterprise_features=None):
    """
    Initialize database — apply Alembic migrations and seed initial data.

    Between the two sits the edition check
    (`app/services/edition/guard.py`): once the schema is current and before
    anything else is written, an Enterprise composition writes the edition
    marker, and a Community composition refuses to go on if the Enterprise
    Edition has used this database. `enterprise_features` is what the
    application was composed of; None reads the process-wide composition.
    The refusal is raised out of here like a failed migration, so the
    process does not serve.

    The schema is Alembic's alone: an empty database gets the baseline
    revision and every later one (`alembic upgrade head`); a database with
    an `alembic_version` table gets the revisions it lacks; a database with
    tables but no `alembic_version` is refused.

    Uses a PostgreSQL advisory lock so multiple workers starting
    simultaneously don't race on schema initialization. The unlock is
    best-effort (Aurora Serverless may drop the connection during long
    operations; advisory locks auto-release on connection close anyway).

    Sibling workers under the same Gunicorn master skip the
    migrate-and-seed pass entirely via a PID-keyed sentinel — the
    advisory lock alone was enough to prevent races, but every worker
    was still re-running `alembic upgrade head`, the full seed pass,
    and the secondary scheduler boot-up. With ≥80 catalog entries
    seeded per worker pass on a fresh boot, that's measurable wasted
    work. The marker is best-effort: if it can't be created, init
    still completes successfully and the next worker just re-runs
    everything (idempotent anyway).
    """
    import os
    from sqlalchemy import text, inspect

    try:
        # Import all models to ensure they're registered
        from app.database import models  # noqa

        # Pre-warm the pool BEFORE we grab the advisory lock or run any
        # Alembic command. Running prewarm later (after the outer
        # connection has already been checked out and Alembic has done
        # its own connection dance) previously produced a race with
        # Gunicorn's pre-fork: workers 1 and 2 would both enter
        # init_database(), serialise on pg_advisory_lock(8742), and
        # whichever one went second sometimes saw a pool connection in
        # a half-initialised state and crashed the SELECT 1 with
        # `PGRES_TUPLES_OK and no message from the libpq`. Warming
        # first means every downstream step (lock, inspect, Alembic,
        # seed) runs on a pool that's already known-good.
        _prewarm_connection()

        sentinel = _init_sentinel_path()

        with engine.connect() as conn:
            # Every pooled connection carries `-c statement_timeout`
            # (database_config.py — default 60s, request-oriented). The
            # init connection is the wrong place for it: the very next
            # call is a BLOCKING `pg_advisory_lock(8742)` that legitimately
            # waits for however long a sibling worker takes to bootstrap a
            # fresh DB (migrate + seed). On a cold boot that
            # sibling pass can exceed the timeout, and the lock WAIT then
            # gets canceled ("canceling statement due to statement
            # timeout"), crashing startup on the second worker. Disable the
            # timeout for this connection's init scope; reset in `finally`
            # before it returns to the pool. Postgres-only (the SQLite test
            # engine has no statement_timeout and rejects the SET).
            is_pg = conn.dialect.name == "postgresql"
            if is_pg:
                conn.execute(text("SET statement_timeout = 0"))
            conn.execute(text("SELECT pg_advisory_lock(8742)"))
            try:
                # Sibling-worker fast-path. The sentinel is checked
                # AFTER taking the lock so the first worker has time
                # to win the lock + finish init + touch the marker
                # before the second worker observes it.
                if os.path.exists(sentinel):
                    logger.info(
                        "Database already initialised by sibling worker — "
                        "skipping migrate + seed pass",
                        context={
                            "sentinel": sentinel,
                            "master_pid": os.getppid(),
                            "worker_pid": os.getpid(),
                        },
                    )
                    return
                inspector = inspect(engine)
                existing_tables = set(inspector.get_table_names(schema="public"))
                if "alembic_version" in existing_tables:
                    logger.info("Running Alembic migrations (upgrade head)")
                elif not existing_tables:
                    # A new installation: the baseline revision creates the
                    # schema, then any later revision applies.
                    logger.info("Fresh database — creating the schema (alembic upgrade head)")
                else:
                    # Tables without Alembic's record: not a database this
                    # release made. Never adopted, never completed.
                    raise RuntimeError(
                        "The database has %d tables but no alembic_version table: it was not "
                        "created by this release. Use an empty database." % len(existing_tables)
                    )
                _alembic_upgrade_head()

                # The edition check. After the schema (one
                # schema serves both editions, and the check reads tables the
                # migrations create), before the first write of this start.
                from app.services.edition.guard import enforce_edition_at_start

                enforce_edition_at_start(engine, enterprise_features)

                _update_schema_version()
                seed_initial_data()
                # Platform measurement changes: after
                # the schema is current, inside the lock, once per entry per
                # installation. Never raises; a failure retries next start.
                from app.services.measurement_changes import apply_pending_at_startup

                apply_pending_at_startup()

                # Mark the init as completed for this Gunicorn master so
                # sibling workers skip the duplicate pass. Best-effort:
                # a failed touch just makes the next worker re-run the
                # idempotent init, which is the pre-fix status quo.
                try:
                    with open(sentinel, "w") as fh:
                        fh.write(str(os.getpid()))
                except OSError as marker_err:
                    logger.warning(
                        "Could not write init sentinel — sibling workers will "
                        "re-run idempotent init",
                        context={"sentinel": sentinel, "error": str(marker_err)},
                    )
            finally:
                # Release the advisory lock. Best-effort: Aurora Serverless v2 may
                # drop the connection during scaling, killing this lock with it.
                # Advisory locks auto-release when the connection terminates.
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(8742)"))
                    conn.commit()
                except Exception as unlock_err:
                    logger.warning(
                        "Advisory unlock failed (connection likely dropped); "
                        "lock auto-releases on connection close",
                        context={"error": str(unlock_err)},
                    )
                # Restore the request-oriented timeout before this
                # connection returns to the pool — pool reset is rollback,
                # which does not clear a session SET.
                if is_pg:
                    try:
                        conn.execute(text("SET statement_timeout = DEFAULT"))
                        conn.commit()
                    except Exception:
                        pass

    except Exception as e:
        logger.error("Database initialization failed", context={
            "error": str(e)
        }, exc_info=True)
        raise


def _alembic_config():
    """Build an Alembic Config pointing at our alembic/ directory with current DATABASE_URL."""
    from alembic.config import Config

    # alembic.ini lives at the project root (parent of app/)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = Config(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))
    # Alembic stores config in a configparser, which interprets
    # `%` as interpolation syntax. A `%` anywhere in the URL
    # (most commonly in an auto-generated DB password) makes
    # `set_main_option` raise InterpolationSyntaxError before
    # any DB work happens. Doubling literal `%` to `%%` is the
    # documented configparser escape and round-trips cleanly
    # through SQLAlchemy when Alembic reads the value back.
    cfg.set_main_option("sqlalchemy.url", db_config.url.replace("%", "%%"))
    return cfg


def _has_migration_files() -> bool:
    """Check if any Alembic revision files exist in alembic/versions/."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    versions_dir = os.path.join(project_root, "alembic", "versions")
    if not os.path.isdir(versions_dir):
        return False
    for fn in os.listdir(versions_dir):
        if fn.endswith(".py") and not fn.startswith("_"):
            return True
    return False


def _alembic_upgrade_head():
    """Run `alembic upgrade head` against the live DB. No-op if no migrations exist."""
    if not _has_migration_files():
        logger.info("No Alembic migrations found — skipping upgrade")
        return
    from alembic import command
    command.upgrade(_alembic_config(), "head")


def _prewarm_connection(max_attempts: int = 8, base_delay: float = 0.5) -> None:
    """
    Warm up the DB connection pool before heavy init workload.

    Two classes of transient failure to absorb here:

    * **Aurora Serverless v2 cold start.** Scales from low ACUs lazily —
      the first query can trigger a scale-up that drops SSL mid-flight.
      Surfaces as `OperationalError`.
    * **Pre-fork pool race.** Gunicorn forks N workers which all hit
      `init_database()` in parallel. Even with an advisory lock
      serialising the body, the shared engine's pool can hand back a
      connection left in a half-initialised state by the previous
      worker (concretely: psycopg2's
      `error with status PGRES_TUPLES_OK and no message from the libpq`
      on a trivial `SELECT 1`, correlating with postgres's
      `WARNING: there is already a transaction in progress`). Surfaces
      as `DBAPIError` / `DatabaseError` — NOT `OperationalError`.

    We catch both, `engine.dispose()` between retries so the next attempt
    gets a genuinely fresh connection, and back off exponentially. If
    the pool is still unhealthy after `max_attempts`, we log and proceed
    — downstream code will re-raise a clearer error on its own query.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, OperationalError
    import time

    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1")).scalar()
                conn.commit()
            if attempt > 1:
                logger.info(f"Database connection ready after {attempt} attempts")
            return
        except (OperationalError, DBAPIError) as e:
            # Purge the pool — any lingering bad connections from a
            # worker race or scale-up are gone. `close=False` leaves
            # currently-checked-out connections alone (there shouldn't
            # be any here since prewarm runs before anything else).
            try:
                engine.dispose(close=False)
            except Exception:
                pass

            if attempt == max_attempts:
                logger.warning(
                    "Database pre-warm failed after max attempts; proceeding anyway",
                    context={"error": str(e), "attempts": attempt},
                )
                return
            logger.info(
                f"Database pre-warm attempt {attempt} failed "
                f"(scaling or pool-state race); retrying in {delay:.1f}s",
                context={"error": str(e)},
            )
            time.sleep(delay)
            delay = min(delay * 2, 8.0)


def _update_schema_version():
    """Record schema version (set once) and app version (updated every startup)."""
    db = SessionLocal()
    try:
        from app.database.models.schema_version import SchemaVersion

        app_version = BRUTOR_VERSION.require()
        now = datetime.now(timezone.utc)
        existing = db.query(SchemaVersion).filter(SchemaVersion.id == 1).first()

        if existing:
            # Schema version stays frozen — only app version updates
            existing.app_version = app_version
            existing.app_started_at = now
            logger.info(f"Schema created by {existing.schema_version}, "
                        f"now running {app_version}")
        else:
            db.add(SchemaVersion(
                id=1,
                schema_version=app_version,
                app_version=app_version,
                schema_created_at=now,
                app_started_at=now,
            ))
            logger.info(f"Schema created by {app_version}")

        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to record schema version: {e}")
    finally:
        db.close()


def get_or_create_default_tenant(db):
    """Return the platform's default tenant, creating it if absent.

    Always keyed on `is_default`, NEVER on an arbitrary row. The previous
    seeding code keyed on total tenant count and then grabbed
    `db.query(Tenant).first()` — an unordered, random row. On a multi-tenant
    install that could select an operator's *customer* tenant and re-provision
    it on every boot (admin password reset, default resource group re-seeded,
    LLMs re-added). Keying on the default tenant's existence means: if none
    exists (empty DB, or only customer tenants), create the default — never
    hijack someone else's tenant.

    On an existing default tenant, backfills any feature flags newly added to
    `create_default_tenant()` (allow-only: never downgrades a flag the operator
    turned off). Idempotent.
    """
    from app.database.models.tenant import Tenant

    default_tenant = (
        db.query(Tenant).filter(Tenant.is_default == True).first()  # noqa: E712
    )

    if default_tenant is None:
        # Create default tenant using ORM
        default_tenant = Tenant.create_default_tenant()

        # Set default theme/branding (the Brutor Agent Control sky blue theme)
        default_tenant.theme_primary_color = TenantDefaultTheme.PRIMARY_COLOR.value
        default_tenant.theme_secondary_color = TenantDefaultTheme.SECONDARY_COLOR.value
        default_tenant.theme_background_color = TenantDefaultTheme.BACKGROUND_COLOR.value
        default_tenant.theme_surface_color = TenantDefaultTheme.SURFACE_COLOR.value
        default_tenant.theme_text_primary = TenantDefaultTheme.TEXT_PRIMARY_COLOR.value
        default_tenant.theme_text_secondary = TenantDefaultTheme.TEXT_SECONDARY_COLOR.value
        default_tenant.theme_border_color = TenantDefaultTheme.BORDER_COLOR.value
        default_tenant.theme_logo_url = None  # No custom logo for default tenant
        default_tenant.theme_company_name = "Brutor Agent Control"
        default_tenant.theme_favicon_url = None  # No custom favicon for default tenant

        db.add(default_tenant)
        db.commit()
        db.refresh(default_tenant)

        logger.info(f"Default tenant created: {default_tenant.name} ({default_tenant.id})")
    else:
        # Backfill feature flags on the existing default tenant when new
        # features have been added to create_default_tenant() but the DB row
        # was seeded before they existed. Allow-only: never downgrade a flag
        # the operator has turned off.
        fresh_defaults = Tenant.create_default_tenant().features or {}
        current = dict(default_tenant.features or {})
        added_keys = [k for k in fresh_defaults if k not in current]
        if added_keys:
            for k in added_keys:
                current[k] = fresh_defaults[k]
            default_tenant.features = current
            db.add(default_tenant)
            db.commit()
            db.refresh(default_tenant)
            logger.info(
                f"Backfilled default tenant features: {sorted(added_keys)}"
            )

    return default_tenant


def seed_initial_data():
    """Seed initial data, attributed to the platform rather than to nobody.

    Everything written below — here and in every nested `seed_*` call — is
    stamped `created_by = "system"`. The actor is bound once here rather
    than at each call site: the ContextVar propagates through the whole
    call tree, and the alternative is dozens of seeds each having to
    remember, which is how half of them would not.

    `version` needs no equivalent — the column defaults to "1.0", so a
    seeded resource is already at the default rather than NULL.
    """
    from app.database.models.resource_mixins import SYSTEM_ACTOR, acting_as

    with acting_as(SYSTEM_ACTOR):
        _seed_initial_data()


def _seed_initial_data():
    """
    Seed initial data into the database.

    What is seeded, and in what order, is the `INSTALL_SEED` list in
    `app/composition/seeding.py` — the system admin, the default tenant and its
    provisioning, the convenience accounts of a first install, and the
    reference data — plus the steps extensions registered (`SEEDING` in
    `app/composition/extensions.py`). This function owns the session and the
    failure handling; it holds no step of its own.
    """
    from app.composition.extensions import SEEDING
    from app.composition.seeding import SeedContext, run_seed_steps

    db = SessionLocal()

    try:
        run_seed_steps(SeedContext(db=db, logger=logger), SEEDING.steps())

    except Exception as e:
        db.rollback()
        logger.error("Failed to seed initial data", context={
            "error": str(e)
        }, exc_info=True)
        raise
    finally:
        db.close()


def _seed_system_health_check_key(db: Session, tenant_id: str, logger):
    """
    Seed a system-level API key for internal health checks.

    The key is the one configured (`BRUTOR_GATEWAY_SERVICE_API_KEY`), and
    is then never written anywhere but the database, hashed. Where none is
    configured nothing is created and nothing secret is logged: the log
    names the variable, and the key it holds is registered at the next start.
    (A generated key used to be logged in full; one that is not shown is of no
    use, since the control plane presents only the configured key.)

    It is the platform service key: bound to NO resource group and
    flagged `is_platform_service`. The core bills its calls (health checks,
    the assurance judge, knowledge-base embeddings, the KB uploader) to the
    tenant's `Unbound` system group, lets a health probe test a model that
    is in no group yet, and refuses every other key that has no group. Bound
    to Default, a model outside Default could not be health-checked at all.

    Idempotent: an existing key keeps its secret (the deployed one) and is
    converged to unbound + flagged.
    """
    from app.database.models.api_key import APIKey

    existing = db.query(APIKey).filter(
        APIKey.tenant_id == tenant_id,
        APIKey.name == "system-health-check",
    ).first()

    if existing:
        if existing.group_id is not None or not existing.is_platform_service:
            logger.info(
                "System health-check key made the platform service key "
                "(unbound, billed to the Unbound group)",
                context={"tenant_id": tenant_id, "was_group_id": existing.group_id},
            )
            existing.group_id = None
            existing.is_platform_service = True
            db.flush()
        return  # Already seeded

    # The configured key (the DB then matches the deployed secret). None
    # configured: nothing to register, and nothing secret to log.
    from app.constants.proxy import gateway_service_api_key

    env_key = gateway_service_api_key()
    if not env_key:
        logger.warning(
            f"{BRUTOR_GATEWAY_SERVICE_API_KEY.name} is not set: the control plane's calls to the "
            "gateway (model health checks, the assurance judge) are refused until it is. Set it "
            "and restart; the key is registered then.",
            context={"tenant_id": tenant_id},
        )
        return

    import hashlib
    key_hash = hashlib.sha256(env_key.encode()).hexdigest()
    key_prefix = env_key[:20]

    api_key = APIKey(
        id=str(ULID()),
        tenant_id=tenant_id,
        name="system-health-check",
        description=(
            "The key the control plane presents to the gateway on its own calls. "
            f"Set as {BRUTOR_GATEWAY_SERVICE_API_KEY.name}."
        ),
        key_hash=key_hash,
        key_prefix=key_prefix,
        scope="global",
        group_id=None,  # unbound: see the docstring
        is_platform_service=True,
        enabled=True,
        created_by="system",
    )
    db.add(api_key)
    db.flush()

    # The operator's own key: it is in the environment, and nowhere else.
    logger.info(
        "Registered the configured gateway service key",
        context={"tenant_id": tenant_id},
    )


def ensure_unbound_resource_groups(db: Session):
    """Give every tenant its Unbound system group. Runs on every
    startup: provisioning creates it for new tenants, this covers tenants
    provisioned before the group existed. Idempotent."""
    from app.database.models.tenant import Tenant
    from app.services.tenant.tenant_provisioning import TenantProvisioner

    provisioner = TenantProvisioner(logger=logger)
    for tenant in db.query(Tenant).all():
        provisioner._create_unbound_resource_group(db, tenant.id)


def sync_role_permissions(db: Session):
    """
    Ensure all system roles have the latest permissions from code.

    Runs on every startup. Idempotent: inserts missing permissions, and takes
    from the system roles that only read any permission that changes something.

    - Tenant Admin role: gets ALL permissions except SYSTEM_ADMIN
    - Read-Only role: gets the read-only subset defined in TenantProvisioner
    """
    from app.database.models.admin import TenantRole, TenantRolePermission
    from app.database.models.tenant import Tenant
    from app.core.admin_user.permissions import Permission
    from app.services.tenant.tenant_provisioning import TenantProvisioner

    provisioner = TenantProvisioner(logger=logger)
    default_roles = provisioner.get_default_tenant_roles()

    tenants = db.query(Tenant).all()
    total_added = 0

    for tenant in tenants:
        for role_name, role_config in default_roles.items():
            role = db.query(TenantRole).filter(
                TenantRole.tenant_id == tenant.id,
                TenantRole.name == role_name,
                TenantRole.is_system == True,
            ).first()

            if not role:
                continue

            # Get current permissions for this role
            existing_perms = set(rp.permission for rp in role.permissions)
            expected_perms = set(role_config["permissions"])

            # Add missing permissions
            missing = expected_perms - existing_perms
            for perm in missing:
                db.add(TenantRolePermission(
                    id=f"perm-{uuid.uuid4().hex[:12]}",
                    role_id=role.id,
                    permission=perm,
                    created_at=datetime.now(timezone.utc),
                ))
                total_added += 1

    # A system role that only reads holds no registered permission that
    # changes something: one it was granted by an earlier default is taken
    # from it.
    from app.composition.permissions import PERMISSIONS
    from app.constants.tenant import READ_ONLY_ROLES

    total_removed = 0
    for tenant in tenants:
        for role_name in READ_ONLY_ROLES:
            role = db.query(TenantRole).filter(
                TenantRole.tenant_id == tenant.id,
                TenantRole.name == role_name,
                TenantRole.is_system == True,
            ).first()
            if not role:
                continue
            for held in list(role.permissions):
                if PERMISSIONS.is_registered(held.permission) and not PERMISSIONS.is_read_only(held.permission):
                    db.delete(held)
                    total_removed += 1

    if total_added > 0 or total_removed > 0:
        db.commit()
        logger.info(
            f"Synced role permissions: added {total_added} missing permission(s), removed {total_removed} "
            f"changing permission(s) from roles that only read, across {len(tenants)} tenant(s)"
        )


# reset_database() — REMOVED
# This function was a security risk: it dropped ALL tables and recreated them.
# Database resets must be done manually via SQL (DROP SCHEMA public CASCADE)
# by an authorized operator with direct DB access, never via application code.