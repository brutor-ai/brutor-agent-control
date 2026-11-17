# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic request/response models for /v1/admin/argument-policies.

The DSL ("rules" list) is validated server-side here so a
malformed policy cannot land in the DB. The runtime evaluator
in the Rust proxy assumes the rule shape is already valid.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator, ConfigDict

from app.database.models.argument_policy import (
    ALLOWED_ANALYZERS,
    ALLOWED_SEVERITIES,
    ALLOWED_SURFACES,
    ANALYZER_JSON,
    ANALYZER_PATH,
    ANALYZER_SHELL,
    ANALYZER_SQL,
    ANALYZER_URL,
    SEVERITY_DENY,
)


# =============================================================================
# Predicate catalogue (per-analyzer allowed rule predicates)
# =============================================================================
#
# Keep these in sync with the Rust predicate registry
# (services/argument_policy/evaluator.rs). Adding a predicate
# requires touching three places: this list, the Rust evaluator,
# and the admin UI dropdown spec.

PREDICATES_BY_ANALYZER: Dict[str, set] = {
    ANALYZER_SQL: {
        "writes",
        "statement_type_in",
        "statement_type_not_in",
        "touches_any",
        "touches_none_of",
        "subquery_depth_gt",
        "multi_statement",
    },
    ANALYZER_URL: {
        "host_in_private_range",
        "scheme_not_in",
        "scheme_in",
        "host_in",
        "host_not_in",
    },
    ANALYZER_SHELL: {
        "command_in",
        "command_not_in",
        "flag_present",
        "flag_not_present",
    },
    ANALYZER_PATH: {
        "traverses_parent",
        "absolute_required",
        "inside_root",
        "outside_root",
    },
    ANALYZER_JSON: {
        "schema_invalid",
        "field_present",
        "field_not_present",
        # The closed comparison set. Deliberately fixed at eight: general
        # expressions belong to the policy engine's CEL profile.
        "field_gt",
        "field_gte",
        "field_lt",
        "field_lte",
        "field_eq",
        "field_ne",
        "field_in",
        "field_not_in",
    },
}

#: Predicates that read `rule.field` (a dotted path) rather than operating on
#: the whole argument. Adding one means touching three places, per this
#: module's contract: here, the Rust evaluator, and the admin UI dropdown.
PREDICATES_REQUIRING_FIELD: set = {
    "field_gt",
    "field_gte",
    "field_lt",
    "field_lte",
    "field_eq",
    "field_ne",
    "field_in",
    "field_not_in",
}

#: Of those, the ones whose operand must be a number.
PREDICATES_REQUIRING_NUMERIC_VALUE: set = {
    "field_gt",
    "field_gte",
    "field_lt",
    "field_lte",
}

#: And the ones that take a list in `values` instead of a scalar `value`.
PREDICATES_REQUIRING_LIST_VALUES: set = {
    "field_in",
    "field_not_in",
}

# Predicates that REQUIRE a `values` list (vs. boolean predicates
# that take no arguments).
PREDICATES_REQUIRING_VALUES: set = {
    "statement_type_in",
    "statement_type_not_in",
    "touches_any",
    "touches_none_of",
    "subquery_depth_gt",
    "scheme_not_in",
    "scheme_in",
    "host_in",
    "host_not_in",
    "command_in",
    "command_not_in",
    "flag_present",
    "flag_not_present",
    "inside_root",
    "outside_root",
    "field_present",
    "field_not_present",
}


#: And the ones that take one scalar in `value`: a string, a number or a
#: boolean, which is what the gateway compares a leaf with.
PREDICATES_REQUIRING_SCALAR_VALUE: set = {
    "field_eq",
    "field_ne",
}

# How much of an argument the gateway's `json` analyzer walks
# (`AnalysisLimits` in its `services/argument_policy/mod.rs`): what a policy
# that states no limit is analyzed under, and the most a policy may state in
# `analyzer_config`. They are stated to the administrator because a call
# beyond a policy's limits is refused, whatever the rules say.
ANALYSIS_LIMIT_DEFAULTS: Dict[str, int] = {"max_leaves": 512, "max_depth": 8, "max_array_elements": 64}
ANALYSIS_LIMIT_CEILINGS: Dict[str, int] = {"max_leaves": 4096, "max_depth": 32, "max_array_elements": 1024}

ANALYSIS_CAPS = (
    f"The gateway analyzes at most {ANALYSIS_LIMIT_DEFAULTS['max_array_elements']} elements of an array, "
    f"{ANALYSIS_LIMIT_DEFAULTS['max_leaves']} leaves and {ANALYSIS_LIMIT_DEFAULTS['max_depth']} levels of "
    "nesting of the arguments, unless the policy states its own limits in analyzer_config. "
    "A call whose arguments go beyond a policy's limits is refused (rule 'arguments_truncated') "
    "whenever that policy is enabled and targets the tool, whatever its rules and severities."
)

_ANALYZER_DESCRIPTION = (
    "How the argument is read: sql, url, shell, path or json. For json: " + ANALYSIS_CAPS
)

_ANALYZER_CONFIG_DESCRIPTION = (
    "Settings of the analyzer. For json: 'schema' ({'required': [paths]}), 'strict_numbers', and the "
    "analysis limits of this policy: "
    + "; ".join(
        f"'{key}' (an integer from 1 to {ANALYSIS_LIMIT_CEILINGS[key]}, default {ANALYSIS_LIMIT_DEFAULTS[key]})"
        for key in ANALYSIS_LIMIT_DEFAULTS
    )
    + ". A limit that is not an integer in its range is refused, never adjusted. A call that several "
    "policies target is analyzed under each policy's own limits and passes only when it fits all of them."
)


def analysis_limit_error(analyzer_config: Any) -> Optional[str]:
    """Why a limit stated in `analyzer_config` is not one the gateway
    applies (it refuses the policy's calls instead); None when every stated
    limit is an integer from 1 to its ceiling, or none is stated."""
    if not isinstance(analyzer_config, dict):
        return None
    for key, ceiling in ANALYSIS_LIMIT_CEILINGS.items():
        if key not in analyzer_config:
            continue
        stated = analyzer_config[key]
        # `True` is an `int` in Python and not an integer in JSON.
        if isinstance(stated, bool) or not isinstance(stated, int) or not 1 <= stated <= ceiling:
            return f"analyzer_config.{key} must be an integer from 1 to {ceiling}"
    return None


#: What a policy's own severity may be: what a tripped rule does when it
#: carries no severity of its own. The gateway reads it as stored.
POLICY_SEVERITIES = (SEVERITY_DENY, "approval_required", "warn")

_POLICY_SEVERITY_DESCRIPTION = (
    "What a tripped rule does when it carries no severity of its own: 'deny' refuses the call, "
    "'approval_required' holds it for a person to approve or reject (not available on the "
    "`llm_output_tool_calls` surface, where it is refused), 'warn' logs it and lets it "
    "through. A rule's own severity takes precedence."
)


def _is_comparable(value: Any) -> bool:
    """A string, a number or a boolean: what the gateway compares a leaf
    with. A list or an object as operand compares with nothing, and the
    gateway refuses every call that carries the field as undecidable."""
    return isinstance(value, (str, bool, int, float))


def comparison_operand_error(predicate: Any, value: Any, values: Any) -> Optional[str]:
    """Why the operand of an equality or membership rule cannot be
    compared by the gateway; None when it can, or for another predicate."""
    if predicate in PREDICATES_REQUIRING_SCALAR_VALUE:
        if value is not None and not _is_comparable(value):
            return (
                f"Predicate '{predicate}' compares 'field' with a string, a number or a boolean: "
                f"'value' is {type(value).__name__}"
            )
    elif predicate in PREDICATES_REQUIRING_LIST_VALUES:
        if not isinstance(values, list):
            return None if values is None else f"Predicate '{predicate}' requires a 'values' list"
        for option in values:
            if not _is_comparable(option):
                return (
                    f"Predicate '{predicate}' compares 'field' with a string, a number or a boolean: "
                    f"'values' holds {'null' if option is None else type(option).__name__}"
                )
    return None


# =============================================================================
# Rule
# =============================================================================

class ArgumentPolicyRule(BaseModel):
    """One predicate in a policy's rules list.

    Either `deny_if` OR `allow_only_if` must be set (mutually
    exclusive). `values` is required for predicates in
    PREDICATES_REQUIRING_VALUES. `message` is shown to the
    caller verbatim — keep it operator-friendly.
    """

    deny_if: Optional[str] = Field(
        None,
        description="Predicate that, if TRUE, denies the request",
    )
    allow_only_if: Optional[str] = Field(
        None,
        description="Predicate that, if FALSE, denies the request",
    )
    values: Optional[List[Any]] = Field(
        None,
        description=(
            "Argument list for predicates that need one (e.g. allowlist). For field_in and "
            "field_not_in: the options, each a string, a number or a boolean; a boolean field is "
            "judged only against a list that offers a boolean."
        ),
    )
    field: Optional[str] = Field(
        None,
        max_length=256,
        description=(
            "Dotted path into the argument object for the comparison "
            "predicates, e.g. 'amount' or 'transfer.amount' or 'items.0.qty'. "
            f"The gateway reads the first {ANALYSIS_LIMIT_DEFAULTS['max_array_elements']} elements of "
            f"an array and {ANALYSIS_LIMIT_DEFAULTS['max_depth']} levels of nesting (or the policy's own "
            "limits in analyzer_config): a path beyond them is never reached, and the call is refused "
            "instead."
        ),
    )
    value: Optional[Any] = Field(
        None,
        description=(
            "Operand the comparison predicates test `field` against: a number for field_gt, "
            "field_gte, field_lt and field_lte; a string, a number or a boolean for field_eq and "
            "field_ne. A field is compared with an operand of its own type; a field that holds "
            "another type cannot be decided and the call is refused."
        ),
    )
    severity: Optional[Literal["deny", "approval_required", "warn"]] = Field(
        None,
        description=(
            "Overrides the policy severity for this rule only. Rules are "
            "evaluated in order and the first match wins, so ordering is what "
            "expresses a band: deny over 10000, approval over 1000, allow below."
        ),
    )
    message: Optional[str] = Field(
        None,
        max_length=512,
        description="Human-facing reason returned to the client on denial",
    )

    @model_validator(mode="after")
    def _exactly_one_predicate(self):
        if (self.deny_if is None) == (self.allow_only_if is None):
            raise ValueError(
                "Rule must set exactly one of deny_if or allow_only_if"
            )
        return self

    @model_validator(mode="after")
    def _comparison_rules_are_complete(self):
        """A comparison predicate without a field or operand compares
        nothing and silently never fires — the worst outcome for a policy,
        because it looks enforced."""
        predicate = self.deny_if or self.allow_only_if
        if predicate in PREDICATES_REQUIRING_FIELD:
            if not self.field:
                raise ValueError(
                    f"Predicate '{predicate}' requires a 'field' path"
                )
            if predicate in PREDICATES_REQUIRING_NUMERIC_VALUE:
                if isinstance(self.value, bool) or not isinstance(
                    self.value, (int, float)
                ):
                    raise ValueError(
                        f"Predicate '{predicate}' requires a numeric 'value'"
                    )
            elif predicate in PREDICATES_REQUIRING_LIST_VALUES:
                if not self.values:
                    raise ValueError(
                        f"Predicate '{predicate}' requires a 'values' list"
                    )
            elif self.value is None:
                raise ValueError(
                    f"Predicate '{predicate}' requires a 'value'"
                )
            error = comparison_operand_error(predicate, self.value, self.values)
            if error:
                raise ValueError(error)
        return self


# =============================================================================
# Create / Update
# =============================================================================

def _rule_part(rule: Any, key: str) -> Any:
    """One member of a rule, whether it is the parsed model (the admin API)
    or the stored dict (policy-as-code, a row read back for a PATCH)."""
    if isinstance(rule, dict):
        return rule.get(key)
    return getattr(rule, key, None)


def effective_rule_severity(rule: Any, policy_severity: str) -> Any:
    """The severity the gateway acts on when this rule trips.

    Mirrors `rule.severity.unwrap_or(policy.severity)` in the gateway's
    `services/argument_policy/evaluator.rs`: the rule's own value when it
    carries one, the policy's otherwise. An absent key and an explicit null
    both inherit.
    """
    own = _rule_part(rule, "severity")
    return policy_severity if own is None else own


def validate_comparison_rules_have_a_presence_guard(
    rules: List[Any],
    analyzer_config: Optional[Dict[str, Any]],
    argument_key: str,
    policy_severity: str,
) -> None:
    """Refuse a banded policy whose compared field could simply be omitted.

    This is the single most dangerous shape in Part A. A comparison
    predicate that reads a MISSING field does not hold, and a rule that does
    not hold does not deny — so an agent that calls
    ``transfer_funds({})`` with no ``amount`` at all walks straight past a
    ``field_gt: amount`` threshold. The policy looks enforced on the page
    and is not, which is strictly worse than having no policy.

    The fix already exists in the schema: ``analyzer_config.schema.required``
    populates ``json_schema_errors``, and the ``schema_invalid`` predicate
    denies on it. An explicit ``field_present`` rule on the same path does
    the same job. This refuses a policy that has neither.

    A guard counts only when the rule that carries it DENIES. The gateway
    logs a ``warn`` and lets the call through, and holds an
    ``approval_required`` for a human instead of refusing it, so a guard of
    either severity leaves the omitted field open. The severity judged is
    the effective one (`effective_rule_severity`).

    It runs on the row as it will be STORED, on every path that writes one:
    create and import through `ArgumentPolicyCreateRequest`, PATCH on the
    merged row, and policy-as-code on each bundle entry. ``rules`` is
    therefore either parsed `ArgumentPolicyRule`s or the stored dicts.

    This one has three layers because it fails silently: this refusal, the form seeding the presence rule, and a live
    test asserting the omit-the-field call is refused.
    """
    def predicate(rule: Any) -> Any:
        return _rule_part(rule, "deny_if") or _rule_part(rule, "allow_only_if")

    compared = {
        _rule_part(rule, "field")
        for rule in rules
        if predicate(rule) in PREDICATES_REQUIRING_FIELD
        and isinstance(_rule_part(rule, "field"), str)
        and _rule_part(rule, "field")
    }
    if not compared:
        return

    if argument_key != "*":
        raise ValueError(
            "Comparison rules address fields by dotted path inside the whole "
            "argument object, so argument_key must be '*'. Naming a single "
            "key analyzes only that argument, and the paths would never "
            f"resolve (got {argument_key!r})."
        )

    def denies(rule: Any) -> bool:
        return effective_rule_severity(rule, policy_severity) == SEVERITY_DENY

    # A `schema.required` entry covers the top-level key of a dotted path:
    # requiring `transfer` does not guarantee `transfer.amount`, so only the
    # exact path counts.
    required = set()
    if analyzer_config:
        schema = analyzer_config.get("schema") or {}
        required = {r for r in (schema.get("required") or []) if isinstance(r, str)}

    # `field_present` / `field_not_present` carry their paths in `values`,
    # not in `field`. Either phrasing guards the path: `allow_only_if:
    # field_present` denies when it is absent, and so does `deny_if:
    # field_not_present`. The gateway reads a list of several paths as ANY
    # (`targets.iter().any` in evaluator.rs), so a rule naming two is
    # satisfied by one of them and guards neither: only a rule naming
    # exactly one path counts.
    present_paths: set = set()
    weak_paths: set = set()
    shared_paths: set = set()
    for rule in rules:
        if (
            _rule_part(rule, "allow_only_if") == "field_present"
            or _rule_part(rule, "deny_if") == "field_not_present"
        ):
            paths = {v for v in (_rule_part(rule, "values") or []) if isinstance(v, str)}
            if len(paths) != 1:
                shared_paths.update(paths)
            elif denies(rule):
                present_paths.update(paths)
            else:
                weak_paths.update(paths)

    # A `schema.required` entry only bites if some rule actually denies on
    # the resulting error, so the schema route needs BOTH halves.
    schema_rules = [r for r in rules if _rule_part(r, "deny_if") == "schema_invalid"]
    schema_guarded: set = set()
    if any(denies(r) for r in schema_rules):
        schema_guarded = required
    elif schema_rules:
        weak_paths.update(required)

    unguarded = sorted(
        path for path in compared if path not in schema_guarded and path not in present_paths
    )
    if unguarded:
        message = (
            "A comparison rule can be bypassed by omitting the field it "
            "compares. Add the path to analyzer_config.schema.required (with "
            "a schema_invalid rule) or an explicit field_present rule for: "
            + ", ".join(unguarded)
        )
        only_logged = sorted(path for path in unguarded if path in weak_paths)
        if only_logged:
            message += (
                ". The rule guarding "
                + ", ".join(only_logged)
                + " does not deny: its effective severity (its own, else the "
                "policy's) must be 'deny', because the gateway lets a 'warn' "
                "through and holds an 'approval_required' instead of refusing it"
            )
        shared = sorted(path for path in unguarded if path in shared_paths)
        if shared:
            message += (
                ". A presence rule naming several paths is satisfied by any "
                "one of them, so it guards none: give "
                + ", ".join(shared)
                + " a presence rule of its own"
            )
        raise ValueError(message)


#: The surface of a model's tool calls. The gateway holds nothing for a
#: person there, so nothing on it may ask for an approval.
SURFACE_MODEL_TOOL_CALLS = "llm_output_tool_calls"
NO_APPROVAL_ON_MODEL_TOOL_CALLS = (
    "Approvals are not available for model tool calls: the gateway holds nothing for a "
    "person on that surface. Use 'deny' or 'warn', or put the policy on the tool's own "
    "surface (mcp_input, skill_input), where a call can be held."
)


def approval_on_model_tool_calls_error(
    surface: Optional[str], rules: List[Any], policy_severity: Optional[str]
) -> Optional[str]:
    """Why a policy on the model tool-call surface cannot be saved: it, or
    one of its rules, asks for an approval."""
    if surface != SURFACE_MODEL_TOOL_CALLS:
        return None
    if policy_severity == "approval_required":
        return f"severity: {NO_APPROVAL_ON_MODEL_TOOL_CALLS}"
    for i, rule in enumerate(rules or []):
        if _rule_part(rule, "severity") == "approval_required":
            return f"rules[{i}].severity: {NO_APPROVAL_ON_MODEL_TOOL_CALLS}"
    return None


def validate_policy_as_stored(
    rules: List[Any],
    analyzer_config: Optional[Dict[str, Any]],
    argument_key: str,
    policy_severity: str,
    surface: Optional[str] = None,
) -> None:
    """What every path that writes a policy asks of the row as it will be
    stored (create, import, PATCH on the merged row, a bundle entry):

    - a policy on the model tool-call surface asks for no approval, by its
      own severity or a rule's;

    - every analysis limit stated in `analyzer_config` is one the gateway
      applies;
    - the operand of every equality and membership rule is one the gateway
      can compare (a bundle hands the stored dicts, which no rule model
      has read);
    - a compared field cannot simply be omitted.
    """
    error = approval_on_model_tool_calls_error(surface, rules, policy_severity)
    if error:
        raise ValueError(error)
    error = analysis_limit_error(analyzer_config)
    if error:
        raise ValueError(error)
    for i, rule in enumerate(rules):
        error = comparison_operand_error(
            _rule_part(rule, "deny_if") or _rule_part(rule, "allow_only_if"),
            _rule_part(rule, "value"),
            _rule_part(rule, "values"),
        )
        if error:
            raise ValueError(f"rules[{i}]: {error}")
    validate_comparison_rules_have_a_presence_guard(rules, analyzer_config, argument_key, policy_severity)


class ArgumentPolicyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)
    surface: Literal[
        "llm_output_tool_calls",
        "mcp_input",
        "skill_input",
    ]
    target: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Tool name (LLM/MCP) or skill name. Use '*' to match any.",
    )
    argument_key: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "Key inside the arguments JSON to analyze, or '*' for the whole "
            "argument object. Comparison rules address fields by dotted path "
            "inside that object, so a banded policy uses '*'."
        ),
    )
    analyzer: Literal["sql", "url", "shell", "path", "json"] = Field(..., description=_ANALYZER_DESCRIPTION)
    analyzer_config: Optional[Dict[str, Any]] = Field(None, description=_ANALYZER_CONFIG_DESCRIPTION)
    rules: List[ArgumentPolicyRule] = Field(default_factory=list)
    enabled: bool = True
    severity: Literal["deny", "approval_required", "warn"] = Field("deny", description=_POLICY_SEVERITY_DESCRIPTION)
    group_ids: Optional[List[str]] = Field(
        None,
        description="Resource groups to assign on creation",
    )

    @model_validator(mode="after")
    def _validate_predicates_against_analyzer(self):
        allowed = PREDICATES_BY_ANALYZER[self.analyzer]
        for i, rule in enumerate(self.rules):
            predicate = rule.deny_if or rule.allow_only_if
            if predicate not in allowed:
                raise ValueError(
                    f"rules[{i}]: predicate {predicate!r} is not valid for "
                    f"analyzer {self.analyzer!r}. "
                    f"Allowed: {sorted(allowed)}"
                )
            if predicate in PREDICATES_REQUIRING_VALUES and not rule.values:
                raise ValueError(
                    f"rules[{i}]: predicate {predicate!r} requires "
                    f"a non-empty `values` list"
                )
        validate_policy_as_stored(
            self.rules, self.analyzer_config, self.argument_key, self.severity, self.surface
        )
        return self


class ArgumentPolicyUpdateRequest(BaseModel):
    """All fields optional — PATCH semantics."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)
    surface: Optional[
        Literal["llm_output_tool_calls", "mcp_input", "skill_input"]
    ] = None
    target: Optional[str] = Field(None, min_length=1, max_length=255)
    argument_key: Optional[str] = Field(None, min_length=1, max_length=255)
    analyzer: Optional[Literal["sql", "url", "shell", "path", "json"]] = Field(
        None, description=_ANALYZER_DESCRIPTION
    )
    analyzer_config: Optional[Dict[str, Any]] = Field(None, description=_ANALYZER_CONFIG_DESCRIPTION)
    rules: Optional[List[ArgumentPolicyRule]] = None
    enabled: Optional[bool] = None
    severity: Optional[Literal["deny", "approval_required", "warn"]] = Field(
        None, description=_POLICY_SEVERITY_DESCRIPTION
    )

    @model_validator(mode="after")
    def _validate_predicates_against_analyzer(self):
        # A limit is judged on its own, whatever else the patch carries:
        # the merged row is judged again at the endpoint.
        error = analysis_limit_error(self.analyzer_config)
        if error:
            raise ValueError(error)
        # Only validate if BOTH analyzer + rules are being changed
        # together. If only one is patched, the caller must accept
        # the existing other side — we'll re-validate the merged
        # row at endpoint time using the combined view.
        if self.analyzer and self.rules is not None:
            allowed = PREDICATES_BY_ANALYZER[self.analyzer]
            for i, rule in enumerate(self.rules):
                predicate = rule.deny_if or rule.allow_only_if
                if predicate not in allowed:
                    raise ValueError(
                        f"rules[{i}]: predicate {predicate!r} is not valid for "
                        f"analyzer {self.analyzer!r}. "
                        f"Allowed: {sorted(allowed)}"
                    )
                if predicate in PREDICATES_REQUIRING_VALUES and not rule.values:
                    raise ValueError(
                        f"rules[{i}]: predicate {predicate!r} requires "
                        f"a non-empty `values` list"
                    )
        return self


class ArgumentPolicyGroupAssociationRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    group_ids: List[str] = Field(
        ...,
        description="Resource group IDs to associate with this policy. "
                    "Replaces the current set (idempotent).",
    )


# =============================================================================
# Response
# =============================================================================

class ArgumentPolicyGroupInfo(BaseModel):
    """Minimal group info for argument-policy responses (matches GuardrailGroupInfo)."""
    id: str
    name: str
    display_name: Optional[str] = None
    parent_group_id: Optional[str] = None
    is_active: Optional[bool] = None


class ArgumentPolicyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    surface: str
    target: str
    argument_key: str
    analyzer: str
    analyzer_config: Optional[Dict[str, Any]] = None
    rules: List[Dict[str, Any]]
    enabled: bool
    severity: str
    group_ids: List[str] = Field(default_factory=list)
    # Full group records — surfaces in the admin UI via GroupTreeDisplay
    # so operators see names + hierarchy, not just counts.
    groups: List[ArgumentPolicyGroupInfo] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def severities_by_surface() -> Dict[str, List[str]]:
    """The severities a policy, or a rule of it, may have on each surface:
    every severity, less the ones the surface has no way to carry out."""
    return {
        surface: [
            severity
            for severity in POLICY_SEVERITIES
            if approval_on_model_tool_calls_error(surface, [], severity) is None
        ]
        for surface in ALLOWED_SURFACES
    }


class ArgumentPolicyListResponse(BaseModel):
    policies: List[ArgumentPolicyResponse]
    total: int
    severities_by_surface: Dict[
        Literal["llm_output_tool_calls", "mcp_input", "skill_input"],
        List[Literal["deny", "approval_required", "warn"]],
    ] = Field(
        default_factory=severities_by_surface,
        description=(
            "The severities a policy and its rules may have on each surface. `approval_required` "
            "is not among those of `llm_output_tool_calls`: the gateway holds nothing for a person "
            "on a model's tool calls, and a policy or rule that asks for it there is refused."
        ),
    )
