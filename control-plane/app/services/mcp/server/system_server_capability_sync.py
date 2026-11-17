# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System Server Capability Sync

Keeps the Agent Skills system MCP server's capabilities field in sync
with published skills. The capabilities field on MCPServer stores the
tool definitions that MCP clients see via tools/list.

Call sync_agent_skill_capabilities() whenever:
    - A skill is published (status → PUBLISHED)
    - A skill is unpublished (status → DRAFT/ARCHIVED/DEPRECATED)
    - A skill is enabled/disabled
    - A skill's metadata (description, input_schema) changes
    - A skill binding changes for a resource group

This is a write-through pattern: we eagerly rebuild the capabilities
so tools/list reads are fast (just return the JSON column).

Usage:
    from app.services.system_server_capability_sync import SystemServerCapabilitySync

    sync = SystemServerCapabilitySync(logger=logger)
    sync.sync_agent_skill_capabilities(db, tenant_id)

Integration points:
    - Call from admin agent_skills.py publish/unpublish/enable/disable endpoints
    - Call from AgentSkillOrchestrator after state transitions
    - Optionally hook into PluginManager SKILL_PUBLISHED / SKILL_UNPUBLISHED events
"""

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.constants.agent_skill import SkillStatus
from app.constants.system_server import AgentSkillServer
from app.database.models.mcp_server import MCPServer
from app.database.models.agent_skill import AgentSkill


class SystemServerCapabilitySync:
    """
    Synchronizes system MCP server capabilities with published Agent Skills.

    Each published skill becomes an MCP tool definition in the capabilities JSON.
    The tool definition follows the MCP spec format that clients expect from tools/list.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("system-server-cap-sync")

    def sync_agent_skill_capabilities(
        self,
        db: Session,
        tenant_id: str,
    ) -> int:
        """
        Rebuild the Agent Skills server's capabilities from all published skills.

        This replaces the entire capabilities.tools array — it's a full rebuild,
        not an incremental update. This is intentional: it's simpler, idempotent,
        and fast enough (skills are typically < 100 per tenant).

        Args:
            db: Database session
            tenant_id: Tenant to sync for

        Returns:
            Number of tools in the updated capabilities
        """
        # Get the system server. NOTE: the seeded id is tenant-suffixed
        # (`{SERVER_ID}-{tenant_id}`), so filtering on the bare SERVER_ID
        # silently matched nothing and skipped the sync — fixed here.
        server = db.query(MCPServer).filter(
            MCPServer.id == f"{AgentSkillServer.SERVER_ID}-{tenant_id}",
            MCPServer.tenant_id == tenant_id,
        ).first()

        if not server:
            self.logger.warning(
                "Agent Skills system server not found — skipping capability sync",
                context={"tenant_id": tenant_id}
            )
            return 0

        # Skills are NOT tools. The server's tools are the
        # four generic `skills__*` verbs (static). Publishing/unpublishing a
        # skill does NOT change the tool surface; it changes the catalog the
        # model sees in the initialize `instructions` (built live by the core).
        # Writing the verbs here surfaces them in the admin argument-policy /
        # per-tool rate-limit pickers so operators can govern them.
        tools = list(AgentSkillServer.VERB_TOOLS)

        server.capabilities = {
            "tools": tools,
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }
        server.discovered_at = datetime.now(timezone.utc)
        server.updated_at = datetime.now(timezone.utc)

        db.flush()

        self.logger.info("Agent Skills capabilities synced (skills__* verbs)", context={
            "tenant_id": tenant_id,
            "server_id": server.id,
            "tool_count": len(tools),
        })

        return len(tools)

    def sync_single_skill(
        self,
        db: Session,
        tenant_id: str,
        skill: AgentSkill,
    ) -> None:
        """
        Convenience method: full rebuild triggered by a single skill change.

        We always do a full rebuild rather than surgical insert/remove because:
        - It's idempotent and correct regardless of race conditions
        - The number of skills is small (< 100)
        - JSON column update is a single write either way
        """
        self.sync_agent_skill_capabilities(db, tenant_id)

    # ========================================================================
    # Tool Definition Builder
    # ========================================================================

    def _skill_to_tool_definition(self, skill: AgentSkill) -> Dict[str, Any]:
        """
        Convert an AgentSkill to an MCP tool definition.

        This is Progressive Disclosure Level 2: the tool schema.
        Level 1 (name + description) comes from the catalog.
        Level 3 (resources, scripts, assets) loads at execution time.

        The output matches the MCP spec format:
        {
            "name": "skill__query-sales-data",
            "description": "Query sales data from the CRM with date filtering",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date_range": {"type": "string", "description": "..."},
                    "output_format": {"type": "string", "enum": ["csv", "json"]}
                },
                "required": ["date_range"]
            }
        }
        """
        tool_name = AgentSkillServer.tool_name(skill.name)

        # Build description with metadata enrichment
        description_parts = []

        if skill.description:
            description_parts.append(skill.description)

        # Add category and tags as hints for the LLM
        category = skill.get_metadata_value("category")
        if category:
            description_parts.append(f"Category: {category}")

        tags = skill.get_tags()
        if tags:
            description_parts.append(f"Tags: {', '.join(tags)}")

        # Add cost/approval hints so the LLM can make informed decisions
        if skill.is_approval_required():
            description_parts.append(
                "⚠️ This skill requires human approval before execution."
            )

        cost_estimate = skill.get_cost_estimate() if hasattr(skill, 'get_cost_estimate') else None
        if cost_estimate and cost_estimate > 0:
            description_parts.append(
                f"Estimated cost: ${cost_estimate:.4f} per execution."
            )

        description = " | ".join(description_parts) if description_parts else tool_name

        # Build input schema from skill metadata
        input_schema = self._build_input_schema(skill)

        return {
            "name": tool_name,
            "description": description,
            "inputSchema": input_schema,
        }

    def _build_input_schema(self, skill: AgentSkill) -> Dict[str, Any]:
        """
        Build the JSON Schema for the tool's input parameters.

        Sources (in priority order):
        1. skill.metadata.input_schema — explicit JSON Schema from SKILL.md frontmatter
        2. skill.metadata.parameters — simplified parameter definitions
        3. Fallback — generic schema accepting any input_params object

        The schema always includes the skill execution envelope fields
        (group_id, correlation_id) as optional properties alongside the
        skill-specific parameters.
        """
        # Try explicit input_schema first
        explicit_schema = skill.get_metadata_value("input_schema")
        if explicit_schema and isinstance(explicit_schema, dict):
            # Merge envelope fields into explicit schema
            return self._merge_envelope_fields(explicit_schema)

        # Try simplified parameters definition
        parameters = skill.get_metadata_value("parameters")
        if parameters and isinstance(parameters, list):
            return self._parameters_to_schema(parameters)

        # Fallback: accept anything via input_params
        return {
            "type": "object",
            "properties": {
                "input_params": {
                    "type": "object",
                    "description": "Skill-specific input parameters",
                    "additionalProperties": True,
                },
                "group_id": {
                    "type": "string",
                    "description": "Optional: Target Resource Group ID (auto-resolved if omitted)",
                },
            },
            "required": [],
        }

    def _parameters_to_schema(
        self,
        parameters: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Convert simplified parameter definitions to JSON Schema.

        Input format (from SKILL.md frontmatter):
        parameters:
          - name: date_range
            type: string
            description: Date range filter
            required: true
          - name: output_format
            type: string
            description: Output format
            enum: [csv, json]
            default: json

        Output: standard JSON Schema
        """
        properties = {}
        required = []

        for param in parameters:
            name = param.get("name")
            if not name:
                continue

            prop: Dict[str, Any] = {}

            # Type
            param_type = param.get("type", "string")
            prop["type"] = param_type

            # Description
            if param.get("description"):
                prop["description"] = param["description"]

            # Enum
            if param.get("enum"):
                prop["enum"] = param["enum"]

            # Default
            if "default" in param:
                prop["default"] = param["default"]

            # Min/max for numbers
            if param.get("minimum") is not None:
                prop["minimum"] = param["minimum"]
            if param.get("maximum") is not None:
                prop["maximum"] = param["maximum"]

            properties[name] = prop

            if param.get("required", False):
                required.append(name)

        schema = self._merge_envelope_fields({
            "type": "object",
            "properties": properties,
            "required": required,
        })

        return schema

    def _merge_envelope_fields(
        self,
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Add standard envelope fields (group_id, correlation_id) to a schema.

        These are always optional and allow callers to override defaults
        without polluting the skill-specific parameter space.
        """
        result = schema.copy()
        props = result.get("properties", {})

        # Only add if not already present (skill author may have defined them)
        if "group_id" not in props:
            props["group_id"] = {
                "type": "string",
                "description": (
                    "Optional: Target Resource Group ID. "
                    "Auto-resolved from auth context if omitted."
                ),
            }

        if "correlation_id" not in props:
            props["correlation_id"] = {
                "type": "string",
                "description": (
                    "Optional: Correlation ID for request tracing "
                    "and workflow chaining."
                ),
            }

        result["properties"] = props
        return result
