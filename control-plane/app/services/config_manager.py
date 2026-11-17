# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Configuration of the application, as the services ask for it.

Every value comes from the environment-variable registry
(`app/env_registry.py`, `app/env_vars.py`): this class reads nothing
itself. It is what the services that are handed "the configuration" hold
(the encryption service, the CORS middleware, the service-information
routes), and it checks the two shared secrets when the application is
built.
"""

from typing import List, Optional

from app import env_vars
from app.core.logging_system import BrutorLogger


PLACEHOLDER_SECRET_MARKERS = ("your-", "change-this", "changeme", "example")


def is_placeholder_secret(value: str) -> bool:
    """True when ``value`` is one of the published example secrets.

    Mirrors ``config::is_placeholder_secret`` in the core proxy and the
    registry. Length is deliberately *not* part of this check here — the
    weak-length warning above stays a warning for compatibility with
    existing deployments; a template value is never acceptable.
    """
    v = (value or "").strip().lower()
    return any(m in v for m in PLACEHOLDER_SECRET_MARKERS)


class ConfigManager:
    """The configuration the application's services read.

    Precedence: the environment (a `.env` file is loaded into it at start;
    a command-line option of `python main.py` overrides it), then the
    default of the variable's definition.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None, validate: bool = True):
        """
        Args:
            logger: Logger instance for operation tracking
            validate: Check the shared secrets (disable for read-only usage)
        """
        self.logger = logger or BrutorLogger("config-manager")

        if validate:
            self.validate_production_config()

    # =========================================================================
    # Production Validation
    # =========================================================================

    def validate_production_config(self) -> bool:
        """
        Validate required configuration for production deployment

        Returns:
            bool: True if all required variables are present and valid
        """
        required_vars = [env_vars.JWT_SECRET, env_vars.DATABASE_URL, env_vars.ENCRYPTION_KEY]

        missing_vars = [var.name for var in required_vars if not var.is_set()]
        weak_vars = []

        if missing_vars:
            self.logger.error(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )
            self.logger.error(
                "Set these in your .env file or container environment"
            )

        # The two secrets every token and every stored credential depend on.
        for var in (env_vars.JWT_SECRET, env_vars.ENCRYPTION_KEY):
            value = var.exact()
            if value and len(value) < 32:
                weak_vars.append(f"{var.name} (should be 32+ characters)")
                self.logger.warning(f"{var.name} is weak (< 32 characters)")

        # A stock password is not warned about here: the password rule
        # refuses it where the account is created (`check_password`).

        is_valid = len(missing_vars) == 0

        if weak_vars:
            self.logger.warning(
                f"Weak configuration detected: {', '.join(weak_vars)}"
            )

        # A secret that still carries a template value is a public secret:
        # every admin, portal and gateway token is signed with JWT_SECRET
        # and every stored provider key is wrapped with ENCRYPTION_KEY.
        # Refuse to start — the same rule the core proxy and the registry
        # apply, so the platform never comes up half-protected.
        placeholders = [
            var.name for var in (env_vars.JWT_SECRET, env_vars.ENCRYPTION_KEY)
            if var.exact() and is_placeholder_secret(var.exact())
        ]
        if placeholders:
            raise RuntimeError(
                f"{', '.join(placeholders)} still hold a placeholder value from an example "
                "file. Generate real secrets (openssl rand -hex 32) and set them in the "
                "environment; the control plane refuses to start with a publicly known secret."
            )

        return is_valid

    # =========================================================================
    # Server Configuration
    # =========================================================================

    def get_version(self) -> str:
        """The version this control plane reports."""
        return env_vars.BRUTOR_VERSION.require()

    def get_listen_addr(self) -> str:
        """`host:port` the server binds (BRUTOR_CONTROL_PLANE_LISTEN_ADDR)."""
        return env_vars.BRUTOR_CONTROL_PLANE_LISTEN_ADDR.require()

    def get_host(self) -> str:
        """Host of the listen address (an IPv6 address without brackets)."""
        return env_vars.split_listen_addr(self.get_listen_addr())[0]

    def get_port(self) -> int:
        """Port of the listen address."""
        return env_vars.split_listen_addr(self.get_listen_addr())[1]

    def get_gateway_base_url(self) -> str:
        """The control plane's own base URL as a browser reaches it; when
        none is configured, http:// and the listen address."""
        return env_vars.BRUTOR_CONTROL_PLANE_PUBLIC_URL.string() or f"http://{self.get_listen_addr()}"

    # =========================================================================
    # JWT & Encryption Configuration
    # =========================================================================

    def get_jwt_secret(self) -> str:
        """Get JWT secret (REQUIRED)"""
        secret = env_vars.JWT_SECRET.exact()
        if not secret:
            raise ValueError(f"{env_vars.JWT_SECRET.name} must be set in environment")
        return secret

    def get_jwt_algorithm(self) -> str:
        """Get JWT signing algorithm"""
        from app.core.jwt_settings import jwt_algorithm

        return jwt_algorithm()

    def get_encryption_key(self) -> str:
        """Get encryption key (REQUIRED)"""
        key = env_vars.ENCRYPTION_KEY.exact()
        if not key:
            raise ValueError(f"{env_vars.ENCRYPTION_KEY.name} must be set in environment")
        return key

    # =========================================================================
    # Database
    # =========================================================================

    def get_database_url(self) -> str:
        """The database URL; empty when none is configured (the engine
        itself refuses to be built without one)."""
        return env_vars.DATABASE_URL.exact() or ""

    # =========================================================================
    # Logging Configuration
    # =========================================================================

    def get_log_level(self) -> str:
        """Get log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"""
        return env_vars.BRUTOR_LOG_LEVEL.require()

    def use_json_logging(self) -> bool:
        """Whether the log is written as JSON."""
        return env_vars.BRUTOR_LOG_JSON_ENABLED.flag()

    # =========================================================================
    # CORS Configuration
    # =========================================================================

    def get_cors_origins(self) -> List[str]:
        """Get allowed CORS origins"""
        return env_vars.BRUTOR_CORS_ORIGINS.items()
