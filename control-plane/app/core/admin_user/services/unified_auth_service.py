# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from app.constants.tenant import TenantDefaultRole

"""
Unified Authentication Service
Verifies every administrator's token through the principal registry, and
signs tenant administrators in. Another principal's sign-in is its own
(the system administrator's: `enterprise/brutor_enterprise/multi_tenant/system_principal.py`).
"""

import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, Union, Literal
import bcrypt
import jwt
from sqlalchemy.orm import Session

from app.constants.admin import AdminType
from app.services import auth_read_model
from app.database.models.admin import TenantAdmin, TenantAdminSession, TenantRole
from app.composition.principals import PRINCIPALS, PrincipalSpec
from app.services.mfa.policy import has_2fa, requires_2fa_for_admins, tenant_mfa_policy
from app.core.admin_user import lockout
from app.core.logging_system import BrutorLogger


def _admin_type(name: str) -> Union[AdminType, str]:
    """A principal's name as handlers compare it: the `AdminType` member for
    the two this code base defines, the name itself for any other."""
    try:
        return AdminType(name)
    except ValueError:
        return name


class UnifiedAuthService:
    """
    Unified authentication service for the admin API

    This service handles:
    - Tenant admin login (tenant-scoped access)
    - JWT token generation with admin type
    - Session management for both admin types
    """

    def __init__(
        self,
        jwt_secret: str,
        jwt_expiry_hours: int = 8,
        jwt_algorithm: str = "HS256",
        logger: Optional[BrutorLogger] = None
    ):
        self.jwt_secret = jwt_secret
        self.jwt_expiry_hours = jwt_expiry_hours
        self.jwt_algorithm = jwt_algorithm
        self.logger = logger or BrutorLogger("unified-auth")
        self.max_failed_attempts = lockout.MAX_FAILED_ATTEMPTS

    def _hash_password(self, password: str) -> str:
        from app.core.password_rule import check_password

        check_password(password)
        """Hash password using bcrypt"""
        return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        try:
            return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
        except Exception as e:
            self.logger.error("Password verification error", context={
                "error": str(e)
            }, exc_info=True)
            return False

    def login_tenant_admin(
        self,
        username: str,
        password: str,
        tenant_id: Optional[str] = None,
        db: Session = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Authenticate tenant admin (tenant-scoped access)

        Args:
            username: Username or email
            password: Password
            tenant_id: Tenant ID (optional — if omitted, resolves from username)
            db: Database session
            ip_address: Optional IP address
            user_agent: Optional user agent string

        Returns:
            Dict with token and admin info, or None if authentication failed
        """
        try:
            # Find admin — by tenant+username if tenant given, or by username only
            if tenant_id:
                admin = db.query(TenantAdmin).filter(
                    TenantAdmin.username == username,
                    TenantAdmin.tenant_id == tenant_id
                ).first()
            else:
                # No tenant id given: the user name is unique, resolve the tenant from it
                admin = db.query(TenantAdmin).filter(
                    TenantAdmin.username == username
                ).first()
                if admin:
                    tenant_id = admin.tenant_id

            if not admin:
                # The refusal costs what a wrong password costs (no faster
                # answer for a name that does not exist).
                lockout.spend_one_verification(password)
                self.logger.warning("Tenant admin login failed - user not found", context={
                    "username": username,
                    "tenant_id": tenant_id
                })
                return None

            # The lock is looked at BEFORE the password: a locked account is
            # refused whatever was sent, so a guess made while it is locked
            # learns nothing. A timed lock whose end has passed is released
            # here and the count starts again.
            if not admin.is_active or lockout.release_if_expired(admin):
                lockout.spend_one_verification(password)
                self.logger.warning("Tenant admin login failed - inactive or locked", context={
                    "username": username,
                    "tenant_id": tenant_id,
                    "is_active": admin.is_active,
                    "is_locked": admin.is_locked
                })
                return None

            # A count already at the threshold on an account not marked
            # locked (a row written by hand): it is locked now.
            if lockout.lock_at_threshold(admin, db, max_attempts=self.max_failed_attempts):
                lockout.spend_one_verification(password)
                self.logger.warning("Tenant admin locked due to failed attempts", context={
                    "username": username,
                    "tenant_id": tenant_id,
                    "failed_attempts": admin.failed_login_attempts
                })
                return None

            # Verify password. A wrong one is counted and COMMITTED
            # (`lockout.count_failure`): the route refuses by
            # raising, which rolls this session back.
            if not self._verify_password(password, admin.password_hash):
                locked = lockout.count_failure(admin, db, max_attempts=self.max_failed_attempts)
                self.logger.warning("Tenant admin login failed - invalid password", context={
                    "username": username,
                    "tenant_id": tenant_id,
                    "failed_attempts": admin.failed_login_attempts,
                    "locked": locked,
                })
                return None

            # Does this sign-in owe a second factor? Either the admin has one
            # (a code is owed), or the tenant's policy requires one and the
            # admin has none (an enrolment is owed). The policy is read
            # here, at the one place a tenant admin's password is accepted:
            # the form without a tenant id in the request is this same method.
            has_second_factor = has_2fa(admin)
            must_enrol = not has_second_factor and requires_2fa_for_admins(
                tenant_mfa_policy(db, tenant_id)
            )

            # Success - reset failed attempts. While a second factor is owed
            # the password is only half a login; the counter also tracks
            # wrong TOTP codes (login_with_2fa, the enrolment at sign-in) and
            # is cleared there, after the code verifies — never here, or a
            # caller who knows the password could clear the 2FA lockout by
            # logging in again.
            if not (has_second_factor or must_enrol):
                lockout.clear(admin)
                admin.last_login = datetime.now(timezone.utc)
                db.flush()

            # Check if 2FA is enabled — return a challenge instead of full JWT
            if has_second_factor:
                return self._second_factor_challenge(
                    admin, tenant_id, db,
                    token_type=self.TWO_FACTOR_CHALLENGE_TYPE,
                    flag='requires_2fa',
                    minutes=self.TWO_FACTOR_CHALLENGE_MINUTES,
                )

            # The policy requires 2FA and this admin has none: NO session.
            # The token below is good for enrolling a first factor and for
            # nothing else (`type`, not `typ`: `verify_token` refuses it, and
            # so do the core proxy and the registry, which share the secret).
            if must_enrol:
                return self._second_factor_challenge(
                    admin, tenant_id, db,
                    token_type=self.TWO_FACTOR_ENROLLMENT_TYPE,
                    flag='requires_2fa_enrollment',
                    minutes=self.TWO_FACTOR_ENROLLMENT_MINUTES,
                )

            return self.issue_tenant_admin_login(
                admin, tenant_id, db, ip_address=ip_address, user_agent=user_agent
            )

        except Exception as e:
            self.logger.error("Tenant admin login error", context={
                "username": username,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            return None

    #: `type` of the token that stands for "password accepted, a code is owed".
    TWO_FACTOR_CHALLENGE_TYPE = '2fa_challenge'
    TWO_FACTOR_CHALLENGE_MINUTES = 5
    #: `type` of the token that stands for "password accepted, the tenant's
    #: policy requires 2FA and this admin has none": accepted by the two
    #: enrolment routes (`/login/2fa/enroll/*`) and by nothing else.
    TWO_FACTOR_ENROLLMENT_TYPE = '2fa_enrollment'
    TWO_FACTOR_ENROLLMENT_MINUTES = 10

    def _second_factor_challenge(
        self,
        admin: TenantAdmin,
        tenant_id: str,
        db: Session,
        *,
        token_type: str,
        flag: str,
        minutes: int,
    ) -> Dict[str, Any]:
        """What a tenant admin's password sign-in answers instead of a session
        while a second factor is owed: a short-lived token of `token_type`
        and the `flag` the client routes on."""
        now = datetime.now(timezone.utc)
        payload = {
            'type': token_type,
            'admin_id': admin.id,
            'tenant_id': tenant_id,
            # Explicit admin_type so the consumer doesn't have to infer from
            # tenant_id presence (a sysadmin challenge has tenant_id=None
            # too; before this field existed they were indistinguishable).
            'admin_type': AdminType.TENANT_ADMIN_TYPE.value,
            'iat': now,
            'exp': now + timedelta(minutes=minutes),
        }
        temp_token = jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

        # Get tenant info for display
        from app.database.models.tenant import Tenant
        tenant_obj = db.query(Tenant).filter(Tenant.id == tenant_id).first()

        return {
            flag: True,
            'temp_token': temp_token,
            # admin_type echoed so the React client can route without
            # inspecting the JWT — and so the endpoint that returns this dict
            # can be told apart from its system-admin counterpart by
            # consumers downstream.
            'admin_type': AdminType.TENANT_ADMIN_TYPE.value,
            'tenant_id': tenant_id,
            'tenant_display_name': tenant_obj.display_name if tenant_obj else tenant_id,
            'admin': {'username': admin.username, 'email': admin.email},
        }

    def issue_tenant_admin_login(
        self,
        admin: TenantAdmin,
        tenant_id: str,
        db: Session,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Issue a session to a tenant admin whose sign-in is complete, and
        return the login response.

        The one place the response of a tenant admin's sign-in is built: the
        password sign-in ends here, and so does an enrolment at sign-in. The
        caller has done every check; nothing here decides whether to sign in.
        """
        token = self.create_admin_jwt(
            admin, tenant_id, db=db, ip_address=ip_address, user_agent=user_agent
        )

        # Downgraded from INFO — successful logins are audit-log
        # material, not operational-log material. Failed logins
        # remain at WARNING.
        self.logger.debug("Tenant admin logged in", context={
            "admin_id": admin.id,
            "username": admin.username,
            "tenant_id": tenant_id,
        })

        # Get tenant info for display + trial status
        from app.database.models.tenant import Tenant
        tenant_obj = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        tenant_display_name = tenant_obj.display_name if tenant_obj else tenant_id

        # Stamp tenant activity on every admin login — gives ops a
        # cheap signal of which tenants are actively used without
        # having to query proxy_logs or audit tables.
        if tenant_obj is not None:
            tenant_obj.update_last_activity()
            db.flush()

        return {
            'access_token': token,
            'token_type': 'bearer',
            'expires_in': self.jwt_expiry_hours * 3600,
            'admin_type': AdminType.TENANT_ADMIN_TYPE.value,
            'tenant_id': tenant_id,
            'tenant_display_name': tenant_display_name,
            'is_trial': tenant_obj.is_trial if tenant_obj else False,
            'trial_ends_at': tenant_obj.trial_ends_at.isoformat() if tenant_obj and tenant_obj.trial_ends_at else None,
            'suspended': tenant_obj.suspended if tenant_obj else False,
            'suspension_reason': tenant_obj.suspension_reason if tenant_obj else None,
            # A generated password: the client sends the admin to change it
            # first; every other admin route refuses the session until then.
            'password_change_required': bool(admin.password_change_required),
            'admin': admin.to_dict()
        }

    def create_admin_jwt(
        self,
        admin: TenantAdmin,
        tenant_id: str,
        db: Optional[Session] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> str:
        """
        Create a JWT for a tenant admin: the session of every completed
        tenant-admin sign-in (password alone, password + code, enrolment).

        Mints the JWT AND persists a matching TenantAdminSession row —
        `verify_token` looks up the session by jti on every request, so
        without the session row every follow-up API call fails with
        "Invalid or expired token". Pass `db` to persist the session
        (recommended); callers that already created their own session
        row may pass db=None, but that's a footgun and will break auth.
        """
        role_name = admin.role.name if admin.role else None
        permissions = admin.role.get_permissions() if admin.role else []
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.jwt_expiry_hours)

        payload = {
            # Every admin token says so; a verifier requires it.
            'typ': self.ADMIN_TOKEN_TYPE,
            'user_id': admin.id,
            'username': admin.username,
            'admin_type': AdminType.TENANT_ADMIN_TYPE.value,
            'tenant_id': tenant_id,
            'role_id': admin.role_id,
            'role_name': role_name,
            'permissions': permissions,
            'iat': now,
            'exp': expires_at,
            'jti': session_id,
        }
        token = jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

        if db is not None:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            session = TenantAdminSession(
                id=session_id,
                tenant_id=tenant_id,
                admin_id=admin.id,
                token_hash=token_hash,
                ip_address=ip_address,
                user_agent=user_agent,
                created_at=now,
                expires_at=expires_at,
                last_activity=now,
                is_active=True,
                risk_score=0.0,
            )
            db.add(session)
            db.flush()

        return token

    #: `typ` of every admin session token: `create_admin_jwt` mints it, and so
    #: does every other principal's sign-in.
    ADMIN_TOKEN_TYPE = 'admin'

    @staticmethod
    def _principal_of(payload: Dict[str, Any]) -> Optional[PrincipalSpec]:
        """The registered principal an admin token declares, or None when the
        token is not one: its `typ` is not `admin`, or its `admin_type` is
        missing or not registered (`app/composition/principals.py`). Never a
        default."""
        if payload.get('typ') != UnifiedAuthService.ADMIN_TOKEN_TYPE:
            return None
        return PRINCIPALS.get(payload.get('admin_type'))

    @staticmethod
    def _admin_type_of(payload: Dict[str, Any]) -> Optional[str]:
        """The admin type an admin token declares, or None when the token is
        not one (see `_principal_of`). Never a default."""
        principal = UnifiedAuthService._principal_of(payload)
        return principal.name if principal is not None else None

    @staticmethod
    def _session_recently_verified(session_id: Optional[str]) -> bool:
        """True when the read model vouches for the session: it was seen
        active within ``auth_read_model.TTL_SECONDS`` and has not expired
        since. Revocation clears the entry (see ``deactivate()`` on the
        session models), so a revoked session is refused on the next
        request even inside the window."""
        if not session_id:
            return False
        valid_until = auth_read_model.session_valid_until(session_id)
        return valid_until is not None and valid_until > datetime.now(timezone.utc)

    def verify_token(
            self,
            token: str,
            db: Session
    ) -> Optional[Tuple[Any, Any, Union[AdminType, str]]]:
        """
        Verify JWT token and return admin and session

        Args:
            token: JWT token
            db: Database session

        Returns:
            Tuple of (Admin, Session, admin_type) or None if invalid
        """
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm])
            return self.verify_claims(payload, db)

        except jwt.ExpiredSignatureError:
            self.logger.debug("JWT token expired")
            return None
        except jwt.InvalidTokenError:
            self.logger.debug("Invalid JWT token")
            return None
        except Exception as e:
            self.logger.error("Token verification error", context={
                "error": str(e)
            }, exc_info=True)
            return None

    def token_claims(self, token: str) -> Optional[Dict[str, Any]]:
        """The claims of a token this service signed and that has not
        expired, or None. `verify_claims` says whether they are still a
        session."""
        try:
            return jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm])
        except jwt.InvalidTokenError:
            return None

    def verify_claims(
            self,
            payload: Dict[str, Any],
            db: Session
    ) -> Optional[Tuple[Any, Any, Union[AdminType, str]]]:
        """Whether the claims of a verified token are an open session now:
        (Admin, Session, admin_type), or None. Every check of
        `verify_token` but the signature, for a caller that verified the
        token earlier and kept its claims (a stream ticket)."""
        try:
            # Claims kept from an earlier verification can have expired since.
            expires = payload.get('exp')
            if expires is not None and datetime.now(timezone.utc).timestamp() >= float(expires):
                return None
            # A token says what it is, or it is refused.
            # What it may say is what the principal registry holds: a type
            # nobody registered — a missing claim, an unknown name, the system
            # administrator in an installation that has none — is no
            # principal, and there is no branch to fall into.
            principal = self._principal_of(payload)
            if principal is None:
                self.logger.warning("Token is not an admin token", context={
                    "typ": payload.get('typ'),
                    "admin_type": payload.get('admin_type'),
                })
                return None

            # The principal's own verifier decides. Anything but an
            # (administrator, session) pair is a refusal, and so is a verifier
            # that raises (the handler below).
            verified = principal.verify(self, payload, db)
            if not isinstance(verified, tuple) or len(verified) != 2 or verified[0] is None:
                return None
            admin, session = verified
            return admin, session, _admin_type(principal.name)
        except Exception as e:
            self.logger.error("Token verification error", context={
                "error": str(e)
            }, exc_info=True)
            return None

    def logout(self, token: str, db: Session) -> bool:
        """
        Logout and deactivate session (works for both admin types)

        Args:
            token: JWT token
            db: Database session

        Returns:
            True if successful
        """
        try:
            payload = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=[self.jwt_algorithm],
                options={"verify_exp": False}
            )
            jwt_id = payload.get('jti')
            principal = self._principal_of(payload)
            if principal is None:
                # Not an admin token: it ends nobody's session.
                return False
            admin_type = principal.name

            if jwt_id:
                session = principal.find_session(db, jwt_id)

                if session:
                    session.deactivate("user_logout")
                    db.flush()
                    self.logger.info(f"{admin_type.title()} admin logged out", context={
                        "session_id": jwt_id,
                        "admin_type": admin_type
                    })
                    return True

        except Exception as e:
            self.logger.error("Logout error", context={
                "error": str(e)
            }, exc_info=True)

        return False

    def create_tenant_admin(
        self,
        username: str,
        email: str,
        password: str,
        tenant_id: str,
        db: Session,
        full_name: Optional[str] = None,
        role_id: Optional[str] = None,
        created_by: Optional[str] = None
    ) -> TenantAdmin:
        """
        Create new tenant admin with role assignment

        Args:
            username: Username (unique within tenant)
            email: Email address (unique within tenant)
            password: Plain text password
            tenant_id: Tenant ID
            db: Database session
            full_name: Optional full name
            role_id: Optional role ID to assign (defaults to "Tenant Admin" role)
            created_by: Optional admin who created this account

        Returns:
            Created TenantAdmin

        Raises:
            ValueError: If admin already exists in tenant or role not found
        """
        try:
            # Check if admin exists in this tenant
            existing = db.query(TenantAdmin).filter(
                TenantAdmin.tenant_id == tenant_id,
                (TenantAdmin.username == username) | (TenantAdmin.email == email)
            ).first()

            if existing:
                raise ValueError(
                    f"Tenant admin with username '{username}' or email '{email}' already exists in this tenant"
                )

            # Get role - use provided role_id or default to "Tenant Admin" role
            if role_id:
                # Use provided role
                role = db.query(TenantRole).filter(
                    TenantRole.id == role_id,
                    TenantRole.tenant_id == tenant_id
                ).first()

                if not role:
                    raise ValueError(f"Role '{role_id}' not found in tenant '{tenant_id}'")
            else:
                # Default to "Tenant Admin" role
                role = db.query(TenantRole).filter(
                    TenantRole.tenant_id == tenant_id,
                    TenantRole.name == TenantDefaultRole.ADMIN_USER.value,
                    TenantRole.is_system == True
                ).first()

                if not role:
                    raise ValueError(
                        f"Default '{TenantDefaultRole.ADMIN_USER.value}' role not found for tenant '{tenant_id}'. "
                        "Please run database initialization to create system roles."
                    )

            admin = TenantAdmin(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                username=username,
                email=email,
                password_hash=self._hash_password(password),
                full_name=full_name,
                role_id=role.id,  # Assign role via role_id
                is_active=True,
                is_locked=False,
                failed_login_attempts=0,
                created_at=datetime.now(timezone.utc),
                created_by=created_by
            )

            db.add(admin)
            db.flush()
            db.refresh(admin)

            self.logger.info("Tenant admin created with role", context={
                "admin_id": admin.id,
                "tenant_id": tenant_id,
                "username": username,
                "role_id": role.id,
                "role_name": role.name
            })

            return admin

        except ValueError:
            raise
        except Exception as e:
            self.logger.error("Failed to create tenant admin", context={
                "username": username,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    def unlock_admin(self, admin_id: str, admin_type: AdminType, tenant_id: Optional[str], db: Session) -> bool:
        """
        Unlock a locked admin account

        Args:
            admin_id: Admin ID
            admin_type: a registered principal's name ('system' or 'tenant')
            tenant_id: Required for an administrator pinned to a tenant
            db: Database session

        Returns:
            True if unlocked. False for an administrator that is not found,
            and for a type that is not a registered principal: it is never
            looked up as some other principal's.
        """
        try:
            principal = PRINCIPALS.get(admin_type)
            if principal is None or principal.find_admin is None:
                return False
            if not principal.platform_wide and not tenant_id:
                return False

            admin = principal.find_admin(db, admin_id)
            if admin is not None and not principal.platform_wide and admin.tenant_id != tenant_id:
                admin = None

            if not admin:
                return False

            lockout.clear(admin)
            admin.updated_at = datetime.now(timezone.utc)
            db.flush()

            self.logger.info(f"{principal.name.title()} admin unlocked", context={
                "admin_id": admin_id,
                "username": admin.username,
                "admin_type": admin_type
            })

            return True

        except Exception as e:
            self.logger.error("Failed to unlock admin", context={
                "admin_id": admin_id,
                "admin_type": admin_type,
                "error": str(e)
            }, exc_info=True)
            return False
