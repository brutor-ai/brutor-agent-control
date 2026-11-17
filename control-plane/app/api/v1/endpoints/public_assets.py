# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Public asset routes — served without authentication.

These endpoints exist so the admin UI (which talks to the
control-plane directly in dev / ALB-routed in prod) can render
user avatars from the same canonical `/v1/assets/users/...`
URLs that the portal uses against the proxy.

The portal's gateway proxy has the matching routes at
`brutor-gateway-core/src/routes/icons.rs`; the two services
must serve the same paths so an `avatar_url` written by either
upload pipeline (Rust portal-auth or Python admin) renders in
both UIs without requiring host-aware URL building on the
client.

Public, ETag-cacheable — same threat model as MCP icons. A
user's photo is visible to anyone who can see them in audit
logs, chat history, or approval queues; pre-signing each render
would be more friction than the bytes are worth.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.photo_token import photo_token_ok
from app.database import get_db


def create_public_assets_router() -> APIRouter:
    router = APIRouter(prefix="/assets", tags=["public-assets"])

    @router.get(
        "/users/end-user/{user_id}/photo",
        summary="End-user profile photo",
        description="Serve an end-user's profile photo (public, cacheable).",
    )
    def get_end_user_photo(
        user_id: int,
        request: Request,
        h: Optional[str] = Query(None, description="Photo capability token (prefix of photo_hash)"),
        db: Session = Depends(get_db),
    ) -> Response:
        from app.database.models.end_user import EndUser

        user = db.query(EndUser).filter(EndUser.id == user_id).first()
        # Capability check: require the `?h=` token (a prefix of photo_hash)
        # so this public route can't be used to enumerate photos by integer
        # id or scrape other tenants'. 404 (not 403) so we don't reveal which
        # ids exist.
        if not user or not user.photo_data or not photo_token_ok(user.photo_hash, h):
            raise HTTPException(404, "Photo not found")

        etag = user.photo_hash or ""
        if_none_match = request.headers.get("if-none-match", "")
        # Tolerate both quoted ("abc") and bare (abc) ETag forms —
        # browsers send quoted, curl scripts often don't.
        if etag and if_none_match.strip('"') == etag:
            return Response(status_code=304)

        return Response(
            content=user.photo_data,
            media_type=user.photo_mime_type or "image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "ETag": etag,
            },
        )

    @router.get(
        "/users/admin/{admin_id}/photo",
        summary="Admin user profile photo",
        description="Serve an admin user's profile photo (public, cacheable).",
    )
    def get_admin_user_photo(
        admin_id: str,
        request: Request,
        h: Optional[str] = Query(None, description="Photo capability token (prefix of photo_hash)"),
        db: Session = Depends(get_db),
    ) -> Response:
        from app.database.models.admin import TenantAdmin

        admin = db.query(TenantAdmin).filter(TenantAdmin.id == admin_id).first()
        if not admin or not admin.photo_data or not photo_token_ok(admin.photo_hash, h):
            raise HTTPException(404, "Photo not found")

        etag = admin.photo_hash or ""
        if_none_match = request.headers.get("if-none-match", "")
        if etag and if_none_match.strip('"') == etag:
            return Response(status_code=304)

        return Response(
            content=admin.photo_data,
            media_type=admin.photo_mime_type or "image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "ETag": etag,
            },
        )

    return router
