# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant Context Manager
Handles tenant isolation and context for multi-tenant requests
"""

import jwt
import hashlib
from contextvars import ContextVar
from typing import Optional, Any, Tuple
from fastapi import Request, HTTPException, status
from sqlalchemy.orm import Session

from app.env_vars import JWT_SECRET
from app.constants.http import HttpHeaders
from app.constants.tenant import TenantResourceTypes
from app.database.models.tenant import Tenant
from app.database.models.api_key import APIKey
from app.core.logging_system import BrutorLogger

logger = BrutorLogger("tenant_context")

# Context variable to store current tenant for the request
_current_tenant: ContextVar[Optional[str]] = ContextVar('current_tenant', default=None)


class TenantContext:
    """
    Thread-safe tenant context using contextvars

    This ensures each request has isolated tenant context even in async environments.
    """

    @staticmethod
    def set_tenant_id(tenant_id: str) -> None:
        """
        Set the current tenant ID for this request context

        Args:
            tenant_id: Tenant ID to set
        """
        _current_tenant.set(tenant_id)
        logger.debug(f"Set tenant context: {tenant_id}")

    @staticmethod
    def get_tenant_id() -> Optional[str]:
        """
        Get the current tenant ID for this request context

        Returns:
            Current tenant ID or None if not set
        """
        return _current_tenant.get()

    @staticmethod
    def clear() -> None:
        """Clear the tenant context"""
        _current_tenant.set(None)

    @staticmethod
    def require_tenant_id() -> str:
        """
        Get tenant ID or raise exception if not set

        Returns:
            Current tenant ID

        Raises:
            HTTPException: If tenant context is not set
        """
        tenant_id = _current_tenant.get()
        if not tenant_id:
            logger.error("Tenant context not set in request")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Tenant context not properly initialized"
            )
        return tenant_id


# The `error` of the 429 a tenant at its own quota is answered with.
TENANT_QUOTA_REACHED = "tenant_quota_reached"

class TenantService:
    """
    Service for tenant management and validation
    """

    @staticmethod
    def get_tenant_from_request(request: Request, db: Session) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract tenant_id from request JWT token or API Key

        The tenant ID comes from:
        1. TenantAdmin JWT token (contains tenant_id claim)
        2. API Key (contains tenant_id in database record)
        3. SystemAdmin can access any tenant (no tenant_id in token)
        4. Request state set by authentication middleware

        Args:
            request: FastAPI request object
            db: Database session

        Returns:
            Tuple of (tenant_id, source) where source is 'request_state',
            'admin', 'api_key', 'jwt', or None
        """
        # Priority 1: Check if already set in request state by auth middleware
        if hasattr(request.state, 'tenant_id'):
            return request.state.tenant_id, 'request_state'

        # Priority 2: Check if admin object is attached (set by dependencies)
        if hasattr(request.state, 'admin'):
            admin = request.state.admin
            admin_type = getattr(request.state, 'admin_type', None)

            # Tenant admins have tenant_id
            if admin_type == 'tenant' and hasattr(admin, 'tenant_id'):
                return admin.tenant_id, 'admin'

            # System admins don't have tenant_id - they can access any tenant
            # The tenant_id should be provided as query param or path param
            if admin_type == 'system':
                return None, None

        # Priority 3: Parse JWT token directly to extract tenant_id from payload
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

            # Check if this is an API Key (opaque token, not a JWT), such as
            # sk_brutor_api_VPBhQgykZX2uuetrFK47gOxCa_JD3SP3fKl8bHLoLSQ
            # API Keys are stored in the database and contain tenant_id.
            if token.startswith(HttpHeaders.BEARER_API_KEY_PREFIX.value):
                try:
                    key_hash = hashlib.sha256(token.encode()).hexdigest()
                    api_key = db.query(APIKey).filter(
                        APIKey.key_hash == key_hash
                    ).first()

                    if api_key and api_key.is_valid:
                        logger.debug("Extracted tenant_id from API Key", context={
                            "key_id": api_key.id,
                            "key_name": api_key.name,
                            "tenant_id": api_key.tenant_id,
                            "scope": api_key.scope,
                        })
                        return api_key.tenant_id, 'api_key'

                    logger.debug("API Key not found or invalid, tenant_id not resolved", context={
                        "key_prefix": token[:20] if token else None,
                        "found": api_key is not None,
                        "valid": api_key.is_valid if api_key else False,
                    })
                    return None, None
                except Exception as e:
                    logger.warning("Failed to look up API Key for tenant_id", context={
                        "error": str(e),
                        "key_prefix": token[:20] if token else None,
                    })
                    return None, None

            try:
                # Decode JWT directly to get tenant_id from payload
                jwt_secret = JWT_SECRET.exact()
                if not jwt_secret:
                    return None, None
                from app.core.jwt_settings import jwt_algorithm as configured_jwt_algorithm

                jwt_algorithm = configured_jwt_algorithm()

                # Decode without verification first to check the payload
                # This is safe because we only use it for tenant_id extraction
                # The actual authentication is done by the auth dependencies
                payload = jwt.decode(token, jwt_secret, algorithms=[jwt_algorithm])

                # Extract tenant_id directly from JWT payload
                tenant_id = payload.get('tenant_id')

                if tenant_id:
                    logger.debug(f"Extracted tenant_id from JWT: {tenant_id}")
                    return tenant_id, 'jwt'

                # Check admin_type to determine if this is a system admin
                admin_type = payload.get('admin_type')
                if admin_type == 'system':
                    logger.debug("System admin JWT detected (no tenant_id)")
                    return None, None

                # Tenant admin JWT should have tenant_id
                logger.warning("Tenant admin JWT missing tenant_id claim", context={
                    "user_id": payload.get('user_id'),
                    "username": payload.get('username')
                })
                return None, None

            except jwt.ExpiredSignatureError:
                logger.warning("JWT token expired", exc_info=False)
                return None, None
            except jwt.InvalidTokenError as e:
                logger.warning(f"Invalid JWT token: {e}", exc_info=False)
                return None, None
            except Exception as e:
                logger.error(f"Error decoding JWT for tenant_id: {e}", exc_info=True)
                return None, None

        return None, None

    @staticmethod
    def get_or_create_default_tenant(db: Session) -> Tenant:
        """
        Get or create the default tenant for single-tenant installations

        Args:
            db: Database session

        Returns:
            Default tenant instance
        """
        default_tenant = db.query(Tenant).filter(Tenant.is_default == True).first()

        if not default_tenant:
            logger.info("Creating default tenant")
            default_tenant = Tenant.create_default_tenant()
            db.add(default_tenant)
            db.flush()
            db.refresh(default_tenant)

        return default_tenant

    @staticmethod
    def validate_tenant_cached(db: Session, tenant_id: str) -> Optional[Tenant]:
        """``validate_tenant`` behind the per-request read model.

        Returns the ``Tenant`` row when the table was consulted, or ``None``
        when the read model vouched for the tenant (seen active and
        unsuspended within ``auth_read_model.TTL_SECONDS``). Callers that
        need the row query it themselves; the middleware only needs the
        verdict. A status edit on the tenant clears the entry through the
        ORM event registered in ``models/tenant.py``.
        """
        from app.services import auth_read_model
        if auth_read_model.tenant_known_active(tenant_id) is not None:
            return None
        tenant = TenantService.validate_tenant(db, tenant_id)
        auth_read_model.remember_tenant_active(tenant.id, tenant.name or "")
        return tenant

    @staticmethod
    def validate_tenant(db: Session, tenant_id: str) -> Tenant:
        """
        Validate tenant exists and is active

        Args:
            db: Database session
            tenant_id: Tenant ID to validate

        Returns:
            Tenant instance

        Raises:
            HTTPException: If tenant not found, inactive, or suspended
        """
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()

        if not tenant:
            logger.error(f"Tenant not found: {tenant_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{tenant_id}' not found"
            )

        if not tenant.is_active:
            logger.warning(f"Tenant is inactive: {tenant_id}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant account is inactive"
            )

        if tenant.suspended:
            logger.warning(f"Tenant is suspended: {tenant_id}", context={
                'reason': tenant.suspension_reason
            })
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tenant account is suspended: {tenant.suspension_reason}"
            )

        # What else can make a tenant unusable is not this edition's to know
        # (the Enterprise Edition's trial expiry): a registered check refuses
        # by raising.
        from app.composition.hooks import HOOKS, TENANT_STATUS_CHECKS

        for check in HOOKS.all(TENANT_STATUS_CHECKS):
            check(tenant=tenant)

        return tenant

    @staticmethod
    def check_quota(db: Session, tenant_id: str, resource_type: str) -> None:
        """
        Check if tenant has exceeded quota for a resource type

        Args:
            db: Database session
            tenant_id: Tenant ID
            resource_type: Type of resource to check

        Raises:
            HTTPException: If quota exceeded
        """
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()

        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{tenant_id}' not found"
            )

        # Get current count based on resource type.
        # For LLM models and MCP servers, count only ENABLED ones (not the full catalog).
        from app.database.models.admin import TenantAdmin
        from app.database.models.end_user import EndUser

        count_queries = {
            'admin_users': lambda: db.query(TenantAdmin).filter(TenantAdmin.tenant_id == tenant_id).count(),
            # Without the users a traffic import created.
            'end_users': lambda: EndUser.count_for_quota(db, tenant_id),
        }

        # LLM models — count enabled only (catalog can have 300+ models but only a few enabled)
        try:
            from app.database.models.llm_model import LLModel
            count_queries['llm_models'] = lambda: db.query(LLModel).filter(
                LLModel.tenant_id == tenant_id, LLModel.enabled == True,
                # A routing group's virtual row (`rg-<id>`) is how the
                # group is addressed, not a model of the tenant.
                LLModel.mode != "routing"
            ).count()
        except ImportError:
            pass

        # MCP servers — count enabled only
        try:
            from app.database.models.mcp_server import MCPServer
            count_queries['mcp-servers'] = lambda: db.query(MCPServer).filter(
                MCPServer.tenant_id == tenant_id, MCPServer.enabled == True
            ).count()
        except ImportError:
            pass

        try:
            from app.database.models.agent_skill import AgentSkill
            count_queries['agent_skills'] = lambda: db.query(AgentSkill).filter(AgentSkill.tenant_id == tenant_id).count()
        except ImportError:
            pass

        try:
            from app.database.models.agent_card import AgentCard
            count_queries['agent_cards'] = lambda: db.query(AgentCard).filter(AgentCard.tenant_id == tenant_id).count()
        except ImportError:
            pass

        try:
            from app.database.models.agent_identity import AgentIdentity
            count_queries['agent_identities'] = lambda: db.query(AgentIdentity).filter(AgentIdentity.tenant_id == tenant_id).count()
        except ImportError:
            pass

        # The resource types of Enterprise Edition features (MCP registries,
        # knowledge-base collections) are counted by whoever registered at the
        # hook point; this module names none of
        # their tables. A hook that fails is an error, not a quota left
        # unenforced.
        from app.composition.hooks import HOOKS, TENANT_QUOTA_COUNTS

        for hook in HOOKS.all(TENANT_QUOTA_COUNTS):
            for counted_type, count in hook(db=db, tenant_id=tenant_id).items():
                count_queries.setdefault(counted_type, count)

        try:
            from app.database.models.end_user_group import EndUserGroup
            count_queries['end_user_groups'] = lambda: db.query(EndUserGroup).filter(EndUserGroup.tenant_id == tenant_id).count()
        except ImportError:
            pass

        if resource_type not in count_queries:
            # No count query for this type. Distinguish two cases:
            #   * a VALID resource type that simply has no quota configured
            #     (no `max_*` field / quota-map entry) — it's unlimited by
            #     design, so skip quietly (debug), don't cry wolf on every
            #     create; and
            #   * a genuinely unrecognised value — that's a real wiring bug
            #     worth a warning.
            valid_values = {t.value for t in TenantResourceTypes}
            if resource_type in TenantResourceTypes or resource_type in valid_values:
                logger.debug(
                    f"No quota configured for resource type '{resource_type}' — skipping check"
                )
            else:
                logger.warning(f"Unknown resource type for quota check: {resource_type}")
            return

        # A bound above the tenant's own quota belongs to whoever registered
        # at the hook point; this module knows of none. A hook refuses by
        # raising, and one that fails is an error, not a bound left out.
        from app.composition.hooks import TENANT_QUOTA_CEILINGS

        for ceiling in HOOKS.all(TENANT_QUOTA_CEILINGS):
            ceiling(db=db, tenant=tenant, resource_type=resource_type, count=count_queries[resource_type])

        current_count = count_queries[resource_type]()

        if tenant.is_quota_exceeded(resource_type, current_count):
            # The tenant's own quota, by the name of its column (the name
            # the licence gives the same limit).
            quota_limits = {
                'admin_users': 'max_admin_users',
                'end_users': 'max_end_users',
                'end_user_groups': 'max_end_user_groups',
                'llm_models': 'max_llm_models',
                'mcp-servers': 'max_mcp_servers',
                'registries': 'max_mcp_registries',
                'knowledge_base': 'max_kb_collections',
                'agent_skills': 'max_agent_skills',
                'agent_cards': 'max_agent_cards',
                'agent_identities': 'max_agent_identities',
            }
            limit_name = quota_limits.get(resource_type)
            max_limit = getattr(tenant, limit_name, None) if limit_name else None

            logger.warning(f"Quota exceeded for tenant {tenant_id}", context={
                'resource_type': resource_type,
                'current_count': current_count,
                'max_limit': max_limit
            })

            # Structured, as the licence's 403 is (a
            # sentence alone would have to be taken apart by pattern).
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": TENANT_QUOTA_REACHED,
                    "limit": limit_name,
                    "maximum": max_limit,
                    "current": current_count,
                    "message": (
                        f"Quota exceeded for {resource_type.replace('_', ' ')}. "
                        f"Current: {current_count}, Maximum: {max_limit}. "
                        f"Please upgrade your plan."
                    ),
                },
            )

    @staticmethod
    def quota_refusal_reason(refusal: HTTPException) -> str:
        """The sentence of a `check_quota` refusal, for a creation path that
        reports it in a form of its own (an import's per-item result, a
        refused sign-in) instead of letting it be the response
        A hook's refusal carries its sentence as `message`."""
        detail = refusal.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("error") or detail)
        return str(detail)

    @staticmethod
    def check_feature_access(db: Session, tenant_id: str, feature_name: str) -> None:
        """
        Check if tenant has access to a specific feature

        Args:
            db: Database session
            tenant_id: Tenant ID
            feature_name: Feature to check

        Raises:
            HTTPException: If feature is not enabled for tenant
        """
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()

        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{tenant_id}' not found"
            )

        if not tenant.has_feature(feature_name):
            logger.warning(f"Feature not enabled for tenant {tenant_id}: {feature_name}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature '{feature_name}' is not enabled for your account. "
                       f"Please upgrade your plan."
            )


def get_tenant_filter(model_class: Any) -> Any:
    """
    Get SQLAlchemy filter for current tenant

    Usage:
        query = db.query(MCPServer).filter(get_tenant_filter(MCPServer))

    Args:
        model_class: SQLAlchemy model class

    Returns:
        SQLAlchemy filter expression
    """
    tenant_id = TenantContext.require_tenant_id()
    return model_class.tenant_id == tenant_id


def apply_tenant_filter(query: Any, model_class: Any) -> Any:
    """
    Apply tenant filter to existing query

    Usage:
        query = db.query(MCPServer).filter(MCPServer.enabled == True)
        query = apply_tenant_filter(query, MCPServer)

    Args:
        query: SQLAlchemy query object
        model_class: SQLAlchemy model class

    Returns:
        Filtered query
    """
    tenant_id = TenantContext.require_tenant_id()
    return query.filter(model_class.tenant_id == tenant_id)