# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic models for Tenant Theme
"""

from typing import Optional
from pydantic import BaseModel, Field, validator, ConfigDict
import re

from app.constants.tenant import TenantDefaultTheme


class TenantThemeConfig(BaseModel):
    """Theme configuration for tenant branding"""
    primary_color: str = Field(
        TenantDefaultTheme.PRIMARY_COLOR.value,
        max_length=7,
        description="Primary brand color (hex format)"
    )
    secondary_color: str = Field(
        TenantDefaultTheme.SECONDARY_COLOR.value,
        max_length=7,
        description="Secondary brand color (hex format)"
    )
    background_color: str = Field(
        TenantDefaultTheme.BACKGROUND_COLOR.value,
        max_length=7,
        description="Main background color (hex format)"
    )
    surface_color: str = Field(
        TenantDefaultTheme.SURFACE_COLOR.value,
        max_length=7,
        description="Surface/card background color (hex format)"
    )
    text_primary: str = Field(
         TenantDefaultTheme.TEXT_PRIMARY_COLOR.value,
        max_length=7,
        description="Primary text color (hex format)"
    )
    text_secondary: str = Field(
         TenantDefaultTheme.TEXT_SECONDARY_COLOR.value,
        max_length=7,
        description="Secondary/muted text color (hex format)"
    )
    border_color: str = Field(
         TenantDefaultTheme.BORDER_COLOR.value,
        max_length=7,
        description="Border and divider color (hex format)"
    )
    logo_url: Optional[str] = Field(
        None,
        max_length=500,
        description="URL to custom logo image"
    )
    company_name: Optional[str] = Field(
        None,
        max_length=255,
        description="Company name override for branding"
    )
    favicon_url: Optional[str] = Field(
        None,
        max_length=500,
        description="URL to custom favicon"
    )

    @validator(
        'primary_color', 'secondary_color', 'background_color',
        'surface_color', 'text_primary', 'text_secondary', 'border_color'
    )
    def validate_hex_color(cls, v):
        """Validate hex color format (#RRGGBB)"""
        if v and not re.match(r'^#[0-9A-Fa-f]{6}$', v):
            raise ValueError(
                f"Invalid hex color format: {v}. Must be in format #RRGGBB (e.g., #FF5733)"
            )
        return v.upper() if v else v  # Normalize to uppercase

    @validator('logo_url', 'favicon_url')
    def validate_url(cls, v):
        """Validate URL format"""
        if v:
            if not (v.startswith('http://') or v.startswith('https://')):
                raise ValueError("URL must start with http:// or https://")
            # Additional validation: check for common issues
            if ' ' in v:
                raise ValueError("URL cannot contain spaces")
        return v

    @validator('company_name')
    def validate_company_name(cls, v):
        """Validate company name"""
        if v:
            # Remove excessive whitespace
            v = ' '.join(v.split())
            if len(v) < 2:
                raise ValueError("Company name must be at least 2 characters")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "primary_color": "#FF5733",
                "secondary_color": "#FFC300",
                "background_color": "#FFFFFF",
                "surface_color": "#F5F5F5",
                "text_primary": "#2C3E50",
                "text_secondary": "#7F8C8D",
                "border_color": "#BDC3C7",
                "logo_url": "https://example.com/logo.png",
                "company_name": "Acme Corporation",
                "favicon_url": "https://example.com/favicon.ico"
            }
        }


class TenantThemeUpdate(BaseModel):
    """Request model for updating tenant theme"""
    theme: TenantThemeConfig = Field(
        ...,
        description="Theme configuration to update. All fields are optional for partial updates."
    )

    class Config:
        extra = "forbid"
        json_schema_extra = {
            "example": {
                "theme": {
                    "primary_color": "#FF5733",
                    "company_name": "Acme Corporation",
                    "logo_url": "https://acme.com/logo.png"
                }
            }
        }


class TenantThemeResponse(BaseModel):
    """Response model for tenant theme"""
    tenant_id: str = Field(..., description="Tenant ID")
    tenant_name: str = Field(..., description="Tenant display name")
    theme: TenantThemeConfig = Field(..., description="Theme configuration")
    is_custom: bool = Field(
        ...,
        description="Whether tenant has customized their theme (not using all defaults)"
    )

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "tenant_id": "tenant-123",
                "tenant_name": "Acme Corporation",
                "theme": {
                    "primary_color": "#FF5733",
                    "secondary_color": "#FFC300",
                    "background_color": "#FFFFFF",
                    "surface_color": "#F5F5F5",
                    "text_primary": "#2C3E50",
                    "text_secondary": "#7F8C8D",
                    "border_color": "#BDC3C7",
                    "logo_url": "https://example.com/logo.png",
                    "company_name": "Acme Corporation",
                    "favicon_url": "https://example.com/favicon.ico"
                },
                "is_custom": True
            }
        }


class TenantThemePresetResponse(BaseModel):
    """Response model for theme presets (for future enhancement)"""
    preset_id: str = Field(..., description="Preset identifier")
    name: str = Field(..., description="Preset display name")
    description: str = Field(..., description="Preset description")
    theme: TenantThemeConfig = Field(..., description="Theme configuration")
    preview_image_url: Optional[str] = Field(None, description="Preview image URL")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")

    class Config:
        json_schema_extra = {
            "example": {
                "preset_id": "modern-blue",
                "name": "Modern Blue",
                "description": "Clean and professional blue theme",
                "theme": {
                    "primary_color": "#2563EB",
                    "secondary_color": "#60A5FA",
                    "background_color": "#FFFFFF",
                    "surface_color": "#F9FAFB",
                    "text_primary": "#111827",
                    "text_secondary": "#6B7280",
                    "border_color": "#E5E7EB"
                },
                "preview_image_url": "https://example.com/previews/modern-blue.png",
                "tags": ["professional", "corporate", "cool"]
            }
        }


# =============================================================================
# Multi-variant theme (admin/portal × light/dark)
# =============================================================================
#
# Shape used by `/v1/admin/tenants/{id}/theme-variants` and
# `/v1/public/tenants/{id}/theme`. Each variant carries the same nine
# fields as the legacy single-theme `TenantThemeConfig`, so the per-
# variant editor reuses validation. `company_name` lives on the
# wrapper because it's shared across all four surfaces.


class TenantThemeVariant(BaseModel):
    """One of admin_light / admin_dark / portal_light / portal_dark."""

    primary_color: Optional[str] = Field(None, max_length=7)
    secondary_color: Optional[str] = Field(None, max_length=7)
    background_color: Optional[str] = Field(None, max_length=7)
    surface_color: Optional[str] = Field(None, max_length=7)
    text_primary: Optional[str] = Field(None, max_length=7)
    text_secondary: Optional[str] = Field(None, max_length=7)
    border_color: Optional[str] = Field(None, max_length=7)
    logo_url: Optional[str] = Field(None, max_length=500)
    favicon_url: Optional[str] = Field(None, max_length=500)

    @validator(
        'primary_color', 'secondary_color', 'background_color',
        'surface_color', 'text_primary', 'text_secondary', 'border_color',
    )
    def _hex(cls, v):
        if v is None or v == '':
            return None
        if not re.match(r'^#[0-9A-Fa-f]{6}$', v):
            raise ValueError(
                f"Invalid hex color {v!r}; expected #RRGGBB."
            )
        return v.upper()

    @validator('logo_url', 'favicon_url')
    def _url(cls, v):
        if v is None or v == '':
            return None
        if not (v.startswith('http://') or v.startswith('https://')):
            raise ValueError("URL must start with http:// or https://")
        if ' ' in v:
            raise ValueError("URL cannot contain spaces")
        return v


class TenantThemeVariantsPayload(BaseModel):
    """Request body for `PUT /theme-variants`. All four variants
    optional — operators can ship a partial payload (e.g. only the
    dark variants) and the missing variants stay as-is on the row.

    Field names match what `Tenant.theme_variants` stores.
    """

    model_config = ConfigDict(extra="forbid")

    company_name: Optional[str] = Field(None, max_length=255)
    admin_light: Optional[TenantThemeVariant] = None
    admin_dark: Optional[TenantThemeVariant] = None
    portal_light: Optional[TenantThemeVariant] = None
    portal_dark: Optional[TenantThemeVariant] = None

    @validator('company_name')
    def _company(cls, v):
        if v is None:
            return v
        v = ' '.join(v.split())
        if v and len(v) < 2:
            raise ValueError("Company name must be at least 2 characters")
        return v or None


class TenantThemeVariantsResponse(BaseModel):
    """Response for both `GET /admin/.../theme-variants` and the
    public `GET /public/.../theme` endpoint. Shape:

        { tenant_id, company_name,
          variants: { admin_light: {...9...}, admin_dark: {...},
                      portal_light: {...},   portal_dark: {...} } }

    Variants always return the *merged* values (per-variant
    overrides → admin_light legacy-column fallback → default
    palette). Clients never have to handle a "null variant".
    """

    tenant_id: str
    company_name: Optional[str] = None
    variants: dict = Field(
        ...,
        description="Map of variant name → resolved colour/url fields.",
    )

    class Config:
        from_attributes = True


class TenantThemeValidationResponse(BaseModel):
    """Response model for theme validation (for future enhancement)"""
    is_valid: bool = Field(..., description="Whether theme is valid")
    errors: list[str] = Field(default_factory=list, description="Validation errors")
    warnings: list[str] = Field(default_factory=list, description="Validation warnings")
    contrast_ratios: dict[str, float] = Field(
        default_factory=dict,
        description="WCAG contrast ratios for color combinations"
    )
    accessibility_score: int = Field(..., ge=0, le=100, description="Accessibility score (0-100)")

    class Config:
        json_schema_extra = {
            "example": {
                "is_valid": True,
                "errors": [],
                "warnings": [
                    "Primary color and background color contrast ratio is 2.1:1, which is below WCAG AA standard (4.5:1)"
                ],
                "contrast_ratios": {
                    "primary_on_background": 2.1,
                    "text_on_background": 18.5,
                    "text_on_surface": 15.2
                },
                "accessibility_score": 75
            }
        }