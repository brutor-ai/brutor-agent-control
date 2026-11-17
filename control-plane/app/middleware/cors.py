# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from app.services.config_manager import ConfigManager

"""
CORS middleware with enhanced structured logging, improved origin validation and error handling
"""


import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.logging_system import BrutorLogger


class CORSMiddleware(BaseHTTPMiddleware):
    """CORS middleware with enhanced logging - handles all CORS logic including preflight OPTIONS requests"""

    def __init__(self, app, config_manager: ConfigManager, logger: Optional[BrutorLogger] = None):
        super().__init__(app)
        self.logger = logger or BrutorLogger("cors-middleware")

        # The origins allowed: listed (BRUTOR_CORS_ORIGINS), never every one.
        self.allowed_origins = config_manager.get_cors_origins()

    def _is_origin_allowed(self, request_origin: str) -> bool:
        """Check if the origin is allowed with logging"""
        if not request_origin:
            self.logger.debug("Request without origin header - allowing", context={
                "operation": "check_origin",
                "origin": None,
                "allowed": True,
                "reason": "no_origin_header"
            })
            return True  # Allow requests without origin (like Postman, curl)

        is_allowed = request_origin in self.allowed_origins
        self.logger.debug("Origin check completed", context={
            "operation": "check_origin",
            "origin": request_origin,
            "allowed": is_allowed,
            "reason": "explicit_allowlist" if is_allowed else "not_in_allowlist"
        })

        return is_allowed

    def _get_allowed_origin_header(self, request_origin: str) -> Optional[str]:
        """Get the origin value to return in Access-Control-Allow-Origin header"""
        if not request_origin:
            # No origin header - return first allowed origin
            result = self.allowed_origins[0] if self.allowed_origins else "*"
            self.logger.debug("Generated origin header for no-origin request", context={
                "operation": "get_origin_header",
                "request_origin": None,
                "response_origin": result
            })
            return result

        if request_origin in self.allowed_origins:
            # Specific allowed origin - return it
            self.logger.debug("Using specific allowed origin", context={
                "operation": "get_origin_header",
                "request_origin": request_origin,
                "response_origin": request_origin
            })
            return request_origin

        # Disallowed origin - return None to not set the header
        self.logger.warning("Origin not allowed - no header will be set", context={
            "operation": "get_origin_header",
            "request_origin": request_origin,
            "response_origin": None,
            "reason": "disallowed_origin"
        })
        return None

    def _get_cors_headers(self, origin_header_value: Optional[str]) -> dict:
        """Get standard CORS headers with logging"""
        headers = {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS, PUT, PATCH, DELETE",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, X-Tenant-ID, X-Gateway-Authorization, mcp-protocol-version, mcp-session-id, x-requested-with, x-api-key, x-correlation-id",
            "Access-Control-Max-Age": "3600",  # 1 hour cache
            # Expose headers so JavaScript can read them
            # This is essential for OAuth discovery (www-authenticate header)
            "Access-Control-Expose-Headers": (
                "www-authenticate, "
                "WWW-Authenticate, "
                "Content-Type, "
                "Content-Length, "
                "X-Gateway-Request-Id, "
                "X-Auth-Type, "
                "X-User-Authenticated, "
                "X-Request-Type, "
                "MCP-Session-Id, "
                "X-Gateway-Mode"
            )
        }

        if origin_header_value:
            headers["Access-Control-Allow-Origin"] = origin_header_value

            # Only include credentials if not using wildcard origin
            if origin_header_value != "*":
                headers["Access-Control-Allow-Credentials"] = "true"

        self.logger.debug("CORS headers generated", context={
            "operation": "get_cors_headers",
            "origin_header": origin_header_value,
            "headers_count": len(headers),
            "credentials_enabled": headers.get("Access-Control-Allow-Credentials") == "true"
        })

        return headers

    async def dispatch(self, request: Request, call_next):
        """Handle CORS for all requests with comprehensive logging"""
        start_time = time.time()
        origin = request.headers.get("origin")
        method = request.method
        path = request.url.path

        self.logger.debug("CORS processing started", context={
            "operation": "cors_dispatch",
            "method": method,
            "path": path,
            "origin": origin or "none"
        })

        # Handle CORS preflight OPTIONS requests
        if method == "OPTIONS":
            preflight_start = time.time()

            if not self._is_origin_allowed(origin):
                duration_ms = (time.time() - preflight_start) * 1000
                self.logger.warning("CORS preflight rejected", context={
                    "operation": "cors_preflight",
                    "origin": origin,
                    "path": path,
                    "duration_ms": duration_ms,
                    "reason": "origin_not_allowed"
                })
                return JSONResponse(
                    {"error": "Origin not allowed", "origin": origin},
                    status_code=403
                )

            # Allow preflight
            origin_header = self._get_allowed_origin_header(origin)
            cors_headers = self._get_cors_headers(origin_header)

            duration_ms = (time.time() - preflight_start) * 1000
            self.logger.debug("CORS preflight successful", context={
                "operation": "cors_preflight",
                "origin": origin,
                "path": path,
                "duration_ms": duration_ms,
                "headers_added": len(cors_headers)
            })

            # Return 204 No Content for preflight as per spec
            return Response(status_code=204, headers=cors_headers)

        # Process the actual request
        try:
            response = await call_next(request)

            # Add CORS headers to response if origin is allowed
            if self._is_origin_allowed(origin):
                origin_header = self._get_allowed_origin_header(origin)
                if origin_header:  # Only add headers if we have a valid origin
                    cors_headers = self._get_cors_headers(origin_header)

                    for key, value in cors_headers.items():
                        response.headers[key] = value

                    duration_ms = (time.time() - start_time) * 1000
                    self.logger.debug("CORS headers added to response", context={
                        "operation": "cors_dispatch",
                        "method": method,
                        "path": path,
                        "origin": origin,
                        "duration_ms": duration_ms,
                        "status_code": response.status_code,
                        "headers_added": len(cors_headers)
                    })
                else:
                    duration_ms = (time.time() - start_time) * 1000
                    self.logger.debug("CORS headers not added - disallowed origin", context={
                        "operation": "cors_dispatch",
                        "method": method,
                        "path": path,
                        "origin": origin,
                        "duration_ms": duration_ms,
                        "reason": "disallowed_origin"
                    })
            else:
                duration_ms = (time.time() - start_time) * 1000
                self.logger.warning("CORS request blocked", context={
                    "operation": "cors_dispatch",
                    "method": method,
                    "path": path,
                    "origin": origin,
                    "duration_ms": duration_ms,
                    "reason": "origin_not_allowed"
                })

            return response

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            self.logger.error("Error in CORS middleware", context={
                "operation": "cors_dispatch",
                "method": method,
                "path": path,
                "origin": origin,
                "duration_ms": duration_ms,
                "error": str(e)
            }, exc_info=True)
            # Let the error propagate, but don't add CORS headers to error responses
            # from disallowed origins
            raise