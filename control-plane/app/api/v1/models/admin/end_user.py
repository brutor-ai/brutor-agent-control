# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-User Admin management Models
Pydantic models for end-user management by administrators
"""

from app.core.password_rule import NewPassword
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator, ConfigDict


class UserStatusUpdate(BaseModel):
    """Request model for updating user status"""

    model_config = ConfigDict(extra="forbid")
    is_active: bool = Field(..., description="Active status")


class CreateEndUserRequest(BaseModel):
    """Request model for creating an end user from the admin console."""
    username: str = Field(..., min_length=3, max_length=100, description="Login username")
    email: str = Field(..., description="Email address")
    password: NewPassword
    display_name: Optional[str] = Field(None, max_length=255, description="Display name")
    role_id: Optional[str] = Field(None, description="Role to assign")
    group_ids: Optional[List[str]] = Field(None, description="Resource groups to add user to")
    avatar_url: Optional[str] = Field(None, description="Profile photo URL")
    send_welcome_email: bool = Field(True, description="Send account-confirmation welcome email (never contains the password)")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        import re
        if not re.match(r'^[a-zA-Z0-9_\-\.]+$', v):
            raise ValueError("Username can only contain letters, numbers, hyphens, underscores, and dots")
        return v.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v.strip().lower()


class EndUserResponse(BaseModel):
    """Response model for end user information"""
    id: int
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    is_verified: bool
    is_active: bool
    avatar_url: Optional[str] = None
    totp_enabled: bool = False
    created_at: str
    last_login: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Primary resource group — active runtime scope for this user.
    # NULL only when the user has no group memberships.
    primary_group_id: Optional[str] = None


class UserDetailResponse(EndUserResponse):
    """Detailed user response with related data"""
    stats: Dict[str, Any] = Field(default_factory=dict)
