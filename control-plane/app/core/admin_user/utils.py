#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from typing import Dict, Union, Optional


def extract_user_context(user_context: Optional[Union[Dict[str, str], object]]) -> Dict[str, str]:
    """
    Extract user context for logging from AdminUser object or similar

    Works with database-driven roles (role_id and role.name)
    """
    if user_context is None:
        return {}

    if hasattr(user_context, 'id') and hasattr(user_context, 'username'):
        # Get role information - database-driven roles
        role_id = getattr(user_context, 'role_id', None)
        role_name = None

        # Try to get role name from role relationship
        if hasattr(user_context, 'role') and user_context.role:
            role_name = user_context.role.name

        # Build role string for logging
        role_str = ""
        if role_name:
            role_str = role_name
        elif role_id:
            role_str = f"role:{role_id}"

        # Unset fields are left out: the responses that carry this declare
        # Dict[str, str], and a system admin has no role, so a None here made
        # every IdP route answer 500 to a system admin.
        context = {
            "user_id": str(user_context.id),
            "username": str(user_context.username),
            "user_email": getattr(user_context, 'email', None),
            "user_role_id": role_id,
            "user_role": role_str
        }
        return {key: value for key, value in context.items() if value is not None}
    elif isinstance(user_context, dict):
        # Get role from dict
        role_id = user_context.get("role_id")
        role_name = user_context.get("role_name")
        role_str = role_name or (f"role:{role_id}" if role_id else "")

        return {
            "user_id": user_context.get("id", user_context.get("user_id")),
            "username": user_context.get("username"),
            "user_email": user_context.get("email"),
            "user_role_id": role_id,
            "user_role": role_str
        }
    elif isinstance(user_context, str):
        return {"user_id": user_context}

    return {}