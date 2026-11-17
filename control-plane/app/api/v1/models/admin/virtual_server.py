# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"MCP Gateway virtual server endpoints models"

from typing import Optional, Dict, Any

from pydantic import BaseModel, Field, ConfigDict


class VirtualServerRequest(BaseModel):
    name: str = Field(..., description="Name of the virtual server")
    description: str = Field(..., description="Description of the virtual server")
    capabilities: Dict[str, list] = Field(..., description="Selected capabilities from different servers")


class VirtualServerUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, description="Name of the virtual server")
    description: Optional[str] = Field(None, description="Description of the virtual server")
    capabilities: Optional[Dict[str, list]] = Field(None, description="Selected capabilities from different servers")
    enabled: Optional[bool] = Field(None, description="Whether the virtual server is enabled")
    owner: Optional[str] = Field(None, description="Owner user ID (can only be changed by admins)")

class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[str] = None
    method: str
    params: Optional[Dict[str, Any]] = None
