# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System MCP Server Constants

Stable identifiers and configuration for system-managed virtual MCP servers.
These servers exist in the database but are handled internally by the gateway
rather than forwarding to a real external server.

Interception pattern:
    Any MCPServer with base_url starting with SYSTEM_SERVER_URL_PREFIX
    is handled by the SystemMCPServerHandler instead of being proxied externally.
"""


# =============================================================================
# URL Convention
# =============================================================================
# System servers use the http://system/ prefix which is never a real network
# destination. The MCP proxy layer detects this prefix and routes to the
# internal handler instead of making an HTTP request.

#: The Agent Skills verb that RUNS a skill's script. Its tools/call rows carry
#: the skill's id; a call to it is a skill run attempt whether it ran, was
#: held for approval or was refused (see ai_system_connection_status).
SKILL_RUN_TOOL = "skills__run_script"


SYSTEM_SERVER_URL_PREFIX = "http://system/"


def is_system_server(base_url: str) -> bool:
    """Check if a server URL indicates a system-managed virtual server."""
    return base_url.startswith(SYSTEM_SERVER_URL_PREFIX)


def system_server_wire_identity():
    """
    (protocol_version, dialect) the gateway's synthesized system servers speak.

    System servers never go through network capability discovery — the only
    code path that stamps protocol_version/dialect on regular servers — so
    callers (seed + Refresh Capabilities) record the wire identity we know
    by construction: the Rust gateway is **dual-era**, answering
    both the legacy `initialize` handshake and the stateless 2026-07-28
    `server/discover` unconditionally. The badge reports the newest version
    (2026-07-28) with the `dual` dialect.
    """
    return "2026-07-28", "dual"


# =============================================================================
# Agent Skills Management Server
# =============================================================================

class AgentSkillServer:
    """Constants for the Agent Skills Management system MCP server."""

    # Stable ID — survives database wipes because seed is idempotent
    SERVER_ID = "system-agent-skill-server"
    CONFIG_ID = f"{SERVER_ID}-config"

    # Display
    NAME = "Agent Skills"
    DESCRIPTION = (
        "System MCP server for governed Agent Skills. Skills are "
        "procedural workflows — not tools — discovered via the initialize "
        "instructions catalog and consumed through the skills__* verbs "
        "(load / read_resource / run_script / render_template)."
    )

    # The server's real, governable TOOLS are the four
    # generic progressive-disclosure verbs (skills are NOT tools). These mirror
    # gateway-core `system_server::skills_generic_tool_definitions`; seeded into
    # `capabilities.tools` so operators can attach argument policies / per-tool
    # rate limits to e.g. `skills__run_script` in the admin UI.
    # MCP ToolAnnotations are pinned to brutor-gateway-core/tests/fixtures/
    # system_tool_annotations.json (agent_skills) (the core's live tools/list uses the same file).
    # The gateway classifies risk from this stored copy: an unannotated tool is
    # side-effecting, so a read without hints is blocked by approval_required /
    # restricted autonomy. run_script acts and deliberately claims no
    # destructiveHint=false.
    VERB_TOOLS = [
        {
            "name": "skills__list",
            "description": (
                "List the Agent Skills available to you (name + what each does). Call this "
                "first to discover which skills you can use, then skills__load one."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "skills__load",
            "description": (
                "Load an Agent Skill's full instructions (SKILL.md) and its manifest of "
                "available scripts, resources and templates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "Name of the skill to load."}
                },
                "required": ["skill_name"],
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "skills__read_resource",
            "description": "Read a bundled reference file (from a skill's manifest) into context.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill that owns the file."},
                    "path": {"type": "string", "description": "The resource filename, from the manifest."},
                },
                "required": ["skill_name", "path"],
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": SKILL_RUN_TOOL,
            "description": (
                "Run one of a skill's scripts (from its manifest) with arguments, in "
                "Brutor's governed sandbox, and return only its output."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill that owns the script."},
                    "script": {"type": "string", "description": "The script filename, from the manifest."},
                    "args": {"type": "object", "additionalProperties": True, "description": "Arguments for the script."},
                    "approval_token": {"type": "string", "description": "One-time token to retry an approval-required run."},
                },
                "required": ["skill_name", "script"],
            },
            "annotations": {"readOnlyHint": False},
        },
        {
            "name": "skills__render_template",
            "description": "Render one of a skill's templates/assets (from its manifest) with variables.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill that owns the template."},
                    "template": {"type": "string", "description": "The template filename, from the manifest."},
                    "vars": {"type": "object", "additionalProperties": True, "description": "Variables to substitute."},
                },
                "required": ["skill_name", "template"],
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        },
    ]

    # URL — never hit over the network; intercepted by the proxy layer
    BASE_URL = f"{SYSTEM_SERVER_URL_PREFIX}agent-skill-server"
    MCP_ENDPOINT_PATH = ""

    # Auth — system server, no external auth needed
    AUTH_TYPE = "none"

    # Server info returned during MCP initialize
    SERVER_INFO = {
        "name": "brutor-agent-skills",
        "version": "1.0.0",
        "protocolVersion": "2024-11-05",
    }

    # Tool name prefix — all skill tools are namespaced
    TOOL_PREFIX = "skill"

    @staticmethod
    def tool_name(skill_name: str) -> str:
        """
        Convert a skill name to its MCP tool name.

        Example: "query-sales-data" → "skill__query-sales-data"

        The double-underscore delimiter is intentional:
        - Unambiguous split point (skill names use single hyphens/underscores)
        - Common convention in MCP for namespace separation
        """
        return f"{AgentSkillServer.TOOL_PREFIX}__{skill_name}"

    @staticmethod
    def skill_name_from_tool(tool_name: str) -> str:
        """
        Extract skill name from MCP tool name.

        Example: "skill__query-sales-data" → "query-sales-data"

        Raises ValueError if tool_name doesn't match the expected pattern.
        """
        prefix = f"{AgentSkillServer.TOOL_PREFIX}__"
        if not tool_name.startswith(prefix):
            raise ValueError(
                f"Tool name '{tool_name}' does not start with '{prefix}'"
            )
        return tool_name[len(prefix):]


# =============================================================================
# Web Search System Server
# =============================================================================

class SearchServer:
    """Constants for the Web Search system MCP server."""

    SERVER_ID = "system-search-server"
    CONFIG_ID = f"{SERVER_ID}-config"

    NAME = "Web Search"
    DESCRIPTION = (
        "Built-in system MCP server providing web search, news search, "
        "content extraction, and image search. Queries are governed, "
        "audited, and rate-limited per tenant. Supports multiple upstream "
        "search providers (Google, Brave, SearXNG)."
    )

    BASE_URL = f"{SYSTEM_SERVER_URL_PREFIX}search-server"
    MCP_ENDPOINT_PATH = ""
    AUTH_TYPE = "none"

    SERVER_INFO = {
        "name": "brutor-web-search",
        "version": "1.0.0",
        "protocolVersion": "2024-11-05",
    }

    TOOL_PREFIX = "search"

    # Tool names
    TOOL_WEB = f"{TOOL_PREFIX}__web"
    TOOL_NEWS = f"{TOOL_PREFIX}__news"
    TOOL_EXTRACT = f"{TOOL_PREFIX}__extract"
    TOOL_IMAGES = f"{TOOL_PREFIX}__images"

    #: Every search tool reads the open web. Pinned to brutor-gateway-core/tests/
    #: fixtures/system_tool_annotations.json (web_search); the gateway classifies
    #: risk from the seeded copy.
    ANNOTATIONS = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}

    @staticmethod
    def tool_name(name: str) -> str:
        """Convert a search tool name to its MCP tool name.
        Example: "web" → "search__web"
        """
        return f"{SearchServer.TOOL_PREFIX}__{name}"


# =============================================================================
# Future system servers can be added here
# =============================================================================
# class WorkflowServer:
#     SERVER_ID = "system-workflow-server"
#     ...
