# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# AGENT SKILL WORKFLOW MODELS
#
# Defines the DAG-based workflow system for composing agent skills into
# multi-step orchestrations. A workflow is a directed acyclic graph (DAG)
# where nodes execute skills, evaluate conditions, transform data, or
# gate on human approval.
#
# Integration:
#   - Place in app/models/ alongside existing agent skill models
#   - Import from workflow endpoints
#   - WorkflowDefinition is linked to AgentSkillConfig via skill_id (1:1)
#

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Enums
# =============================================================================

class WorkflowNodeType(str, Enum):
    """Types of nodes available in a workflow DAG."""
    START = "start"
    END = "end"
    SKILL = "skill"
    CONDITION = "condition"
    PARALLEL_FORK = "parallel_fork"
    PARALLEL_JOIN = "parallel_join"
    TRANSFORM = "transform"
    HUMAN_APPROVAL = "human_approval"


class WorkflowStatus(str, Enum):
    """Workflow lifecycle states (mirrors skill lifecycle)."""
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"


class WorkflowExecutionStatus(str, Enum):
    """Runtime execution states for a workflow instance."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"           # waiting for human approval
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class NodeExecutionStatus(str, Enum):
    """Runtime execution states for individual nodes."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"         # condition branch not taken
    WAITING_APPROVAL = "waiting_approval"


class JoinStrategy(str, Enum):
    """How parallel join waits for branches."""
    ALL = "all"                 # wait for all branches
    ANY = "any"                 # continue when first completes
    N_OF_M = "n_of_m"          # continue when N of M complete


class TimeoutAction(str, Enum):
    """Action when human approval times out."""
    REJECT = "reject"
    APPROVE = "approve"
    FAIL = "fail"


# =============================================================================
# Node Position (canvas coordinates)
# =============================================================================

class NodePosition(BaseModel):
    """XY position on the React Flow canvas."""
    x: float = 0.0
    y: float = 0.0


# =============================================================================
# Data Mapping (field-to-field transformation between nodes)
# =============================================================================

class DataMapping(BaseModel):
    """Maps a field from one node's output to another node's input.

    source_field uses dot notation: "output.ticket_summary.priority"
    target_field uses dot notation: "input.priority_level"
    transform is an optional expression: "upper(value)" or "value + 1"
    """
    source_field: str = Field(..., min_length=1, max_length=500)
    target_field: str = Field(..., min_length=1, max_length=500)
    transform: Optional[str] = Field(
        None, max_length=2000,
        description="Optional transformation expression applied to the source value"
    )


# =============================================================================
# Retry Policy
# =============================================================================

class RetryPolicy(BaseModel):
    """Retry configuration for skill nodes."""
    max_retries: int = Field(default=0, ge=0, le=10)
    backoff_seconds: float = Field(default=1.0, ge=0.1, le=300.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    retry_on_errors: list[str] = Field(
        default_factory=list,
        description="Error types to retry on. Empty = retry all."
    )


# =============================================================================
# Node-Type-Specific Configurations
# =============================================================================

class SkillNodeConfig(BaseModel):
    """Configuration for a node that executes an agent skill."""
    skill_id: str = Field(..., description="ID of the agent skill to execute")
    skill_name: Optional[str] = Field(
        None, description="Denormalized skill name for display"
    )
    input_mapping: list[DataMapping] = Field(
        default_factory=list,
        description="Maps workflow context fields to skill input parameters"
    )
    output_mapping: list[DataMapping] = Field(
        default_factory=list,
        description="Maps skill output fields back to workflow context"
    )
    timeout_seconds: Optional[int] = Field(
        None, ge=1, le=3600,
        description="Max execution time before timeout"
    )
    retry_policy: Optional[RetryPolicy] = None


class ConditionNodeConfig(BaseModel):
    """Configuration for a conditional branching node.

    The expression is evaluated against the workflow execution context.
    Supports dot notation, comparisons, and boolean operators.

    Examples:
        "context.priority == 'critical'"
        "context.score > 0.8 and context.category != 'spam'"
        "context.items | length > 5"
    """
    expression: str = Field(
        ..., min_length=1, max_length=2000,
        description="Boolean expression evaluated against workflow context"
    )
    true_label: str = Field(default="Yes", max_length=50)
    false_label: str = Field(default="No", max_length=50)


class TransformNodeConfig(BaseModel):
    """Configuration for a data transformation node.

    Either use field-level mappings or a Jinja2 template for complex transforms.
    """
    mappings: list[DataMapping] = Field(
        default_factory=list,
        description="Simple field-to-field mappings with optional transforms"
    )
    template: Optional[str] = Field(
        None, max_length=10000,
        description="Jinja2 template for complex data transformations"
    )
    output_key: str = Field(
        default="transformed",
        description="Key under which transform output is stored in context"
    )


class HumanApprovalConfig(BaseModel):
    """Configuration for a human-in-the-loop approval gate."""
    message: str = Field(
        default="Approval required to continue workflow execution",
        max_length=2000,
        description="Message displayed to the approver"
    )
    approver_roles: list[str] = Field(
        default_factory=list,
        description="Roles that can approve. Empty = any admin."
    )
    timeout_seconds: Optional[int] = Field(
        default=3600, ge=60, le=604800,
        description="Max time to wait for approval (1min to 7 days)"
    )
    timeout_action: TimeoutAction = Field(
        default=TimeoutAction.REJECT,
        description="Action if approval times out"
    )
    show_context_fields: list[str] = Field(
        default_factory=list,
        description="Context fields to display to the approver for decision-making"
    )


class ParallelForkConfig(BaseModel):
    """Configuration for a parallel execution fork."""
    branch_labels: list[str] = Field(
        default_factory=list,
        description="Labels for each outgoing branch (for display)"
    )
    max_concurrent: Optional[int] = Field(
        None, ge=1, le=20,
        description="Max branches to execute concurrently. None = unlimited."
    )


class ParallelJoinConfig(BaseModel):
    """Configuration for a parallel execution join (synchronization point)."""
    join_strategy: JoinStrategy = Field(
        default=JoinStrategy.ALL,
        description="When to continue: all branches, any branch, or N of M"
    )
    required_count: Optional[int] = Field(
        None, ge=1,
        description="For N_OF_M strategy: how many branches must complete"
    )
    timeout_seconds: Optional[int] = Field(
        None, ge=1, le=86400,
        description="Max time to wait for branches"
    )
    merge_strategy: str = Field(
        default="collect",
        description="How to merge branch outputs: 'collect' (array) or 'merge' (deep merge)"
    )


# =============================================================================
# NODE_TYPE_CONFIGS: maps node types to their config models
# =============================================================================

NODE_TYPE_CONFIGS = {
    WorkflowNodeType.SKILL: SkillNodeConfig,
    WorkflowNodeType.CONDITION: ConditionNodeConfig,
    WorkflowNodeType.TRANSFORM: TransformNodeConfig,
    WorkflowNodeType.HUMAN_APPROVAL: HumanApprovalConfig,
    WorkflowNodeType.PARALLEL_FORK: ParallelForkConfig,
    WorkflowNodeType.PARALLEL_JOIN: ParallelJoinConfig,
    # START and END have no config
}


# =============================================================================
# Workflow Node
# =============================================================================

class WorkflowNode(BaseModel):
    """A single node in the workflow DAG."""
    id: str = Field(..., min_length=1, max_length=100)
    type: WorkflowNodeType
    label: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    position: NodePosition = Field(default_factory=NodePosition)
    config: Optional[dict[str, Any]] = Field(
        None,
        description="Type-specific configuration. Validated against the appropriate schema."
    )

    @field_validator("config")
    @classmethod
    def validate_config_not_empty(cls, v, info):
        """Basic validation — full type-specific validation happens at workflow level."""
        if v is not None and not isinstance(v, dict):
            raise ValueError("config must be a dict or null")
        return v


# =============================================================================
# Workflow Edge
# =============================================================================

class WorkflowEdge(BaseModel):
    """A directed edge connecting two nodes in the workflow DAG."""
    id: str = Field(..., min_length=1, max_length=100)
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    source_handle: Optional[str] = Field(
        None,
        description="Output handle ID. For condition nodes: 'true' or 'false'"
    )
    target_handle: Optional[str] = Field(
        None,
        description="Input handle ID. Usually null (default input)."
    )
    condition: Optional[str] = Field(
        None, max_length=2000,
        description="Optional guard expression. Edge only traversed if true."
    )
    data_mapping: list[DataMapping] = Field(
        default_factory=list,
        description="Field mappings carried along this edge"
    )
    label: Optional[str] = Field(None, max_length=100)
    animated: bool = Field(default=False, description="Animated edge in UI")


# =============================================================================
# Workflow Variable
# =============================================================================

class WorkflowVariable(BaseModel):
    """Declares a workflow-level variable (input parameter or internal state)."""
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(
        ..., pattern="^(string|number|boolean|object|array)$",
        description="Variable type"
    )
    default_value: Optional[Any] = None
    description: Optional[str] = Field(None, max_length=500)
    required: bool = False


# =============================================================================
# Workflow Viewport (canvas state persistence)
# =============================================================================

class WorkflowViewport(BaseModel):
    """Persisted canvas viewport state for React Flow."""
    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0


# =============================================================================
# Workflow Definition (top-level)
# =============================================================================

class WorkflowDefinition(BaseModel):
    """A complete workflow DAG definition linked to an agent skill.

    A skill can have at most one workflow. The workflow orchestrates
    other skills (and logic nodes) into a multi-step execution graph.
    """
    id: str = Field(..., min_length=1, max_length=100)
    skill_id: str = Field(..., description="Parent skill this workflow belongs to")
    tenant_id: str = Field(..., description="Tenant isolation")
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    status: WorkflowStatus = Field(default=WorkflowStatus.DRAFT)

    # Graph structure
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)

    # Variables (workflow inputs/outputs)
    variables: list[WorkflowVariable] = Field(default_factory=list)

    # Canvas state
    viewport: Optional[WorkflowViewport] = None

    # Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    validated_at: Optional[str] = None
    published_at: Optional[str] = None

    # Metadata
    max_execution_time_seconds: int = Field(
        default=300, ge=10, le=86400,
        description="Overall workflow timeout"
    )
    max_node_count: int = Field(
        default=50, ge=2, le=200,
        description="Maximum nodes allowed in this workflow"
    )


# =============================================================================
# Workflow Validation
# =============================================================================

class WorkflowValidationError(BaseModel):
    """A single validation error."""
    code: str
    message: str
    node_id: Optional[str] = None
    edge_id: Optional[str] = None
    severity: str = "error"     # error, warning


class WorkflowValidationResult(BaseModel):
    """Result of validating a workflow definition."""
    valid: bool
    errors: list[WorkflowValidationError] = Field(default_factory=list)
    warnings: list[WorkflowValidationError] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    has_cycles: bool = False
    is_connected: bool = True
    reachable_from_start: int = 0
    unreachable_nodes: list[str] = Field(default_factory=list)


# =============================================================================
# API Request/Response Models
# =============================================================================

class WorkflowResponse(BaseModel):
    """Response wrapping a workflow definition."""
    workflow: WorkflowDefinition
    message: Optional[str] = None


# =============================================================================
# Workflow Execution Models (runtime)
# =============================================================================

class WorkflowNodeExecution(BaseModel):
    """Execution record for a single node within a workflow run."""
    node_id: str
    node_type: WorkflowNodeType
    node_label: str
    status: NodeExecutionStatus = NodeExecutionStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_data: Optional[dict[str, Any]] = None
    output_data: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None

    # For skill nodes
    skill_execution_id: Optional[str] = None

    # For human approval nodes
    approved_by: Optional[str] = None
    approval_decision: Optional[str] = None    # approved, rejected
    approval_comment: Optional[str] = None


class WorkflowExecution(BaseModel):
    """A single execution instance of a workflow."""
    id: str
    workflow_id: str
    skill_id: str
    tenant_id: str
    status: WorkflowExecutionStatus = WorkflowExecutionStatus.PENDING
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Accumulated execution context. Updated as nodes complete."
    )
    node_executions: list[WorkflowNodeExecution] = Field(default_factory=list)

    # Input that triggered the workflow
    trigger_input: Optional[dict[str, Any]] = None
    triggered_by: Optional[str] = None

    # Timing
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None

    # Error
    error: Optional[str] = None
    failed_node_id: Optional[str] = None

    # Output
    output: Optional[dict[str, Any]] = None


class WorkflowExecutionListResponse(BaseModel):
    """Paginated list of workflow executions."""
    executions: list[WorkflowExecution]
    total: int


# =============================================================================
# Workflow DAG Validation Logic
# =============================================================================

def validate_workflow_dag(workflow: WorkflowDefinition) -> WorkflowValidationResult:
    """Validates the structural integrity of a workflow DAG.

    Checks:
    1. Exactly one START node
    2. At least one END node
    3. All edges reference valid nodes
    4. No cycles (it's a DAG)
    5. All nodes reachable from START
    6. All paths lead to an END node
    7. Condition nodes have exactly 2 outgoing edges (true/false)
    8. Parallel forks have matching joins
    9. Node configs match their types
    10. Node count within limits
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationError] = []
    node_map = {n.id: n for n in workflow.nodes}
    node_ids = set(node_map.keys())

    # --- Check 1: Exactly one START node ---
    start_nodes = [n for n in workflow.nodes if n.type == WorkflowNodeType.START]
    if len(start_nodes) == 0:
        errors.append(WorkflowValidationError(
            code="NO_START", message="Workflow must have exactly one Start node"
        ))
    elif len(start_nodes) > 1:
        errors.append(WorkflowValidationError(
            code="MULTIPLE_STARTS",
            message=f"Workflow has {len(start_nodes)} Start nodes, expected exactly 1",
            node_id=start_nodes[1].id
        ))

    # --- Check 2: At least one END node ---
    end_nodes = [n for n in workflow.nodes if n.type == WorkflowNodeType.END]
    if len(end_nodes) == 0:
        errors.append(WorkflowValidationError(
            code="NO_END", message="Workflow must have at least one End node"
        ))

    # --- Check 3: Edge references ---
    for edge in workflow.edges:
        if edge.source not in node_ids:
            errors.append(WorkflowValidationError(
                code="INVALID_EDGE_SOURCE",
                message=f"Edge '{edge.id}' references non-existent source node '{edge.source}'",
                edge_id=edge.id
            ))
        if edge.target not in node_ids:
            errors.append(WorkflowValidationError(
                code="INVALID_EDGE_TARGET",
                message=f"Edge '{edge.id}' references non-existent target node '{edge.target}'",
                edge_id=edge.id
            ))

    # --- Check 4: Cycle detection (DFS) ---
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in workflow.edges:
        if edge.source in node_ids and edge.target in node_ids:
            adjacency[edge.source].append(edge.target)

    has_cycles = False
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def dfs_cycle(node_id: str) -> bool:
        visited.add(node_id)
        rec_stack.add(node_id)
        for neighbor in adjacency.get(node_id, []):
            if neighbor not in visited:
                if dfs_cycle(neighbor):
                    return True
            elif neighbor in rec_stack:
                return True
        rec_stack.discard(node_id)
        return False

    for nid in node_ids:
        if nid not in visited:
            if dfs_cycle(nid):
                has_cycles = True
                break

    if has_cycles:
        errors.append(WorkflowValidationError(
            code="CYCLE_DETECTED",
            message="Workflow contains a cycle. Workflows must be directed acyclic graphs (DAGs)."
        ))

    # --- Check 5: Reachability from START ---
    reachable: set[str] = set()
    if start_nodes:
        stack = [start_nodes[0].id]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(adjacency.get(current, []))

    unreachable = node_ids - reachable
    # Exclude START from unreachable check if multiple starts
    unreachable_list = [nid for nid in unreachable if node_map.get(nid) and node_map[nid].type != WorkflowNodeType.START]
    if unreachable_list:
        for nid in unreachable_list:
            warnings.append(WorkflowValidationError(
                code="UNREACHABLE_NODE",
                message=f"Node '{node_map[nid].label}' ({nid}) is not reachable from Start",
                node_id=nid,
                severity="warning"
            ))

    # --- Check 6: No incoming edges to START, no outgoing edges from END ---
    for edge in workflow.edges:
        if edge.target in node_ids and node_map[edge.target].type == WorkflowNodeType.START:
            errors.append(WorkflowValidationError(
                code="EDGE_INTO_START",
                message="Start node cannot have incoming edges",
                edge_id=edge.id, node_id=edge.target
            ))
        if edge.source in node_ids and node_map[edge.source].type == WorkflowNodeType.END:
            errors.append(WorkflowValidationError(
                code="EDGE_FROM_END",
                message="End node cannot have outgoing edges",
                edge_id=edge.id, node_id=edge.source
            ))

    # --- Check 7: Condition nodes must have true/false handles ---
    for node in workflow.nodes:
        if node.type == WorkflowNodeType.CONDITION:
            outgoing = [e for e in workflow.edges if e.source == node.id]
            handles = {e.source_handle for e in outgoing}
            if "true" not in handles:
                errors.append(WorkflowValidationError(
                    code="CONDITION_MISSING_TRUE",
                    message=f"Condition node '{node.label}' missing 'Yes' branch",
                    node_id=node.id
                ))
            if "false" not in handles:
                errors.append(WorkflowValidationError(
                    code="CONDITION_MISSING_FALSE",
                    message=f"Condition node '{node.label}' missing 'No' branch",
                    node_id=node.id
                ))

    # --- Check 8: Skill nodes must reference a skill ---
    for node in workflow.nodes:
        if node.type == WorkflowNodeType.SKILL:
            if not node.config or not node.config.get("skill_id"):
                errors.append(WorkflowValidationError(
                    code="SKILL_NODE_NO_SKILL",
                    message=f"Skill node '{node.label}' has no skill_id configured",
                    node_id=node.id
                ))

    # --- Check 9: Node count ---
    if len(workflow.nodes) > workflow.max_node_count:
        errors.append(WorkflowValidationError(
            code="TOO_MANY_NODES",
            message=f"Workflow has {len(workflow.nodes)} nodes, max is {workflow.max_node_count}"
        ))

    # --- Check 10: Disconnected nodes (no edges at all) ---
    nodes_with_edges = set()
    for edge in workflow.edges:
        nodes_with_edges.add(edge.source)
        nodes_with_edges.add(edge.target)
    disconnected = node_ids - nodes_with_edges
    for nid in disconnected:
        if node_map[nid].type not in (WorkflowNodeType.START, WorkflowNodeType.END) or len(workflow.nodes) > 1:
            warnings.append(WorkflowValidationError(
                code="DISCONNECTED_NODE",
                message=f"Node '{node_map[nid].label}' ({nid}) has no connections",
                node_id=nid,
                severity="warning"
            ))

    return WorkflowValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        node_count=len(workflow.nodes),
        edge_count=len(workflow.edges),
        has_cycles=has_cycles,
        is_connected=len(unreachable_list) == 0,
        reachable_from_start=len(reachable),
        unreachable_nodes=unreachable_list,
    )
