# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Two-Factor Authentication (2FA) endpoints for admin users.

Endpoints:
    POST /admin/users/me/2fa/setup    — Generate TOTP secret + QR code
    POST /admin/users/me/2fa/verify   — Verify code, enable 2FA, return backup codes
    POST /admin/users/me/2fa/disable  — Disable 2FA (requires TOTP code)
    GET  /admin/users/me/2fa/status   — Check 2FA status
    POST /admin/users/login/2fa       — Complete login with TOTP code
    POST /admin/users/login/2fa/enroll/setup   — Enrol at sign-in: QR code
    POST /admin/users/login/2fa/enroll/verify  — Enrol at sign-in: verify, sign in

The last two exist for the tenant's two-factor policy: when it requires
2FA of administrators, a password sign-in of one without it issues no session
but a `2fa_enrollment` token, and these two routes are the only thing that
token opens. They run the same setup and verification as the signed-in pair.
"""

from app.core.password_rule import NewPassword
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.database import get_db
from app.constants.admin import AdminType
from app.database.models.admin import TenantAdmin
from app.composition.principals import PRINCIPALS
from app.core import account_lock
from app.core.admin_user import lockout
from app.core.admin_user.dependencies import get_audit_logger, get_current_admin
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.admin_user.services.session_revocation import deactivate_admin_sessions
from app.services.mfa.policy import requires_2fa_for_admins, tenant_mfa_policy
from app.services.mfa.totp_service import (
    generate_totp_setup,
    seal_totp_secret,
    verify_sealed_totp_code,
    generate_backup_codes,
    verify_backup_code,
)
from app.middleware.trusted_proxies import client_ip


# An administrator row of whichever principal is calling. Every principal's
# rows carry the same set of TOTP and password columns, so the 2FA endpoints
# operate on any of them; the principal is asked only for the row itself.
AdminRow = Any


def _resolve_admin_row(admin, admin_type: str, db: Session) -> AdminRow:
    """Look up the live admin row matching the JWT-derived identity.

    Re-queries (rather than trusting the dependency-injected
    object) because the caller may mutate TOTP fields in place and
    we want a session-attached row to commit. Raises 404 if the
    row no longer exists — covers the edge case where the admin
    was deleted between issuing the token and using it.

    The calling principal finds its own row. A type that is
    not a registered principal has none: 403, never some other principal's.
    """
    principal = PRINCIPALS.get(admin_type)
    if principal is None or principal.find_admin is None:
        raise HTTPException(403, "Unknown administrator type")
    row = principal.find_admin(db, admin.id)
    if not row:
        raise HTTPException(404, "Admin not found")
    return row


def _audit_self_service(
    audit: UnifiedAdminAuditLogger,
    db: Session,
    row: AdminRow,
    request: Optional[Request],
    *,
    action: str,
    success: bool = True,
    details: Optional[dict] = None,
) -> None:
    """Audit an admin self-service security action.

    The actor is the resolved admin row itself, so we pass it as
    `admin=` and let the logger file it in its principal's trail. `details` MUST never carry TOTP secrets,
    codes, backup codes, or passwords — booleans/usernames only.
    """
    audit.log(
        action=action,
        db=db,
        admin=row,
        success=success,
        resource_type="admin_user",
        resource_id=str(row.id),
        details=details or {},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent") if request else None,
        request_method=request.method if request else None,
        request_path=str(request.url.path) if request else None,
    )


# ── Enrolment, shared by the signed-in routes and the sign-in routes ──

def begin_totp_setup(
    row: AdminRow, db: Session, audit: UnifiedAdminAuditLogger, request: Optional[Request]
) -> dict:
    """Generate a TOTP secret for `row`, store it sealed and not yet enabled,
    and return what the authenticator app needs. Refuses an account that
    already has 2FA: its secret is replaced only after it was disabled, which
    takes the current code."""
    if row.totp_enabled:
        raise HTTPException(409, "2FA is already enabled. Disable it first to reconfigure.")

    result = generate_totp_setup(
        username=row.username,
        email=row.email,
    )

    # Store secret temporarily (not yet enabled — user must verify first)
    row.totp_secret = seal_totp_secret(result.secret)
    row.totp_enabled = False
    db.commit()

    # No secret/QR material is logged — just that setup began.
    _audit_self_service(
        audit, db, row, request,
        action="mfa_2fa_setup",
        details={"username": row.username},
    )

    return {
        "secret": result.secret,
        "provisioning_uri": result.provisioning_uri,
        "qr_code": result.qr_code_base64,
    }


def require_totp_setup_in_progress(row: AdminRow) -> None:
    """A code can be verified only against a pending secret."""
    if not row.totp_secret:
        raise HTTPException(400, "No 2FA setup in progress. Call /setup first.")
    if row.totp_enabled:
        raise HTTPException(409, "2FA is already enabled")


def enable_totp(
    row: AdminRow, db: Session, audit: UnifiedAdminAuditLogger, request: Optional[Request]
) -> list:
    """Turn 2FA on for `row`, whose code the caller has just verified, and
    return the plaintext backup codes (shown once, stored hashed)."""
    plaintext_codes, hashed_codes = generate_backup_codes(10)

    row.totp_enabled = True
    row.totp_verified_at = datetime.now(timezone.utc)
    row.backup_codes = hashed_codes
    db.commit()

    # Log enablement — never the verification code or backup codes.
    _audit_self_service(
        audit, db, row, request,
        action="mfa_2fa_enable",
        details={"username": row.username, "enabled": True},
    )

    # Send confirmation email. Best-effort — a failing email
    # provider must not break enrollment, which would leave
    # 2FA "stuck enabled" with no way to confirm.
    try:
        from app.services.email import EmailService
        email_svc = EmailService()
        block_on(email_svc.send_2fa_enabled_email(row.email, row.full_name or row.username))
    except Exception:
        pass

    return plaintext_codes


# ── The second step of a sign-in, shared by the code and the enrolment ──


def decode_second_step_token(temp_token: str, expected_type: str, *, expired: str) -> dict:
    """The claims of a token a password sign-in handed out, if it is of
    `expected_type`; 401 otherwise. A `2fa_challenge` opens the code step and
    a `2fa_enrollment` opens the enrolment step, never the other way round:
    with one check for both, a token for enrolling could stand in for a code."""
    import jwt

    from app.env_vars import JWT_SECRET

    jwt_secret = JWT_SECRET.exact() or ""
    if not jwt_secret:
        raise HTTPException(500, "JWT not configured")
    try:
        payload = jwt.decode(temp_token, jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, expired)
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid 2FA token")
    if payload.get("type") != expected_type:
        raise HTTPException(401, "Invalid token type")
    return payload


def refuse_locked_account(
    row: AdminRow, db: Session, audit: UnifiedAdminAuditLogger, request: Request,
    *, action: str, admin_type: str,
) -> None:
    """423 for an account that is locked or deactivated; the answer says
    which (`app/core/account_lock.py`)."""
    if lockout.release_if_expired(row) or not row.is_active:
        _audit_self_service(
            audit, db, row, request,
            action=action,
            success=False,
            details={
                "username": row.username, "admin_type": admin_type,
                "reason": "locked" if row.is_active else "deactivated",
            },
        )
        raise account_lock.refusal(row)


def count_wrong_code(
    row: AdminRow, db: Session, audit: UnifiedAdminAuditLogger, request: Request,
    *, action: str, admin_type: str,
) -> None:
    """A wrong second-step code: count it on the counter the password path
    uses, lock the account at the threshold, and refuse (401, or 423 once
    locked). Always raises."""
    locked_now = lockout.count_failure(row, db, max_attempts=MAX_2FA_ATTEMPTS)
    _audit_self_service(
        audit, db, row, request,
        action=action,
        success=False,
        details={
            "username": row.username,
            "admin_type": admin_type,
            "failed_attempts": row.failed_login_attempts,
            "locked": locked_now,
        },
    )
    if locked_now:
        raise account_lock.refusal(row)
    raise HTTPException(401, "Invalid 2FA code")


# ── Request/Response Models ─────────────────────────────────────

class TotpVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8, description="6-digit TOTP code")

class TotpDisableRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8, description="Current TOTP code to confirm")

class TwoFactorLoginRequest(BaseModel):
    temp_token: str = Field(..., description="Temporary token from password login")
    code: str = Field(..., min_length=6, max_length=10, description="TOTP code or backup code")


class EnrollmentSetupRequest(BaseModel):
    temp_token: str = Field(
        ..., description="The `2fa_enrollment` token a password sign-in returned with `requires_2fa_enrollment`"
    )


class EnrollmentVerifyRequest(BaseModel):
    temp_token: str = Field(
        ..., description="The `2fa_enrollment` token a password sign-in returned with `requires_2fa_enrollment`"
    )
    code: str = Field(..., min_length=6, max_length=8, description="6-digit TOTP code from the authenticator app")


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(
        ..., min_length=1,
        description="The admin's current password — required to prevent drive-by password changes from a hijacked session."
    )
    new_password: NewPassword


# ── Router ──────────────────────────────────────────────────────

def create_mfa_router() -> APIRouter:
    router = APIRouter(
        prefix="/admin/users/me/2fa",
        tags=["Two-Factor Authentication"],
    )

    @router.get("/status", summary="Get 2FA status")
    def get_2fa_status(
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
    ):
        admin, admin_type = admin_and_type
        row = _resolve_admin_row(admin, admin_type, db)

        return {
            "enabled": row.totp_enabled or False,
            "verified_at": row.totp_verified_at.isoformat() if row.totp_verified_at else None,
            "has_backup_codes": bool(row.backup_codes),
        }

    @router.post("/setup", summary="Start 2FA setup — generate QR code")
    def setup_2fa(
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        row = _resolve_admin_row(admin, admin_type, db)

        return begin_totp_setup(row, db, audit, request)

    @router.post("/verify", summary="Verify TOTP code and enable 2FA")
    def verify_and_enable_2fa(
        req: TotpVerifyRequest,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        row = _resolve_admin_row(admin, admin_type, db)

        require_totp_setup_in_progress(row)

        # Verify the TOTP code
        if not verify_sealed_totp_code(row.totp_secret, req.code):
            raise HTTPException(401, "Invalid verification code. Please try again.")

        plaintext_codes = enable_totp(row, db, audit, request)

        return {
            "enabled": True,
            "backup_codes": plaintext_codes,
            "message": "2FA enabled. Save these backup codes — they won't be shown again.",
        }

    @router.post("/disable", summary="Disable 2FA")
    def disable_2fa(
        req: TotpDisableRequest,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        row = _resolve_admin_row(admin, admin_type, db)

        if not row.totp_enabled:
            raise HTTPException(400, "2FA is not enabled")

        # The tenant's policy requires 2FA of its administrators: it
        # cannot be switched off, or the next sign-in would only ask to enrol
        # it again. Refused before the code is looked at. A system
        # administrator is under no tenant's policy.
        if isinstance(row, TenantAdmin) and requires_2fa_for_admins(
            tenant_mfa_policy(db, row.tenant_id)
        ):
            raise HTTPException(
                409,
                "Two-factor authentication is required for administrators by your "
                "organization's security policy and cannot be disabled.",
            )

        # Verify current TOTP code before disabling — disabling is a
        # security-downgrade operation and must require the same
        # second factor it's removing.
        if not verify_sealed_totp_code(row.totp_secret, req.code):
            raise HTTPException(401, "Invalid TOTP code")

        row.totp_secret = None
        row.totp_enabled = False
        row.totp_verified_at = None
        row.backup_codes = None
        db.commit()

        # Security-downgrade event — log it (no code material).
        _audit_self_service(
            audit, db, row, request,
            action="mfa_2fa_disable",
            details={"username": row.username, "enabled": False},
        )

        return {"enabled": False, "message": "2FA has been disabled."}

    return router


def create_me_router() -> APIRouter:
    """
    Self-service endpoints for the currently-authenticated admin —
    things that should require the caller to already know their
    current password (so a hijacked session cannot silently change the
    password and lock out the real user).
    """
    import bcrypt
    router = APIRouter(prefix="/admin/users/me", tags=["Admin Self-Service"])

    @router.post(
        "/change-password",
        summary="Change the current admin's own password",
        description=(
            "Requires the current password. Only changes the caller's "
            "own password — admins updating other admins still use "
            "PATCH /v1/admin/users/tenant/{id}. A wrong current password "
            "counts as a failed sign-in does; the one that reaches the limit "
            "locks the account and is answered 423."
        ),
        responses={
            400: {"description": "The new password equals the current one"},
            401: {"description": "Not signed in, or the current password is wrong"},
            **account_lock.RESPONSES,
        },
    )
    def change_my_password(
        req: ChangePasswordRequest,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        # Works for every principal — `_resolve_admin_row` asks it for
        # its row. Sysadmin used to be rejected with
        # "Use the system admin password flow", but no such flow ever
        # existed, so the most-privileged identity on the platform
        # had no way to rotate its password from the UI. Now both
        # types share this endpoint.
        row = _resolve_admin_row(admin, admin_type, db)

        # Re-authenticate with the current password. bcrypt.checkpw
        # raises on malformed hashes, so guard it. bcrypt (cost 12) is
        # ~80ms of pure CPU; this is a threadpool handler, so the verify
        # never touches the event loop.
        def _verify_current() -> bool:
            try:
                return bcrypt.checkpw(
                    req.current_password.encode("utf-8"),
                    row.password_hash.encode("utf-8"),
                )
            except Exception:
                return False

        # The current password is a secret the caller must know, like the
        # password at sign-in: a locked account is refused before it is
        # looked at (423, as the second step of a sign-in does: the caller is
        # signed in, so the answer tells nobody anything about an account),
        # and a wrong one counts on the same counter — a stolen
        # session is otherwise an unlimited password oracle.
        refuse_locked_account(
            row, db, audit, request, action="admin_password_change", admin_type=getattr(admin_type, "value", admin_type)
        )
        ok = _verify_current()
        if not ok:
            locked_now = lockout.count_failure(row, db)
            _audit_self_service(
                audit, db, row, request,
                action="admin_password_change",
                success=False,
                details={
                    "username": row.username,
                    "reason": "wrong current password",
                    "failed_attempts": row.failed_login_attempts,
                    "locked": locked_now,
                },
            )
            if locked_now:
                raise account_lock.refusal(row)
            raise HTTPException(401, "Current password is incorrect")
        lockout.clear(row)

        # Don't let the new password equal the old — cheap check that
        # catches accidental paste-into-both-fields and nudges toward a
        # real rotation.
        if req.current_password == req.new_password:
            raise HTTPException(400, "New password must differ from current password")

        # Every administrator row exposes set_password() with
        # the same bcrypt-hash-and-stamp-updated_at semantics. The hash is
        # another bcrypt cost-12 op — offload it too before the commit.
        row.set_password(req.new_password)
        # A generated first password has now been replaced.
        if getattr(row, "password_change_required", False):
            row.password_change_required = False
        # A stolen token must not outlive the password. This session stays.
        deactivate_admin_sessions(
            db, row, "password_changed",
            keep_session_id=getattr(request.state, "admin_session_id", None),
        )
        db.commit()

        # Audit the password rotation — never the old/new password.
        _audit_self_service(
            audit, db, row, request,
            action="admin_password_change",
            details={"username": row.username},
        )

        return {"ok": True, "message": "Password changed."}

    return router


# Wrong second-factor codes before the account locks. Same threshold as
# `UnifiedAuthService.max_failed_attempts` for passwords — one counter.
MAX_2FA_ATTEMPTS = lockout.MAX_FAILED_ATTEMPTS


def create_2fa_login_router() -> APIRouter:
    """Separate router for the 2FA login challenge (mounted under /admin/users)."""
    router = APIRouter(prefix="/admin/users", tags=["Authentication"])

    @router.post("/login/2fa", summary="Complete login with 2FA code", responses=account_lock.RESPONSES)
    def login_with_2fa(
        req: TwoFactorLoginRequest,
        request: Request,
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """
        Second step of 2FA login.

        After password verification returns requires_2fa=true with a temp_token,
        the client calls this endpoint with the temp_token and a TOTP/backup code.
        Handles every registered principal — the admin_type field
        baked into the challenge JWT names the principal, which looks up
        its administrator and issues its session.
        """
        import jwt

        # Decode the temp token: a `2fa_challenge` and nothing else.
        payload = decode_second_step_token(
            req.temp_token, "2fa_challenge", expired="2FA challenge expired. Please log in again."
        )
        admin_id = payload.get("admin_id")

        # The challenge names the principal that began the sign-in, and only
        # that principal completes it: it finds its own
        # administrator and, below, issues its own session. A challenge for
        # a type that is not registered — a missing claim, an unknown name,
        # the system administrator where the installation has none — is
        # nobody's: 401. It is never completed as some other principal's;
        # this used to read "system, or else a tenant administrator".
        principal = PRINCIPALS.get(payload.get("admin_type"))
        if principal is None or principal.find_admin is None or principal.complete_sign_in is None:
            raise HTTPException(401, "Invalid 2FA token")
        admin_type = principal.name

        row = principal.find_admin(db, admin_id)
        if not row or not row.totp_enabled:
            raise HTTPException(401, "Invalid 2FA state")

        # SECURITY: a 6-digit code with a ±1-step window is three valid
        # values in a million. This endpoint used to accept unlimited
        # guesses for the five-minute life of the challenge, and the
        # password step reset the failure counter on every new challenge,
        # so a leaked password was a full compromise in minutes of
        # scripted traffic. Wrong codes now count against the same
        # `failed_login_attempts` the password path uses and lock the
        # account at the same threshold; the password path no longer
        # clears the counter while a second factor is still owed.
        refuse_locked_account(row, db, audit, request, action="admin_login_2fa", admin_type=admin_type)

        # Try TOTP code first; fall through to backup codes if the
        # input doesn't look like a 6-digit TOTP.
        code = req.code.strip()
        verified = False

        if len(code) == 6 and code.isdigit():
            verified = verify_sealed_totp_code(row.totp_secret, code)
        else:
            if row.backup_codes:
                idx = verify_backup_code(code, row.backup_codes)
                if idx is not None:
                    # Remove used backup code — single-use semantics.
                    codes = list(row.backup_codes)
                    codes.pop(idx)
                    row.backup_codes = codes
                    db.flush()
                    verified = True

        if not verified:
            count_wrong_code(row, db, audit, request, action="admin_login_2fa", admin_type=admin_type)

        # Update last login — happens here (not at password verify)
        # so a leaked password without the second factor never shows
        # up as "last_login just now" in the audit trail. The failure
        # counter is cleared here, and only here, for 2FA accounts.
        row.last_login = datetime.now(timezone.utc)
        lockout.clear(row)
        db.commit()

        # Audit the successful 2FA login challenge. We only reach here
        # when `verified` is True (the failure path raised 401 above),
        # so success is unconditionally True. No code material logged.
        _audit_self_service(
            audit, db, row, request,
            action="admin_login_2fa",
            success=True,
            details={"username": row.username, "admin_type": admin_type},
        )

        from app.core.admin_user.dependencies import get_auth_service
        auth_service = get_auth_service()

        # The principal issues its own session (and persists its session
        # row: the JWT alone is not enough — the jti must resolve to an
        # active session in verify_token) and answers.
        return principal.complete_sign_in(
            auth_service,
            row,
            payload,
            db,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    # ── Enrolment at sign-in ────────────────────────────────────────
    #
    # Unauthenticated like the route above, and like it opened by a token the
    # password step handed out, here a `2fa_enrollment` one: the tenant's
    # policy requires 2FA of administrators, this one has none, and no session
    # was issued. Wrong codes count on the same `failed_login_attempts` and
    # lock the account at the same threshold as the code step; a locked or
    # deactivated account is refused before anything else is done.

    def enrolling_admin(
        temp_token: str, db: Session, audit: UnifiedAdminAuditLogger, request: Request
    ) -> TenantAdmin:
        """The tenant admin a `2fa_enrollment` token names, if they may enrol."""
        payload = decode_second_step_token(
            temp_token,
            "2fa_enrollment",
            expired="The time to set up two-factor authentication ran out. Please sign in again.",
        )
        # Only a tenant admin is ever handed one: a system administrator is
        # under no tenant's policy. The row is looked up with the tenant the
        # token names, so the two claims have to agree.
        if payload.get("admin_type") != AdminType.TENANT_ADMIN_TYPE.value or not payload.get("tenant_id"):
            raise HTTPException(401, "Invalid 2FA token")
        row = (
            db.query(TenantAdmin)
            .filter(TenantAdmin.id == payload.get("admin_id"), TenantAdmin.tenant_id == payload.get("tenant_id"))
            .first()
        )
        if not row:
            raise HTTPException(401, "Invalid 2FA state")
        refuse_locked_account(
            row, db, audit, request,
            action="admin_login_2fa_enrollment", admin_type=AdminType.TENANT_ADMIN_TYPE.value,
        )
        # The token enrols a FIRST factor. On an account that has one it is
        # spent (or was never owed): replacing a secret takes the current code.
        if row.totp_enabled:
            raise HTTPException(409, "Two-factor authentication is already enabled. Please sign in again.")
        return row

    @router.post(
        "/login/2fa/enroll/setup",
        summary="Enrol in 2FA at sign-in — generate QR code",
        description=(
            "First step of the enrolment a password sign-in asks for with "
            "`requires_2fa_enrollment` (the tenant's two-factor policy requires "
            "2FA of administrators and this one has none). Takes the `temp_token` "
            "of that answer; returns what `POST /admin/users/me/2fa/setup` returns."
        ),
responses=account_lock.RESPONSES,
    )
    def enroll_setup(
        req: EnrollmentSetupRequest,
        request: Request,
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        row = enrolling_admin(req.temp_token, db, audit, request)
        return begin_totp_setup(row, db, audit, request)

    @router.post(
        "/login/2fa/enroll/verify",
        summary="Enrol in 2FA at sign-in — verify the code and sign in",
        description=(
            "Second step: verifies a code from the authenticator app, enables "
            "2FA and completes the sign-in. Returns the response of a tenant "
            "admin sign-in (`access_token`, ...) plus `backup_codes`, which are "
            "shown this once."
        ),
responses=account_lock.RESPONSES,
    )
    def enroll_verify(
        req: EnrollmentVerifyRequest,
        request: Request,
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        row = enrolling_admin(req.temp_token, db, audit, request)
        require_totp_setup_in_progress(row)

        tenant_admin = AdminType.TENANT_ADMIN_TYPE.value
        if not verify_sealed_totp_code(row.totp_secret, req.code.strip()):
            count_wrong_code(
                row, db, audit, request, action="admin_login_2fa_enrollment", admin_type=tenant_admin
            )

        backup_codes = enable_totp(row, db, audit, request)

        # The sign-in is complete only now, as in the code step: last login
        # and the failure counter are settled here, not at the password.
        row.last_login = datetime.now(timezone.utc)
        lockout.clear(row)
        db.commit()

        _audit_self_service(
            audit, db, row, request,
            action="admin_login_2fa_enrollment",
            success=True,
            details={"username": row.username, "admin_type": tenant_admin},
        )

        from app.core.admin_user.dependencies import get_auth_service
        result = get_auth_service().issue_tenant_admin_login(
            row,
            row.tenant_id,
            db,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        return {**result, "backup_codes": backup_codes}

    return router
