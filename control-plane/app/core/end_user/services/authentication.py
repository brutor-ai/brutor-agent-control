# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End User account service (multi-tenant)
The end-user accounts administrators manage, with full tenant isolation.
Business logic only - uses centralized database models. It issues no
session token: the Core Gateway signs end users in.

- All methods accept tenant_id parameter
- All queries include tenant_id filter
- Username/email uniqueness is tenant-scoped
- Comprehensive tenant logging
- Automatic role assignment on registration
"""

from typing import Optional, Dict, Any, List, Tuple

import bcrypt
from sqlalchemy.orm import Session

from app.database.models.end_user import EndUser
from app.core.logging_system import BrutorLogger


class EndUserAuthentication:
    """
    End-user account service with multi-tenant support: the accounts of
    end users who are not admins (create, read, update, delete, password
    hashes), each tenant-scoped. The Core Gateway signs them in.

    All operations are automatically tenant-isolated.
    New users are automatically assigned the "Tenant User" role.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        """
        Initialize the end user account service.

        It signs no session token: in this release only the Core Gateway
        issues the session tokens of end users, at a password sign-in.
        """
        self.logger = logger or BrutorLogger("end-user-auth")


    def _hash_password(self, password: str) -> str:
        """Hash password using bcrypt"""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except Exception as e:
            self.logger.error("Password verification error", context={
                "error": str(e)
            }, exc_info=True)
            return False

    async def list_gateway_users(
        self,
        tenant_id: str,
        db: Session,
        search: Optional[str] = None,
        active_only: bool = False,
        verified_only: bool = False,
        limit: int = 100,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        List users in specified tenant with filtering and pagination

        Args:
            tenant_id: Tenant ID to filter by
            db: Database session
            search: Optional search term
            active_only: Only return active users
            verified_only: Only return verified users
            limit: Maximum results
            offset: Pagination offset

        Returns:
            Tuple of (user list, total count)
        """
        # Start with tenant filter
        query = db.query(EndUser).filter(
            EndUser.tenant_id == tenant_id
        )

        if search:
            search_filter = f"%{search}%"
            query = query.filter(
                (EndUser.username.like(search_filter)) |
                (EndUser.email.like(search_filter))
            )

        if active_only:
            query = query.filter(EndUser.is_active == True)

        if verified_only:
            query = query.filter(EndUser.is_verified == True)

        # Get total count before pagination
        total = query.count()

        # Apply pagination
        users = query.order_by(EndUser.created_at.desc()).limit(limit).offset(offset).all()

        return [user.to_dict() for user in users], total

    async def update_gateway_user_status(
        self,
        user_id: int,
        tenant_id: str,
        is_active: bool,
        db: Session
    ) -> bool:
        """
        Update user active status within tenant

        Args:
            user_id: User ID
            tenant_id: Tenant ID (for verification)
            is_active: New active status
            db: Database session

        Returns:
            True if updated
        """
        try:
            user = db.query(EndUser).filter_by(
                id=user_id,
                tenant_id=tenant_id
            ).first()

            if not user:
                return False

            user.is_active = is_active
            db.flush()

            self.logger.info("User status updated", context={
                "user_id": user_id,
                "username": user.username,
                "tenant_id": tenant_id,
                "is_active": is_active
            })

            return True

        except Exception as e:
            self.logger.error("Failed to update user status", context={
                "user_id": user_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            return False

    async def delete_gateway_user_account(
        self,
        user_id: int,
        tenant_id: str,  
        db: Session
    ) -> bool:
        """
        Delete user account within tenant
        
        Args:
            user_id: User ID
            tenant_id: Tenant ID (for verification)
            db: Database session
            
        Returns:
            True if deleted
        """
        try:
            user = db.query(EndUser).filter_by(
                id=user_id,
                tenant_id=tenant_id  
            ).first()

            if not user:
                return False

            username = user.username
            db.delete(user)
            db.flush()

            self.logger.info("User account deleted", context={
                "user_id": user_id,
                "username": username,
                "tenant_id": tenant_id  
            })

            return True

        except Exception as e:
            self.logger.error("Failed to delete user", context={
                "user_id": user_id,
                "tenant_id": tenant_id,  
                "error": str(e)
            }, exc_info=True)
            return False

    async def get_gateway_user(
        self,
        user_id: int,
        tenant_id: str,
        db: Session
    ) -> Optional[Dict[str, Any]]:
        """
        Get Gateway user by ID within tenant

        Args:
            user_id: User ID
            tenant_id: Tenant ID
            db: Database session

        Returns:
            User data dictionary or None
        """
        user = db.query(EndUser).filter_by(
            id=user_id,
            tenant_id=tenant_id
        ).first()
        return user.to_dict() if user else None
