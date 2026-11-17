# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Brutor Agent Control Plane FastAPI Application 
* LLM Proxy for Chat traffic
* MCP Proxy for MCP Protocol traffic
* OAuth Proxy for OAuth traffic interception
Role-based access control for administrators with proper session persistence
OAuth 2.0 support with automatic token injection and refresh
Database-backed AI Models, MCP servers, virtual servers, registries, and IdPs
"""


import logging
from contextlib import asynccontextmanager
from typing import Optional, Sequence

from fastapi import FastAPI

from app import env_registry, env_vars

# How the application is put together: the services every router is built
# from, and the ordered list of routers.
from app.composition.edition import composed_enterprise_features
from app.composition.extensions import Extension, compose
from app.composition.routers import mount_routers
from app.composition.services import build_services
from app.composition.startup import StartupContext, run_steps
from app.core import account_lock, validation_errors
from app.middleware.maintenance import MaintenanceMiddleware


# Core services
from app.core.logging_system import BrutorLogger, setup_logging

# Middleware
from app.middleware.tenant import TenantMiddleware
from app.middleware.cors import CORSMiddleware
from app.middleware.authorship import AuthorshipContextMiddleware
from app.middleware.trusted_proxies import TrustedProxyMiddleware
from app.middleware.cache_invalidation import (
    CacheInvalidationWarningMiddleware,
    CommitBeforeResponseMiddleware,
)

# ============================================================================
# Suppress Third-Party Library DEBUG Logs
# ============================================================================

# Docker library - only show WARNING and above
logging.getLogger("docker").setLevel(logging.WARNING)
logging.getLogger("docker.utils.config").setLevel(logging.WARNING)
logging.getLogger("docker.auth").setLevel(logging.WARNING)
logging.getLogger("docker.api").setLevel(logging.WARNING)
logging.getLogger("docker.utils").setLevel(logging.WARNING)

# urllib3 - only show WARNING and above (suppress connection pool logs)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)

# FastAPI/Starlette - reduce noise but keep errors
logging.getLogger("uvicorn.access").setLevel(logging.INFO)  # Keep access logs
logging.getLogger("uvicorn.error").setLevel(logging.INFO)

# SQLAlchemy - only show WARNING and above (suppress SQL query logs)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)

# HTTP Clients - only show WARNING and above
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpcore.connection").setLevel(logging.WARNING)
logging.getLogger("httpcore.http11").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)



def create_app(
    logger: Optional[BrutorLogger] = None,
    extensions: Optional[Sequence[Extension]] = None,
) -> FastAPI:
    """
    Create the AI Gateway with hybrid configuration:
    - Gateway infrastructure config settings from ENV vars
    - AI Models, MCP servers, virtual servers, registries, IdPs in the database

    Configuration Precedence:  ENV (set by CLI args or environment) → Default

    Production-ready with async config loading for Gunicorn compatibility.

    `extensions` are composed with the built-in registries
    (`app/composition/extensions.py`): their routers are mounted after the
    built-in ones, their startup steps run after and their shutdown steps
    before, and their seed, provisioning and permission contributions join
    the process-wide registries. With none, the application is the built-in
    registries and nothing else.
    """
    # Everything an extension contributes is checked here, before the
    # application exists; a refused extension leaves nothing behind. Its
    # environment variables join the registry.
    composition = compose(extensions)

    # The environment, before anything of it is used: a name this edition
    # no longer reads, or a value that is not of its variable's type (the
    # signing algorithm among them), refuses the application, one line per
    # name and never a value. The edition is what was composed: the
    # Enterprise Edition's extension brings its own variables.
    enterprise = any(extension.env_vars for extension in (extensions or ()))
    env_registry.check(enterprise, missing=False)

    if logger is None:
        # The path Gunicorn takes: the log is set up here, from the
        # environment. `python main.py` hands its own logger in.
        logger = setup_logging(env_vars.BRUTOR_LOG_LEVEL.require(), use_json=env_vars.BRUTOR_LOG_JSON_ENABLED.flag())

    unread = env_registry.unread_product_variables(enterprise)
    if unread:
        logger.warning(
            f"These {env_registry.PRODUCT_PREFIX}* variables are set and this control plane "
            f"({'Enterprise' if enterprise else 'Community'} Edition) does not read them: a misspelt name, "
            "a variable of another service or of another edition. They have no effect here: "
            + ", ".join(unread)
        )
    # Which edition this application is: Enterprise when
    # anything composed into it belongs to an Enterprise feature.
    enterprise_features = composed_enterprise_features(
        extra=composition.routers + composition.startup + composition.shutdown
    )

    # ========================================================================
    # 1. Create services/managers (sync setup)
    #
    # The gateway configuration is read from environment variables here; the
    # database is initialized in the lifespan startup, which keeps this
    # compatible with Gunicorn/Uvicorn workers.
    # ========================================================================
    services = build_services(logger)
    config_manager = services.config_manager

    # ========================================================================
    # 2. Define lifespan (async startup/shutdown)
    # Create FastAPI app with lifecycle management
    # ========================================================================
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Application lifespan: runs the startup steps, serves, runs the
        shutdown steps. The steps themselves (database, preflights, background
        schedulers, the Redis publisher) are listed in
        `app/composition/startup.py`; none is written here.

        NOTE: Configuration is loaded synchronously from environment variables
              before this lifespan starts (via load_dotenv() in main.py)
        """
        context = StartupContext(
            app=app, services=services, logger=logger, enterprise_features=enterprise_features
        )

        # ====================================================================
        # Startup: the steps are listed in app/composition/startup.py
        # ====================================================================
        try:
            logger.info("Starting Brutor Agent Control Plane...")
            await run_steps(context, composition.startup)

        except Exception as e:
            logger.error("Failed to start gateway", context={"error": str(e)}, exc_info=True)
            raise

        # Yield control to run the application
        yield

        # ====================================================================
        # Shutdown
        # ====================================================================
        try:
            logger.info("Shutting down Brutor Agent Control Plane...")

            await run_steps(context, composition.shutdown)

            logger.info("Gateway shutdown complete")

        except Exception as e:
            logger.error("Error during shutdown", context={"error": str(e)}, exc_info=True)

    # ========================================================================
    # 3. Create app with lifespan
    # ========================================================================
    app = FastAPI(
        title="Brutor Agent Control Plane",
        description=(
            "The administration API of Brutor Agent Control: configuration, "
            "governance and compliance evidence. The Admin Console is its client."
        ),
        version=env_vars.BRUTOR_VERSION.require(),
        lifespan=lifespan
    )

    # ========================================================================
    # 4. Add middleware (needs app to exist)
    # ========================================================================
    # Added FIRST, so it is the INNERMOST middleware: commits the request's
    # writes as the response starts (a failed commit is a 500, not a 200)
    # and then publishes the proxy-cache invalidations they queued (see
    # app/middleware/cache_invalidation.py). It has to sit on the router's
    # own send path — the BaseHTTPMiddlewares below run the inner app in
    # another task.
    app.add_middleware(CommitBeforeResponseMiddleware)
    app.add_middleware(CORSMiddleware, logger=logger, config_manager=config_manager)
    # Maintenance gate. Starlette runs middleware in
    # REVERSE add order, so TenantMiddleware (added below) runs first to
    # populate request.state.tenant_id, then MaintenanceMiddleware reads
    # it and 503s when the tenant is in read_only/drain/paused mode.
    app.add_middleware(MaintenanceMiddleware)
    app.add_middleware(TenantMiddleware)
    # Added last, so it is the OUTERMOST middleware and runs first: the
    # actor slot has to exist in the request's own context before FastAPI
    # resolves the sync admin dependency in a threadpool copy. See
    # app/middleware/authorship.py for why a plain ContextVar.set there
    # silently does nothing.
    app.add_middleware(AuthorshipContextMiddleware)
    # Outside every middleware that reads the request: the client's
    # address, scheme and host every layer below sees are what
    # BRUTOR_TRUSTED_PROXIES allows a reverse proxy to say
    # (app/middleware/trusted_proxies.py). Only the warning middleware
    # below is outside it; that one reads no request field.
    app.add_middleware(TrustedProxyMiddleware)
    # Outermost of all, so it sees `http.response.start` last and can still
    # append to it: a config write that committed while the invalidation
    # publish failed comes back with a Warning rather than a bare 200.
    app.add_middleware(CacheInvalidationWarningMiddleware)

    # A request that fails validation is answered without any secret it
    # carried (app/core/validation_errors.py).
    validation_errors.install(app)
    # A 423 says which case it is (app/core/account_lock.py).
    account_lock.install(app)

    # ========================================================================
    # 5. Add routes (needs app to exist)
    #
    # The routers live in app/composition/routers.py. A new router is added
    # there, never mounted here; an extension's routers come after them.
    # ========================================================================
    mount_routers(app, services, composition.routers)
    app.state.extensions = composition.extensions

    return app