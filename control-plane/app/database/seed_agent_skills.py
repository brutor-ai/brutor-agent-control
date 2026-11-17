# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skills Seeding

Creates default Agent Skills during tenant provisioning.
Each skill includes its SKILL.md content, scripts, assets, and/or resources,
plus a binding to the Default Resource Group.

Called from TenantProvisioner.provision_tenant() after system MCP server seeding.

Usage:
    from app.database.seed_agent_skills import seed_agent_skills

    # Inside provision_tenant(), after seed_system_mcp_servers():
    seed_agent_skills(db, tenant_id, default_group_id, logger)
"""

import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_skill_script import AgentSkillScript
from app.database.models.agent_skill_asset import AgentSkillAsset
from app.database.models.agent_skill_resource import AgentSkillResource
from app.database.models.resource_group import ResourceGroup
from app.constants.agent_skill import (
    SkillStatus,
    ScriptLanguage,
    ScriptExecutionMode,
    AssetType,
    SkillResourceType,
)


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode()).hexdigest()


def seed_agent_skills(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed all default Agent Skills.

    Idempotent -- safe to call on every startup. Checks by stable ID.

    Args:
        db: Database session (caller manages commit)
        tenant_id: Tenant ID
        default_group_id: Default Resource Group ID for initial binding
        logger: Logger instance
    """
    _seed_hello_world_skill(db, tenant_id, default_group_id, logger)
    _seed_skill_creator_skill(db, tenant_id, default_group_id, logger)


# ======================================================================
# Skill 1: hello-world
# ======================================================================

def _seed_hello_world_skill(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed the hello-world demo Agent Skill.

    Creates:
        1. AgentSkill record
        2. AgentSkillScript (get_system_info.py)
        3. AgentSkillAsset (greeting.md template)
        4. Binding to Default Resource Group
    """
    _t = tenant_id
    skill_id = f"skill-system-hello-world-{_t}"
    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 1. AgentSkill
    # ------------------------------------------------------------------
    existing = db.query(AgentSkill).filter(AgentSkill.id == skill_id).first()

    if existing:
        skill = existing
    else:
        skill_md_content = (
            "# Hello World\n"
            "\n"
            "A demo skill that shows how Agent Skills work in Brutor AI.\n"
            "\n"
            'Use this skill when the user says "hello", "hello world", or asks for a greeting.\n'
            "\n"
            "## Workflow\n"
            "\n"
            "1. Run the [script](get_system_info.py) to collect system information.\n"
            "2. Format the response using the [template](greeting.md).\n"
        )

        skill = AgentSkill(
            id=skill_id,
            tenant_id=tenant_id,
            name="hello-world",
            description=(
                "Demo skill that responds to greetings with system information. "
                "Shows how skills use scripts and templates."
            ),
            version="1.0.0",
            status=SkillStatus.PUBLISHED,
            enabled=True,
            license="MIT",
            skill_md_content=skill_md_content,
            skill_md_hash=_sha256(skill_md_content),
            skill_metadata={
                "tags": ["demo", "getting-started"],
                "category": "utility",
                "icon_name": "hand-wave",
                "approval_required": False,
                "cost_estimate_usd": 0.0,
            },
            published_at=now,
            published_by="system",
            created_by="system",
            created_at=now,
            updated_at=now,
        )

        db.add(skill)
        db.flush()

        logger.info("Created hello-world skill", context={
            "skill_id": skill_id, "tenant_id": tenant_id,
        })

    # ------------------------------------------------------------------
    # 2. AgentSkillScript: get_system_info.py
    # ------------------------------------------------------------------
    script_id = f"ass-system-hello-world-script-{_t}"
    existing_script = db.query(AgentSkillScript).filter(
        AgentSkillScript.id == script_id
    ).first()

    if existing_script:
        pass
    else:
        script_content = (
            "import platform\n"
            "import datetime\n"
            "\n"
            'print(f"Hostname:  {platform.node()}")\n'
            'print(f"Platform:  {platform.system()} {platform.release()}")\n'
            'print(f"Python:    {platform.python_version()}")\n'
            'print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).isoformat()}")\n'
        )

        script = AgentSkillScript(
            id=script_id,
            tenant_id=tenant_id,
            skill_id=skill_id,
            filename="get_system_info.py",
            description="Collects basic system information (hostname, platform, timestamp)",
            script_content=script_content,
            script_hash=_sha256(script_content),
            language=ScriptLanguage.PYTHON,
            execution_mode=ScriptExecutionMode.SANDBOX,
            created_at=now,
            updated_at=now,
        )

        db.add(script)
        db.flush()

        logger.info("Created hello-world script", context={
            "script_id": script_id, "skill_id": skill_id,
        })

    # ------------------------------------------------------------------
    # 3. AgentSkillAsset: greeting.md template
    # ------------------------------------------------------------------
    asset_id = f"asa-system-hello-world-template-{_t}"
    existing_asset = db.query(AgentSkillAsset).filter(
        AgentSkillAsset.id == asset_id
    ).first()

    if existing_asset:
        pass
    else:
        template_content = (
            "```\n"
            " _   _      _ _        __        __         _     _ _\n"
            "| | | | ___| | | ___   \\ \\      / /__  _ __| | __| | |\n"
            "| |_| |/ _ \\ | |/ _ \\   \\ \\ /\\ / / _ \\| '__| |/ _` | |\n"
            "|  _  |  __/ | | (_) |   \\ V  V / (_) | |  | | (_| |_|\n"
            "|_| |_|\\___|_|_|\\___/     \\_/\\_/ \\___/|_|  |_|\\__,_(_)\n"
            "```\n"
            "\n"
            "# Hello from Brutor AI!\n"
            "\n"
            "Welcome! Here is some information about the system:\n"
            "\n"
            "```\n"
            "{{get_system_info}}\n"
            "```\n"
            "\n"
            "---\n"
            "*This response was generated by the **hello-world** demo Agent Skill.*\n"
        )

        asset = AgentSkillAsset(
            id=asset_id,
            tenant_id=tenant_id,
            skill_id=skill_id,
            asset_type=AssetType.TEMPLATE,
            filename="greeting.md",
            description="Greeting response template with system info",
            content=template_content,
            content_hash=_sha256(template_content),
            mime_type="text/markdown",
            size_bytes=len(template_content.encode("utf-8")),
            encoding="utf-8",
            created_at=now,
            updated_at=now,
        )

        db.add(asset)
        db.flush()

        logger.info("Created hello-world template asset", context={
            "asset_id": asset_id, "skill_id": skill_id,
        })

    # ------------------------------------------------------------------
    # 4. Bind to Default Resource Group
    # ------------------------------------------------------------------
    _bind_skill_to_group(db, skill, default_group_id, logger)


# ======================================================================
# Skill 2: skill-creator
# ======================================================================

def _seed_skill_creator_skill(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed the skill-creator Agent Skill.

    Based on the official Anthropic skill-creator from:
    https://github.com/anthropics/skills/tree/main/skills/skill-creator

    Creates:
        1. AgentSkill record (with full SKILL.md content)
        2. AgentSkillResource records (agents + schemas references)
        3. AgentSkillScript records (all Python utility scripts)
        4. AgentSkillAsset record (eval_review.html template)
        5. Binding to Default Resource Group
    """
    _t = tenant_id
    skill_id = f"skill-system-skill-creator-{_t}"
    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 1. AgentSkill
    # ------------------------------------------------------------------
    existing = db.query(AgentSkill).filter(AgentSkill.id == skill_id).first()

    if existing:
        skill = existing
    else:
        skill_md_content = _get_skill_creator_md()

        skill = AgentSkill(
            id=skill_id,
            tenant_id=tenant_id,
            name="skill-creator",
            description=(
                "Create new skills, modify and improve existing skills, and "
                "measure skill performance. Use when users want to create a "
                "skill from scratch, edit, or optimize an existing skill, run "
                "evals to test a skill, benchmark skill performance with "
                "variance analysis, or optimize a skill's description for "
                "better triggering accuracy."
            ),
            version="1.0.0",
            status=SkillStatus.PUBLISHED,
            enabled=True,
            license="MIT",
            skill_md_content=skill_md_content,
            skill_md_hash=_sha256(skill_md_content),
            skill_metadata={
                "tags": ["meta", "developer-tools", "automation", "skills"],
                "category": "developer-tools",
                "icon_name": "wand-sparkles",
                "approval_required": False,
                "cost_estimate_usd": 0.01,
                "required_llm_models": ["chat"],
            },
            published_at=now,
            published_by="system",
            created_by="system",
            created_at=now,
            updated_at=now,
        )

        db.add(skill)
        db.flush()

        logger.info("Created skill-creator skill", context={
            "skill_id": skill_id, "tenant_id": tenant_id,
        })

    # ------------------------------------------------------------------
    # 2. AgentSkillResource records (agents + schemas references)
    # ------------------------------------------------------------------
    _seed_skill_creator_resources(db, tenant_id, skill_id, now, logger)

    # ------------------------------------------------------------------
    # 3. AgentSkillScript records (Python utility scripts)
    # ------------------------------------------------------------------
    _seed_skill_creator_scripts(db, tenant_id, skill_id, now, logger)

    # ------------------------------------------------------------------
    # 4. AgentSkillAsset record (eval_review.html template)
    # ------------------------------------------------------------------
    _seed_skill_creator_assets(db, tenant_id, skill_id, now, logger)

    # ------------------------------------------------------------------
    # 5. Bind to Default Resource Group
    # ------------------------------------------------------------------
    _bind_skill_to_group(db, skill, default_group_id, logger)


def _seed_skill_creator_resources(
    db: Session,
    tenant_id: str,
    skill_id: str,
    now: datetime,
    logger: BrutorLogger,
) -> None:
    """Seed all reference resources for skill-creator."""
    _t = tenant_id
    resources = [
        {
            "id": f"asr-system-skill-creator-grader-{_t}",
            "filename": "agents/grader.md",
            "resource_type": SkillResourceType.REFERENCE,
            "content": _get_grader_agent_md(),
        },
        {
            "id": f"asr-system-skill-creator-comparator-{_t}",
            "filename": "agents/comparator.md",
            "resource_type": SkillResourceType.REFERENCE,
            "content": _get_comparator_agent_md(),
        },
        {
            "id": f"asr-system-skill-creator-analyzer-{_t}",
            "filename": "agents/analyzer.md",
            "resource_type": SkillResourceType.REFERENCE,
            "content": _get_analyzer_agent_md(),
        },
        {
            "id": f"asr-system-skill-creator-schemas-{_t}",
            "filename": "references/schemas.md",
            "resource_type": SkillResourceType.SCHEMA,
            "content": _get_schemas_reference_md(),
        },
    ]

    for res_def in resources:
        res_id = res_def["id"]
        existing = db.query(AgentSkillResource).filter(
            AgentSkillResource.id == res_id
        ).first()

        if existing:
            continue

        content = res_def["content"]
        resource = AgentSkillResource(
            id=res_id,
            tenant_id=tenant_id,
            skill_id=skill_id,
            resource_type=res_def["resource_type"],
            filename=res_def["filename"],
            content=content,
            content_hash=_sha256(content),
            mime_type="text/markdown",
            size_bytes=len(content.encode("utf-8")),
            created_at=now,
            updated_at=now,
        )

        db.add(resource)
        db.flush()

        logger.info(f"Created skill-creator resource: {res_def['filename']}", context={
            "resource_id": res_id, "skill_id": skill_id,
        })


def _seed_skill_creator_scripts(
    db: Session,
    tenant_id: str,
    skill_id: str,
    now: datetime,
    logger: BrutorLogger,
) -> None:
    """Seed all Python scripts for skill-creator."""
    _t = tenant_id
    scripts = [
        {
            "id": f"ass-system-skill-creator-utils-{_t}",
            "filename": "scripts/utils.py",
            "description": "Shared utilities: parse SKILL.md frontmatter (name, description, content)",
            "content": _get_script_utils(),
            "execution_mode": ScriptExecutionMode.SANDBOX,
        },
        {
            "id": f"ass-system-skill-creator-quick-validate-{_t}",
            "filename": "scripts/quick_validate.py",
            "description": "Quick validation of SKILL.md frontmatter (name, description, format checks)",
            "content": _get_script_quick_validate(),
            "execution_mode": ScriptExecutionMode.SANDBOX,
        },
        {
            "id": f"ass-system-skill-creator-run-eval-{_t}",
            "filename": "scripts/run_eval.py",
            "description": "Run trigger evaluation: tests whether a skill description causes Claude to trigger for a set of queries",
            "content": _get_script_run_eval(),
            "execution_mode": ScriptExecutionMode.TRUSTED,
        },
        {
            "id": f"ass-system-skill-creator-improve-desc-{_t}",
            "filename": "scripts/improve_description.py",
            "description": "Improve a skill description based on eval results by calling Claude",
            "content": _get_script_improve_description(),
            "execution_mode": ScriptExecutionMode.TRUSTED,
        },
        {
            "id": f"ass-system-skill-creator-run-loop-{_t}",
            "filename": "scripts/run_loop.py",
            "description": "Full eval + improve loop with train/test split until all pass or max iterations reached",
            "content": _get_script_run_loop(),
            "execution_mode": ScriptExecutionMode.TRUSTED,
        },
        {
            "id": f"ass-system-skill-creator-aggregate-{_t}",
            "filename": "scripts/aggregate_benchmark.py",
            "description": "Aggregate individual run grading results into benchmark summary statistics (mean, stddev, delta)",
            "content": _get_script_aggregate_benchmark(),
            "execution_mode": ScriptExecutionMode.SANDBOX,
        },
        {
            "id": f"ass-system-skill-creator-gen-report-{_t}",
            "filename": "scripts/generate_report.py",
            "description": "Generate HTML report from run_loop output showing description optimization iterations",
            "content": _get_script_generate_report(),
            "execution_mode": ScriptExecutionMode.SANDBOX,
        },
        {
            "id": f"ass-system-skill-creator-package-{_t}",
            "filename": "scripts/package_skill.py",
            "description": "Package a skill folder into a distributable .skill (zip) file",
            "content": _get_script_package_skill(),
            "execution_mode": ScriptExecutionMode.SANDBOX,
        },
        {
            "id": f"ass-system-skill-creator-gen-review-{_t}",
            "filename": "eval-viewer/generate_review.py",
            "description": "Generate and serve HTML review page for eval results with feedback collection",
            "content": _get_script_generate_review(),
            "execution_mode": ScriptExecutionMode.TRUSTED,
        },
    ]

    for script_def in scripts:
        script_id = script_def["id"]
        existing = db.query(AgentSkillScript).filter(
            AgentSkillScript.id == script_id
        ).first()

        if existing:
            continue

        content = script_def["content"]
        script = AgentSkillScript(
            id=script_id,
            tenant_id=tenant_id,
            skill_id=skill_id,
            filename=script_def["filename"],
            description=script_def["description"],
            script_content=content,
            script_hash=_sha256(content),
            language=ScriptLanguage.PYTHON,
            execution_mode=script_def["execution_mode"],
            created_at=now,
            updated_at=now,
        )

        db.add(script)
        db.flush()

        logger.info(f"Created skill-creator script: {script_def['filename']}", context={
            "script_id": script_id, "skill_id": skill_id,
        })


def _seed_skill_creator_assets(
    db: Session,
    tenant_id: str,
    skill_id: str,
    now: datetime,
    logger: BrutorLogger,
) -> None:
    """Seed assets for skill-creator (eval review HTML template)."""
    _t = tenant_id
    asset_id = f"asa-system-skill-creator-eval-review-{_t}"
    existing = db.query(AgentSkillAsset).filter(
        AgentSkillAsset.id == asset_id
    ).first()

    if existing:
        return

    content = _get_asset_eval_review_html()
    asset = AgentSkillAsset(
        id=asset_id,
        tenant_id=tenant_id,
        skill_id=skill_id,
        asset_type=AssetType.TEMPLATE,
        filename="assets/eval_review.html",
        description="HTML template for eval review page with query editing and export",
        content=content,
        content_hash=_sha256(content),
        mime_type="text/html",
        size_bytes=len(content.encode("utf-8")),
        encoding="utf-8",
        created_at=now,
        updated_at=now,
    )

    db.add(asset)
    db.flush()

    logger.info("Created skill-creator eval_review asset", context={
        "asset_id": asset_id, "skill_id": skill_id,
    })


# ======================================================================
# Skill-Creator Content Helpers
# ======================================================================

def _get_skill_creator_md() -> str:
    """Return the full SKILL.md content for skill-creator.

    Based on the official Anthropic skill-creator SKILL.md.
    Adapted for the Brutor AI platform context.
    """
    return '''\
---
name: skill-creator
description: >
  Create new skills, modify and improve existing skills, and measure skill
  performance. Use when users want to create a skill from scratch, edit, or
  optimize an existing skill, run evals to test a skill, benchmark skill
  performance with variance analysis, or optimize a skill's description for
  better triggering accuracy.
---

# Skill Creator

A skill for creating new skills and iteratively improving them.

At a high level, the process of creating a skill goes like this:

- Decide what you want the skill to do and roughly how it should do it
- Write a draft of the skill
- Create a few test prompts and run claude-with-access-to-the-skill on them
- Help the user evaluate the results both qualitatively and quantitatively
  - While the runs happen in the background, draft some quantitative evals if there aren't any (if there are some, you can either use as is or modify if you feel something needs to change about them). Then explain them to the user (or if they already existed, explain the ones that already exist)
  - Use the `eval-viewer/generate_review.py` script to show the user the results for them to look at, and also let them look at the quantitative metrics
- Rewrite the skill based on feedback from the user's evaluation of the results (and also if there are any glaring flaws that become apparent from the quantitative benchmarks)
- Repeat until you're satisfied
- Expand the test set and try again at larger scale

Your job when using this skill is to figure out where the user is in this process and then jump in and help them progress through these stages. So for instance, maybe they're like "I want to make a skill for X". You can help narrow down what they mean, write a draft, write the test cases, figure out how they want to evaluate, run all the prompts, and repeat.

On the other hand, maybe they already have a draft of the skill. In this case you can go straight to the eval/iterate part of the loop.

Of course, you should always be flexible and if the user is like "I don't need to run a bunch of evaluations, just vibe with me", you can do that instead.

Then after the skill is done (but again, the order is flexible), you can also run the skill description improver, which we have a whole separate script for, to optimize the triggering of the skill.

## Communicating with the user

The skill creator is liable to be used by people across a wide range of familiarity with coding jargon. Please pay attention to context cues to understand how to phrase your communication! In the default case:

- "evaluation" and "benchmark" are borderline, but OK
- for "JSON" and "assertion" you want to see serious cues from the user that they know what those things are before using them without explaining them

It's OK to briefly explain terms if you're in doubt.

---

## Creating a skill

### Capture Intent

Start by understanding the user's intent. The current conversation might already contain a workflow the user wants to capture (e.g., they say "turn this into a skill"). If so, extract answers from the conversation history first -- the tools used, the sequence of steps, corrections the user made, input/output formats observed. The user may need to fill the gaps, and should confirm before proceeding to the next step.

1. What should this skill enable Claude to do?
2. When should this skill trigger? (what user phrases/contexts)
3. What's the expected output format?
4. Should we set up test cases to verify the skill works? Skills with objectively verifiable outputs (file transforms, data extraction, code generation, fixed workflow steps) benefit from test cases. Skills with subjective outputs (writing style, art) often don't need them. Suggest the appropriate default based on the skill type, but let the user decide.

### Interview and Research

Proactively ask questions about edge cases, input/output formats, example files, success criteria, and dependencies. Wait to write test prompts until you've got this part ironed out.

Check available MCPs - if useful for research (searching docs, finding similar skills, looking up best practices), research in parallel via subagents if available, otherwise inline.

### Write the SKILL.md

Based on the user interview, fill in these components:

- **name**: Skill identifier
- **description**: When to trigger, what it does. This is the primary triggering mechanism - include both what the skill does AND specific contexts for when to use it. All "when to use" info goes here, not in the body. Make the description a little bit "pushy" to combat undertriggering.
- **compatibility**: Required tools, dependencies (optional, rarely needed)
- **the rest of the skill :)**

### Skill Writing Guide

#### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

#### Progressive Disclosure

Skills use a three-level loading system:
1. **Metadata** (name + description) - Always in context (~100 words)
2. **SKILL.md body** - In context whenever skill triggers (<500 lines ideal)
3. **Bundled resources** - As needed (unlimited, scripts can execute without loading)

**Key patterns:**
- Keep SKILL.md under 500 lines; if approaching this limit, add hierarchy with clear pointers about where to follow up.
- Reference files clearly from SKILL.md with guidance on when to read them
- For large reference files (>300 lines), include a table of contents

**Domain organization**: When a skill supports multiple domains/frameworks, organize by variant:
```
cloud-deploy/
├── SKILL.md (workflow + selection)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```
Claude reads only the relevant reference file.

#### Principle of Lack of Surprise

Skills must not contain malware, exploit code, or any content that could compromise system security. A skill's contents should not surprise the user in their intent if described.

#### Writing Patterns

Prefer using the imperative form in instructions.

**Defining output formats** - You can do it like this:
```markdown
## Report structure
ALWAYS use this exact template:
# [Title]
## Executive summary
## Key findings
## Recommendations
```

**Examples pattern** - Include examples where useful:
```markdown
## Commit message format
**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

### Writing Style

Try to explain to the model why things are important in lieu of heavy-handed musty MUSTs. Use theory of mind and try to make the skill general and not super-narrow to specific examples. Start by writing a draft and then look at it with fresh eyes and improve it.

### Test Cases

After writing the skill draft, come up with 2-3 realistic test prompts. Share them with the user for confirmation. Then run them.

Save test cases to `evals/evals.json`. Don't write assertions yet -- just the prompts. You'll draft assertions in the next step while the runs are in progress.

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's task prompt",
      "expected_output": "Description of expected result",
      "files": []
    }
  ]
}
```

See `references/schemas.md` for the full schema.

## Running and evaluating test cases

This section is one continuous sequence -- don't stop partway through.

Put results in `<skill-name>-workspace/` as a sibling to the skill directory. Within the workspace, organize results by iteration (`iteration-1/`, `iteration-2/`, etc.) and within that, each test case gets a directory (`eval-0/`, `eval-1/`, etc.).

### Step 1: Spawn all runs (with-skill AND baseline) in the same turn

For each test case, spawn two subagents in the same turn -- one with the skill, one without. Launch everything at once so it all finishes around the same time.

**With-skill run:**
```
Execute this task:
- Skill path: <path-to-skill>
- Task: <eval prompt>
- Input files: <eval files if any, or "none">
- Save outputs to: <workspace>/iteration-<N>/eval-<ID>/with_skill/outputs/
- Outputs to save: <what the user cares about>
```

**Baseline run** (same prompt, but the baseline depends on context):
- **Creating a new skill**: no skill at all. Same prompt, no skill path, save to `without_skill/outputs/`.
- **Improving an existing skill**: the old version. Snapshot the skill, point baseline subagent at snapshot. Save to `old_skill/outputs/`.

Write an `eval_metadata.json` for each test case:
```json
{
  "eval_id": 0,
  "eval_name": "descriptive-name-here",
  "prompt": "The user's task prompt",
  "assertions": []
}
```

### Step 2: While runs are in progress, draft assertions

Draft quantitative assertions for each test case and explain them to the user. Good assertions are objectively verifiable and have descriptive names.

Update the `eval_metadata.json` files and `evals/evals.json` with the assertions once drafted.

### Step 3: As runs complete, capture timing data

When each subagent task completes, save timing data immediately to `timing.json`:
```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3
}
```

### Step 4: Grade, aggregate, and launch the viewer

Once all runs are done:

1. **Grade each run** -- spawn a grader subagent that reads `agents/grader.md` and evaluates each assertion against the outputs. Save results to `grading.json` in each run directory.

2. **Aggregate into benchmark** -- run the aggregation script:
   ```bash
   python -m scripts.aggregate_benchmark <workspace>/iteration-N --skill-name <name>
   ```

3. **Do an analyst pass** -- read the benchmark data and surface patterns. See `agents/analyzer.md` for what to look for.

4. **Launch the viewer** with both qualitative outputs and quantitative data:
   ```bash
   nohup python eval-viewer/generate_review.py \\
     <workspace>/iteration-N \\
     --skill-name "my-skill" \\
     --benchmark <workspace>/iteration-N/benchmark.json \\
     > /dev/null 2>&1 &
   VIEWER_PID=$!
   ```
   For iteration 2+, also pass `--previous-workspace <workspace>/iteration-<N-1>`.

5. **Tell the user** the results are ready for review.

### Step 5: Read the feedback

When the user is done, read `feedback.json`:
```json
{
  "reviews": [
    {"run_id": "eval-0-with_skill", "feedback": "the chart is missing axis labels", "timestamp": "..."}
  ],
  "status": "complete"
}
```

Empty feedback means the user thought it was fine.

---

## Improving the skill

This is the heart of the loop.

### How to think about improvements

1. **Generalize from the feedback.** We're trying to create skills that can be used many times across many prompts. Rather than put in fiddly overfitty changes, if there's a stubborn issue, try branching out and using different metaphors or recommending different patterns.

2. **Keep the prompt lean.** Remove things that aren't pulling their weight. Read the transcripts, not just final outputs.

3. **Explain the why.** Try hard to explain the **why** behind everything. Today's LLMs are smart. If you find yourself writing ALWAYS or NEVER in all caps, reframe and explain the reasoning.

4. **Look for repeated work across test cases.** If all test cases resulted in writing similar helper scripts, that's a signal the skill should bundle that script.

### The iteration loop

After improving the skill:

1. Apply improvements to the skill
2. Rerun all test cases into a new `iteration-<N+1>/` directory, including baseline runs
3. Launch the reviewer with `--previous-workspace` pointing at the previous iteration
4. Wait for the user to review
5. Read the new feedback, improve again, repeat

Keep going until the user is happy or feedback is all empty.

---

## Advanced: Blind comparison

For rigorous comparison between two versions, there's a blind comparison system. Read `agents/comparator.md` and `agents/analyzer.md` for details.

This is optional and most users won't need it.

---

## Description Optimization

The description field in SKILL.md frontmatter is the primary mechanism that determines whether Claude invokes a skill. After creating or improving a skill, offer to optimize the description for better triggering accuracy.

### Step 1: Generate trigger eval queries

Create 20 eval queries -- a mix of should-trigger and should-not-trigger. Save as JSON:
```json
[
  {"query": "the user prompt", "should_trigger": true},
  {"query": "another prompt", "should_trigger": false}
]
```

The queries must be realistic. Focus on edge cases rather than clear-cut ones. For the should-not-trigger queries, the most valuable ones are near-misses.

### Step 2: Review with user

Present the eval set to the user for review using the HTML template:

1. Read the template from `assets/eval_review.html`
2. Replace placeholders: `__EVAL_DATA_PLACEHOLDER__`, `__SKILL_NAME_PLACEHOLDER__`, `__SKILL_DESCRIPTION_PLACEHOLDER__`
3. Write to a temp file and open it
4. The user can edit queries, toggle should-trigger, add/remove entries, then export

### Step 3: Run the optimization loop

```bash
python -m scripts.run_loop \\
  --eval-set <path-to-trigger-eval.json> \\
  --skill-path <path-to-skill> \\
  --model <model-id-powering-this-session> \\
  --max-iterations 5 \\
  --verbose
```

This handles the full optimization loop automatically. It splits the eval set into 60% train and 40% held-out test, evaluates the current description, then calls Claude to propose improvements. It iterates up to 5 times and returns JSON with `best_description`.

### Step 4: Apply the result

Take `best_description` from the JSON output and update the skill's SKILL.md frontmatter.

---

## Reference files

The agents/ directory contains instructions for specialized subagents. Read them when you need to spawn the relevant subagent.

- `agents/grader.md` -- How to evaluate assertions against outputs
- `agents/comparator.md` -- How to do blind A/B comparison between two outputs
- `agents/analyzer.md` -- How to analyze why one version beat another

The references/ directory has additional documentation:
- `references/schemas.md` -- JSON structures for evals.json, grading.json, etc.

---

Repeating one more time the core loop here for emphasis:

- Figure out what the skill is about
- Draft or edit the skill
- Run claude-with-access-to-the-skill on test prompts
- With the user, evaluate the outputs:
  - Create benchmark.json and run `eval-viewer/generate_review.py` to help the user review them
  - Run quantitative evals
- Repeat until you and the user are satisfied
- Package the final skill and return it to the user.

Good luck!
'''


def _get_grader_agent_md() -> str:
    """Return the grader agent instructions."""
    return '''\
# Grader Agent

Evaluate expectations against an execution transcript and outputs.

## Role

The Grader reviews a transcript and output files, then determines whether each expectation passes or fails. Provide clear evidence for each judgment.

You have two jobs: grade the outputs, and critique the evals themselves. A passing grade on a weak assertion is worse than useless -- it creates false confidence. When you notice an assertion that's trivially satisfied, or an important outcome that no assertion checks, say so.

## Inputs

You receive these parameters in your prompt:

- **expectations**: List of expectations to evaluate (strings)
- **transcript_path**: Path to the execution transcript (markdown file)
- **outputs_dir**: Directory containing output files from execution

## Process

### Step 1: Read the Transcript

1. Read the transcript file completely
2. Note the eval prompt, execution steps, and final result
3. Identify any issues or errors documented

### Step 2: Examine Output Files

1. List files in outputs_dir
2. Read/examine each file relevant to the expectations
3. Note contents, structure, and quality

### Step 3: Evaluate Each Assertion

For each expectation:

1. **Search for evidence** in the transcript and outputs
2. **Determine verdict**:
   - **PASS**: Clear evidence the expectation is true AND the evidence reflects genuine task completion
   - **FAIL**: No evidence, or evidence contradicts the expectation, or evidence is superficial
3. **Cite the evidence**: Quote the specific text or describe what you found

### Step 4: Extract and Verify Claims

Beyond predefined expectations, extract implicit claims from outputs and verify them:

1. **Extract claims**: Factual, process, and quality claims
2. **Verify each claim**: Against outputs, transcript, or external sources
3. **Flag unverifiable claims**

### Step 5: Read User Notes

If `{outputs_dir}/user_notes.md` exists, read it and note any uncertainties.

### Step 6: Critique the Evals

After grading, consider whether the evals could be improved. Good suggestions test meaningful outcomes -- assertions that are hard to satisfy without actually doing the work correctly.

### Step 7: Write Grading Results

Save results to `{outputs_dir}/../grading.json` (sibling to outputs_dir).

## Grading Criteria

**PASS when**:
- Transcript or outputs clearly demonstrate the expectation is true
- Specific evidence can be cited
- Evidence reflects genuine substance, not just surface compliance

**FAIL when**:
- No evidence found
- Evidence contradicts the expectation
- Evidence is superficial
- Cannot be verified from available information

**When uncertain**: The burden of proof to pass is on the expectation.

### Step 8: Read Executor Metrics and Timing

If `{outputs_dir}/metrics.json` or `{outputs_dir}/../timing.json` exist, read and include them.

## Output Format

Write a JSON file with this structure:

```json
{
  "expectations": [
    {
      "text": "The output includes the name 'John Smith'",
      "passed": true,
      "evidence": "Found in transcript Step 3: 'Extracted names: John Smith, Sarah Johnson'"
    }
  ],
  "summary": {
    "passed": 2,
    "failed": 1,
    "total": 3,
    "pass_rate": 0.67
  },
  "execution_metrics": {
    "tool_calls": {"Read": 5, "Write": 2, "Bash": 8},
    "total_tool_calls": 15,
    "total_steps": 6,
    "errors_encountered": 0,
    "output_chars": 12450,
    "transcript_chars": 3200
  },
  "timing": {
    "executor_duration_seconds": 165.0,
    "grader_duration_seconds": 26.0,
    "total_duration_seconds": 191.0
  },
  "claims": [
    {
      "claim": "The form has 12 fillable fields",
      "type": "factual",
      "verified": true,
      "evidence": "Counted 12 fields in field_info.json"
    }
  ],
  "user_notes_summary": {
    "uncertainties": [],
    "needs_review": [],
    "workarounds": []
  },
  "eval_feedback": {
    "suggestions": [],
    "overall": "No suggestions, evals look solid"
  }
}
```

## Guidelines

- **Be objective**: Base verdicts on evidence, not assumptions
- **Be specific**: Quote the exact text that supports your verdict
- **Be thorough**: Check both transcript and output files
- **Be consistent**: Apply the same standard to each expectation
- **Explain failures**: Make it clear why evidence was insufficient
- **No partial credit**: Each expectation is pass or fail
'''


def _get_comparator_agent_md() -> str:
    """Return the blind comparator agent instructions."""
    return '''\
# Blind Comparator Agent

Compare two outputs WITHOUT knowing which skill produced them. Judge purely on output quality.

## Role

You are given two outputs (A and B) from the same evaluation task. Your job is to determine which output better accomplishes the task, without bias.

## Process

### Step 1: Read Both Outputs
Read output A and output B completely.

### Step 2: Understand the Task
Read the eval prompt and understand what was being asked.

### Step 3: Generate Evaluation Rubric
Create a task-specific rubric with two dimensions:
- **Content**: Correctness, completeness, accuracy
- **Structure**: Organization, formatting, usability

Each criterion gets a 1-5 score.

### Step 4: Score Each Output
Apply the rubric to both outputs. Generate dimension totals and an overall 1-10 score.

### Step 5: Check Assertions (if provided)
If assertions are provided, check them against both outputs.

### Step 6: Determine Winner
Declare a winner based on overall quality. Be decisive -- ties are rare.

### Step 7: Document Results
Save as JSON with winner, reasoning, rubric scores, and quality summaries.

## Guidelines

- Stay blind: DO NOT try to infer which skill produced which output
- Judge purely on output quality
- Prioritize output quality over assertion pass rates
- Be decisive -- if one output is better, say so
- Provide concrete examples to support your judgment
'''


def _get_analyzer_agent_md() -> str:
    """Return the post-hoc analyzer agent instructions."""
    return '''\
# Post-hoc Analyzer Agent

Two modes of operation:

## 1. Post-hoc Comparison Analysis

After a blind comparison, analyze why one output won and provide improvement suggestions.

### Process
1. Read the comparator's reasoning and scoring
2. Examine both skills' documentation
3. Analyze execution transcripts for behavioral patterns
4. Evaluate instruction-following fidelity (score 1-10)
5. Identify specific strengths and weaknesses with evidence
6. Generate prioritized, concrete improvement suggestions

The goal is to improve the losing skill, not critique the agent. Suggestions must be specific, grounded in evidence, and organized by expected impact.

## 2. Benchmark Analysis

When reviewing multiple benchmark runs, surface patterns invisible in aggregate metrics:

- Per-assertion consistency across runs (always pass/fail vs. variable)
- Cross-evaluation difficulty patterns
- Resource usage variance and anomalies
- Flaky or non-deterministic behaviors

Output observational notes grounded in data. Avoid speculation or skill improvement suggestions in this mode.

## Guidelines

- Be specific and actionable
- Ground everything in evidence from skills and transcripts
- Prioritize suggestions by expected impact
- Distinguish between correlation and causation
'''


def _get_schemas_reference_md() -> str:
    """Return the JSON schemas reference document."""
    return '''\
# JSON Schemas Reference

## evals.json

Defines evaluation test cases:

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's task prompt",
      "expected_output": "Description of expected result",
      "files": [],
      "assertions": [
        "The output includes X",
        "File Y was created with correct content"
      ]
    }
  ]
}
```

## history.json

Tracks iterative skill improvements:

```json
[
  {
    "iteration": 1,
    "description": "Current description text",
    "passed": 15,
    "failed": 5,
    "total": 20,
    "parent": null,
    "grading_result": "baseline"
  }
]
```

## grading.json

Captures grader assessment:

```json
{
  "expectations": [
    {
      "text": "Assertion text",
      "passed": true,
      "evidence": "Specific evidence"
    }
  ],
  "summary": {
    "passed": 2,
    "failed": 1,
    "total": 3,
    "pass_rate": 0.67
  },
  "execution_metrics": {
    "tool_calls": {},
    "total_tool_calls": 0,
    "total_steps": 0,
    "errors_encountered": 0,
    "output_chars": 0,
    "transcript_chars": 0
  },
  "timing": {
    "executor_duration_seconds": 0,
    "grader_duration_seconds": 0,
    "total_duration_seconds": 0
  },
  "claims": [],
  "user_notes_summary": {
    "uncertainties": [],
    "needs_review": [],
    "workarounds": []
  },
  "eval_feedback": {
    "suggestions": [],
    "overall": ""
  }
}
```

## metrics.json

Documents executor performance:

```json
{
  "tool_calls": {"Read": 5, "Write": 2, "Bash": 8},
  "total_tool_calls": 15,
  "total_steps": 6,
  "files_created": ["output.txt"],
  "errors_encountered": 0,
  "output_chars": 12450,
  "transcript_chars": 3200
}
```

## timing.json

Preserves wall-clock duration and token data:

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3,
  "executor": {
    "total_tokens": 80000,
    "duration_ms": 20000,
    "started_at": "2026-01-15T10:30:00Z",
    "completed_at": "2026-01-15T10:30:20Z"
  },
  "grader": {
    "total_tokens": 4852,
    "duration_ms": 3332,
    "started_at": "2026-01-15T10:30:20Z",
    "completed_at": "2026-01-15T10:30:23Z"
  }
}
```

## benchmark.json

Comparative performance across configurations:

```json
{
  "metadata": {
    "skill_name": "my-skill",
    "skill_path": "path/to/skill",
    "executor_model": "model-name",
    "analyzer_model": "model-name",
    "timestamp": "2026-01-15T10:30:00Z",
    "evals_run": [0, 1, 2],
    "runs_per_configuration": 3
  },
  "runs": [
    {
      "eval_id": 0,
      "configuration": "with_skill",
      "run_number": 1,
      "result": {
        "pass_rate": 0.8,
        "passed": 4,
        "failed": 1,
        "total": 5,
        "time_seconds": 23.3,
        "tokens": 84852,
        "tool_calls": 15,
        "errors": 0
      },
      "expectations": [],
      "notes": []
    }
  ],
  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.8, "stddev": 0.1, "min": 0.6, "max": 1.0},
      "time_seconds": {"mean": 23.3, "stddev": 5.0, "min": 18.0, "max": 30.0},
      "tokens": {"mean": 84852, "stddev": 10000, "min": 70000, "max": 95000}
    },
    "without_skill": {
      "pass_rate": {"mean": 0.4, "stddev": 0.2, "min": 0.2, "max": 0.6},
      "time_seconds": {"mean": 30.0, "stddev": 8.0, "min": 20.0, "max": 40.0},
      "tokens": {"mean": 90000, "stddev": 15000, "min": 70000, "max": 110000}
    },
    "delta": {
      "pass_rate": "+0.40",
      "time_seconds": "-6.7",
      "tokens": "-5148"
    }
  },
  "notes": []
}
```

## comparison.json

Blind comparison outcome:

```json
{
  "winner": "A",
  "reasoning": "Output A was more complete and better organized",
  "rubric": {
    "content": {"A": 4, "B": 3},
    "structure": {"A": 5, "B": 3}
  },
  "overall_scores": {"A": 8, "B": 6},
  "quality_summary": {
    "A": {"strengths": [], "weaknesses": []},
    "B": {"strengths": [], "weaknesses": []}
  },
  "expectations": {
    "A": {"passed": 4, "total": 5},
    "B": {"passed": 3, "total": 5}
  }
}
```

## analysis.json

Post-hoc analysis:

```json
{
  "winning_skill": {
    "instruction_following_score": 9,
    "strengths": [],
    "execution_patterns": []
  },
  "losing_skill": {
    "instruction_following_score": 6,
    "weaknesses": [],
    "execution_patterns": []
  },
  "improvement_suggestions": [
    {
      "priority": "high",
      "suggestion": "Add explicit output format specification",
      "evidence": "Transcript shows model guessing at format",
      "expected_impact": "Should improve structure scores by 1-2 points"
    }
  ]
}
```
'''


# ======================================================================
# Skill-Creator Script Content Helpers
# ======================================================================

def _get_script_utils() -> str:
    """Return scripts/utils.py content."""
    return '''\
"""Shared utilities for skill-creator scripts."""

from pathlib import Path


def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """Parse a SKILL.md file, returning (name, description, full_content)."""
    content = (skill_path / "SKILL.md").read_text()
    lines = content.split("\\n")

    if lines[0].strip() != "---":
        raise ValueError("SKILL.md missing frontmatter (no opening ---)")

    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("SKILL.md missing frontmatter (no closing ---)")

    name = ""
    description = ""
    frontmatter_lines = lines[1:end_idx]
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            value = line[len("description:"):].strip()
            # Handle YAML multiline indicators (>, |, >-, |-)
            if value in (">", "|", ">-", "|-"):
                continuation_lines: list[str] = []
                i += 1
                while i < len(frontmatter_lines) and (
                    frontmatter_lines[i].startswith("  ")
                    or frontmatter_lines[i].startswith("\\t")
                ):
                    continuation_lines.append(frontmatter_lines[i].strip())
                    i += 1
                description = " ".join(continuation_lines)
                continue
            else:
                description = value.strip('"').strip("'")
        i += 1

    return name, description, content
'''


def _get_script_quick_validate() -> str:
    """Return scripts/quick_validate.py content."""
    return '''\
#!/usr/bin/env python3
"""Quick validation script for skills - minimal version."""

import re
import sys

import yaml
from pathlib import Path


def validate_skill(skill_path):
    """Basic validation of a skill."""
    skill_path = Path(skill_path)

    # Check SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    # Read and validate frontmatter
    content = skill_md.read_text()
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    # Extract frontmatter
    match = re.match(r"^---\\n(.*?)\\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    # Parse YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # Check for unexpected properties
    ALLOWED_PROPERTIES = {
        "name", "description", "license", "allowed-tools",
        "metadata", "compatibility",
    }
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"Unexpected key(s) in SKILL.md frontmatter: "
            f"{', '.join(sorted(unexpected_keys))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    # Check required fields
    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    # Validate name
    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if name:
        if not re.match(r"^[a-z0-9-]+$", name):
            return False, f"Name '{name}' should be kebab-case"
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
        if len(name) > 64:
            return False, f"Name is too long ({len(name)} characters). Maximum is 64."

    # Validate description
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if description:
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets (< or >)"
        if len(description) > 1024:
            return False, f"Description is too long ({len(description)} characters). Maximum is 1024."

    return True, "Skill is valid!"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
'''


def _get_script_run_eval() -> str:
    """Return scripts/run_eval.py content."""
    return '''\
#!/usr/bin/env python3
"""Run trigger evaluation for a skill description.

Tests whether a skill description causes Claude to trigger (read the skill)
for a set of queries. Outputs results as JSON.
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from scripts.utils import parse_skill_md


def find_project_root() -> Path:
    """Find the project root by walking up from cwd looking for .claude/."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return current


def run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    project_root: str,
    model: str | None = None,
) -> bool:
    """Run a single query and return whether the skill was triggered."""
    unique_id = uuid.uuid4().hex[:8]
    clean_name = f"{skill_name}-skill-{unique_id}"
    project_commands_dir = Path(project_root) / ".claude" / "commands"
    command_file = project_commands_dir / f"{clean_name}.md"

    try:
        project_commands_dir.mkdir(parents=True, exist_ok=True)
        indented_desc = "\\n  ".join(skill_description.split("\\n"))
        command_content = (
            f"---\\ndescription: |\\n  {indented_desc}\\n---\\n\\n"
            f"# {skill_name}\\n\\n"
            f"This skill handles: {skill_description}\\n"
        )
        command_file.write_text(command_content)

        cmd = [
            "claude", "-p", query,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if model:
            cmd.extend(["--model", model])

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            env=env,
        )

        triggered = False
        start_time = time.time()
        buffer = ""
        pending_tool_name = None
        accumulated_json = ""

        try:
            while time.time() - start_time < timeout:
                if process.poll() is not None:
                    remaining = process.stdout.read()
                    if remaining:
                        buffer += remaining.decode("utf-8", errors="replace")
                    break

                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue

                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\\n" in buffer:
                    line, buffer = buffer.split("\\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") == "stream_event":
                        se = event.get("event", {})
                        se_type = se.get("type", "")

                        if se_type == "content_block_start":
                            cb = se.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tool_name = cb.get("name", "")
                                if tool_name in ("Skill", "Read"):
                                    pending_tool_name = tool_name
                                    accumulated_json = ""
                                else:
                                    return False

                        elif se_type == "content_block_delta" and pending_tool_name:
                            delta = se.get("delta", {})
                            if delta.get("type") == "input_json_delta":
                                accumulated_json += delta.get("partial_json", "")
                                if clean_name in accumulated_json:
                                    return True

                        elif se_type in ("content_block_stop", "message_stop"):
                            if pending_tool_name:
                                return clean_name in accumulated_json
                            if se_type == "message_stop":
                                return False

                    elif event.get("type") == "assistant":
                        message = event.get("message", {})
                        for content_item in message.get("content", []):
                            if content_item.get("type") != "tool_use":
                                continue
                            tool_name = content_item.get("name", "")
                            tool_input = content_item.get("input", {})
                            if tool_name == "Skill" and clean_name in tool_input.get("skill", ""):
                                triggered = True
                            elif tool_name == "Read" and clean_name in tool_input.get("file_path", ""):
                                triggered = True
                            return triggered

                    elif event.get("type") == "result":
                        return triggered
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        return triggered
    finally:
        if command_file.exists():
            command_file.unlink()


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int,
    timeout: int,
    project_root: Path,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
) -> dict:
    """Run the full eval set and return results."""
    results = []

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        future_to_info = {}
        for item in eval_set:
            for run_idx in range(runs_per_query):
                future = executor.submit(
                    run_single_query,
                    item["query"], skill_name, description,
                    timeout, str(project_root), model,
                )
                future_to_info[future] = (item, run_idx)

        query_triggers: dict[str, list[bool]] = {}
        query_items: dict[str, dict] = {}
        for future in as_completed(future_to_info):
            item, _ = future_to_info[future]
            query = item["query"]
            query_items[query] = item
            if query not in query_triggers:
                query_triggers[query] = []
            try:
                query_triggers[query].append(future.result())
            except Exception as e:
                print(f"Warning: query failed: {e}", file=sys.stderr)
                query_triggers[query].append(False)

    for query, triggers in query_triggers.items():
        item = query_items[query]
        trigger_rate = sum(triggers) / len(triggers)
        should_trigger = item["should_trigger"]
        if should_trigger:
            did_pass = trigger_rate >= trigger_threshold
        else:
            did_pass = trigger_rate < trigger_threshold
        results.append({
            "query": query,
            "should_trigger": should_trigger,
            "trigger_rate": trigger_rate,
            "triggers": sum(triggers),
            "runs": len(triggers),
            "pass": did_pass,
        })

    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run trigger evaluation")
    parser.add_argument("--eval-set", required=True)
    parser.add_argument("--skill-path", required=True)
    parser.add_argument("--description", default=None)
    parser.add_argument("--num-workers", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--runs-per-query", type=int, default=3)
    parser.add_argument("--trigger-threshold", type=float, default=0.5)
    parser.add_argument("--model", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, original_description, _ = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    output = run_eval(
        eval_set=eval_set, skill_name=name, description=description,
        num_workers=args.num_workers, timeout=args.timeout,
        project_root=project_root, runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold, model=args.model,
    )

    print(json.dumps(output, indent=2))
'''


def _get_script_improve_description() -> str:
    """Return scripts/improve_description.py content."""
    return '''\
#!/usr/bin/env python3
"""Improve a skill description based on eval results.

Takes eval results (from run_eval.py) and generates an improved description
by calling `claude -p` as a subprocess.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from scripts.utils import parse_skill_md


def _call_claude(prompt: str, model: str | None, timeout: int = 300) -> str:
    """Run `claude -p` with the prompt on stdin and return the text response."""
    cmd = ["claude", "-p", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        env=env, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}\\nstderr: {result.stderr}"
        )
    return result.stdout


def improve_description(
    skill_name: str,
    skill_content: str,
    current_description: str,
    eval_results: dict,
    history: list[dict],
    model: str,
    test_results: dict | None = None,
    log_dir: Path | None = None,
    iteration: int | None = None,
) -> str:
    """Call Claude to improve the description based on eval results."""
    failed_triggers = [
        r for r in eval_results["results"]
        if r["should_trigger"] and not r["pass"]
    ]
    false_triggers = [
        r for r in eval_results["results"]
        if not r["should_trigger"] and not r["pass"]
    ]

    train_score = f"{eval_results['summary']['passed']}/{eval_results['summary']['total']}"
    if test_results:
        test_score = f"{test_results['summary']['passed']}/{test_results['summary']['total']}"
        scores_summary = f"Train: {train_score}, Test: {test_score}"
    else:
        scores_summary = f"Train: {train_score}"

    prompt = f"""You are optimizing a skill description for a skill called "{skill_name}".

The description appears in Claude's "available_skills" list. When a user sends a query, Claude decides whether to invoke the skill based solely on the title and description.

Current description:
<current_description>
"{current_description}"
</current_description>

Current scores ({scores_summary}):
<scores_summary>
"""
    if failed_triggers:
        prompt += "FAILED TO TRIGGER (should have triggered but didn't):\\n"
        for r in failed_triggers:
            prompt += f'  - "{r["query"]}" (triggered {r["triggers"]}/{r["runs"]} times)\\n'
        prompt += "\\n"

    if false_triggers:
        prompt += "FALSE TRIGGERS (triggered but shouldn't have):\\n"
        for r in false_triggers:
            prompt += f'  - "{r["query"]}" (triggered {r["triggers"]}/{r["runs"]} times)\\n'
        prompt += "\\n"

    if history:
        prompt += "PREVIOUS ATTEMPTS (do NOT repeat these):\\n\\n"
        for h in history:
            train_s = f"{h.get('train_passed', h.get('passed', 0))}/{h.get('train_total', h.get('total', 0))}"
            test_s = f"{h.get('test_passed', '?')}/{h.get('test_total', '?')}" if h.get('test_passed') is not None else None
            score_str = f"train={train_s}" + (f", test={test_s}" if test_s else "")
            prompt += f'<attempt {score_str}>\\nDescription: "{h["description"]}"\\n</attempt>\\n\\n'

    prompt += f"""</scores_summary>

Skill content (for context):
<skill_content>
{skill_content}
</skill_content>

Write a new description that is more likely to trigger correctly. Generalize from failures rather than listing specific queries. Stay under 1024 characters (100-200 words).

Tips:
- Phrase in the imperative: "Use this skill for" rather than "this skill does"
- Focus on user intent, not implementation details
- Make it distinctive among competing skills
- Be creative and mix up style across iterations

Respond with only the new description in <new_description> tags."""

    text = _call_claude(prompt, model)

    match = re.search(r"<new_description>(.*?)</new_description>", text, re.DOTALL)
    description = match.group(1).strip().strip('"') if match else text.strip().strip('"')

    # Safety: if over 1024 chars, ask for a shorter rewrite
    if len(description) > 1024:
        shorten_prompt = (
            f"{prompt}\\n\\n---\\n\\n"
            f"Previous attempt was {len(description)} characters (over 1024 limit):\\n\\n"
            f'"{description}"\\n\\n'
            f"Rewrite under 1024 characters. Respond in <new_description> tags."
        )
        shorten_text = _call_claude(shorten_prompt, model)
        match = re.search(r"<new_description>(.*?)</new_description>", shorten_text, re.DOTALL)
        description = match.group(1).strip().strip('"') if match else shorten_text.strip().strip('"')

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"improve_iter_{iteration or 'unknown'}.json"
        log_file.write_text(json.dumps({
            "iteration": iteration,
            "final_description": description,
            "char_count": len(description),
        }, indent=2))

    return description


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Improve a skill description")
    parser.add_argument("--eval-results", required=True)
    parser.add_argument("--skill-path", required=True)
    parser.add_argument("--history", default=None)
    parser.add_argument("--model", required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    skill_path = Path(args.skill_path)
    eval_results = json.loads(Path(args.eval_results).read_text())
    history = json.loads(Path(args.history).read_text()) if args.history else []

    name, _, content = parse_skill_md(skill_path)
    current_description = eval_results["description"]

    new_description = improve_description(
        skill_name=name, skill_content=content,
        current_description=current_description,
        eval_results=eval_results, history=history, model=args.model,
    )

    output = {
        "description": new_description,
        "history": history + [{
            "description": current_description,
            "passed": eval_results["summary"]["passed"],
            "failed": eval_results["summary"]["failed"],
            "total": eval_results["summary"]["total"],
            "results": eval_results["results"],
        }],
    }
    print(json.dumps(output, indent=2))
'''


def _get_script_run_loop() -> str:
    """Return scripts/run_loop.py content."""
    return '''\
#!/usr/bin/env python3
"""Run the eval + improve loop until all pass or max iterations reached.

Combines run_eval.py and improve_description.py in a loop, tracking history
and returning the best description found. Supports train/test split.
"""

import argparse
import json
import random
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from scripts.generate_report import generate_html
from scripts.improve_description import improve_description
from scripts.run_eval import find_project_root, run_eval
from scripts.utils import parse_skill_md


def split_eval_set(
    eval_set: list[dict], holdout: float, seed: int = 42
) -> tuple[list[dict], list[dict]]:
    """Split eval set into train and test sets, stratified by should_trigger."""
    random.seed(seed)
    trigger = [e for e in eval_set if e["should_trigger"]]
    no_trigger = [e for e in eval_set if not e["should_trigger"]]
    random.shuffle(trigger)
    random.shuffle(no_trigger)

    n_trigger_test = max(1, int(len(trigger) * holdout))
    n_no_trigger_test = max(1, int(len(no_trigger) * holdout))

    test_set = trigger[:n_trigger_test] + no_trigger[:n_no_trigger_test]
    train_set = trigger[n_trigger_test:] + no_trigger[n_no_trigger_test:]

    return train_set, test_set


def run_loop(
    eval_set: list[dict],
    skill_path: Path,
    description_override: str | None,
    num_workers: int,
    timeout: int,
    max_iterations: int,
    runs_per_query: int,
    trigger_threshold: float,
    holdout: float,
    model: str,
    verbose: bool,
    live_report_path: Path | None = None,
    log_dir: Path | None = None,
) -> dict:
    """Run the eval + improvement loop."""
    project_root = find_project_root()
    name, original_description, content = parse_skill_md(skill_path)
    current_description = description_override or original_description

    if holdout > 0:
        train_set, test_set = split_eval_set(eval_set, holdout)
    else:
        train_set = eval_set
        test_set = []

    history = []
    exit_reason = "unknown"

    for iteration in range(1, max_iterations + 1):
        if verbose:
            print(f"\\nIteration {iteration}/{max_iterations}: {current_description}", file=sys.stderr)

        all_queries = train_set + test_set
        all_results = run_eval(
            eval_set=all_queries, skill_name=name,
            description=current_description, num_workers=num_workers,
            timeout=timeout, project_root=project_root,
            runs_per_query=runs_per_query,
            trigger_threshold=trigger_threshold, model=model,
        )

        train_queries_set = {q["query"] for q in train_set}
        train_result_list = [r for r in all_results["results"] if r["query"] in train_queries_set]
        test_result_list = [r for r in all_results["results"] if r["query"] not in train_queries_set]

        train_passed = sum(1 for r in train_result_list if r["pass"])
        train_total = len(train_result_list)
        train_summary = {"passed": train_passed, "failed": train_total - train_passed, "total": train_total}
        train_results = {"results": train_result_list, "summary": train_summary}

        if test_set:
            test_passed = sum(1 for r in test_result_list if r["pass"])
            test_total = len(test_result_list)
            test_summary = {"passed": test_passed, "failed": test_total - test_passed, "total": test_total}
            test_results = {"results": test_result_list, "summary": test_summary}
        else:
            test_results = None
            test_summary = None

        history.append({
            "iteration": iteration,
            "description": current_description,
            "train_passed": train_summary["passed"],
            "train_failed": train_summary["failed"],
            "train_total": train_summary["total"],
            "train_results": train_results["results"],
            "test_passed": test_summary["passed"] if test_summary else None,
            "test_failed": test_summary["failed"] if test_summary else None,
            "test_total": test_summary["total"] if test_summary else None,
            "test_results": test_results["results"] if test_results else None,
            "passed": train_summary["passed"],
            "failed": train_summary["failed"],
            "total": train_summary["total"],
            "results": train_results["results"],
        })

        if live_report_path:
            partial_output = {
                "original_description": original_description,
                "best_description": current_description,
                "best_score": "in progress",
                "iterations_run": len(history),
                "holdout": holdout,
                "train_size": len(train_set),
                "test_size": len(test_set),
                "history": history,
            }
            live_report_path.write_text(
                generate_html(partial_output, auto_refresh=True, skill_name=name)
            )

        if train_summary["failed"] == 0:
            exit_reason = f"all_passed (iteration {iteration})"
            break

        if iteration == max_iterations:
            exit_reason = f"max_iterations ({max_iterations})"
            break

        # Improve description based on train results
        blinded_history = [
            {k: v for k, v in h.items() if not k.startswith("test_")}
            for h in history
        ]
        current_description = improve_description(
            skill_name=name, skill_content=content,
            current_description=current_description,
            eval_results=train_results, history=blinded_history,
            model=model, log_dir=log_dir, iteration=iteration,
        )

    # Find best iteration by TEST score (or train if no test set)
    if test_set:
        best = max(history, key=lambda h: h["test_passed"] or 0)
        best_score = f"{best['test_passed']}/{best['test_total']}"
    else:
        best = max(history, key=lambda h: h["train_passed"])
        best_score = f"{best['train_passed']}/{best['train_total']}"

    return {
        "exit_reason": exit_reason,
        "original_description": original_description,
        "best_description": best["description"],
        "best_score": best_score,
        "best_train_score": f"{best['train_passed']}/{best['train_total']}",
        "best_test_score": f"{best['test_passed']}/{best['test_total']}" if test_set else None,
        "final_description": current_description,
        "iterations_run": len(history),
        "holdout": holdout,
        "train_size": len(train_set),
        "test_size": len(test_set),
        "history": history,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run eval + improve loop")
    parser.add_argument("--eval-set", required=True)
    parser.add_argument("--skill-path", required=True)
    parser.add_argument("--description", default=None)
    parser.add_argument("--num-workers", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--runs-per-query", type=int, default=3)
    parser.add_argument("--trigger-threshold", type=float, default=0.5)
    parser.add_argument("--holdout", type=float, default=0.4)
    parser.add_argument("--model", required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report", default="auto")
    parser.add_argument("--results-dir", default=None)
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, _, _ = parse_skill_md(skill_path)

    live_report_path = None
    if args.report != "none":
        if args.report == "auto":
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            live_report_path = Path(tempfile.gettempdir()) / f"skill_description_report_{skill_path.name}_{timestamp}.html"
        else:
            live_report_path = Path(args.report)
        live_report_path.write_text("<html><body><h1>Starting optimization loop...</h1><meta http-equiv='refresh' content='5'></body></html>")
        webbrowser.open(str(live_report_path))

    results_dir = None
    if args.results_dir:
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")
        results_dir = Path(args.results_dir) / timestamp
        results_dir.mkdir(parents=True, exist_ok=True)

    log_dir = results_dir / "logs" if results_dir else None

    output = run_loop(
        eval_set=eval_set, skill_path=skill_path,
        description_override=args.description,
        num_workers=args.num_workers, timeout=args.timeout,
        max_iterations=args.max_iterations,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        holdout=args.holdout, model=args.model,
        verbose=args.verbose, live_report_path=live_report_path,
        log_dir=log_dir,
    )

    json_output = json.dumps(output, indent=2)
    print(json_output)

    if results_dir:
        (results_dir / "results.json").write_text(json_output)

    if live_report_path:
        live_report_path.write_text(generate_html(output, auto_refresh=False, skill_name=name))
        print(f"\\nReport: {live_report_path}", file=sys.stderr)

    if results_dir and live_report_path:
        (results_dir / "report.html").write_text(
            generate_html(output, auto_refresh=False, skill_name=name)
        )
'''


def _get_script_aggregate_benchmark() -> str:
    """Return scripts/aggregate_benchmark.py content."""
    return '''\
#!/usr/bin/env python3
"""Aggregate individual run results into benchmark summary statistics.

Reads grading.json files from run directories and produces:
- run_summary with mean, stddev, min, max for each metric
- delta between with_skill and without_skill configurations

Usage:
    python aggregate_benchmark.py <benchmark_dir>
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


def calculate_stats(values: list[float]) -> dict:
    """Calculate mean, stddev, min, max for a list of values."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}

    n = len(values)
    mean = sum(values) / n
    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def load_run_results(benchmark_dir: Path) -> dict:
    """Load all run results from a benchmark directory."""
    runs_dir = benchmark_dir / "runs"
    if runs_dir.exists():
        search_dir = runs_dir
    elif list(benchmark_dir.glob("eval-*")):
        search_dir = benchmark_dir
    else:
        print(f"No eval directories found in {benchmark_dir}")
        return {}

    results: dict[str, list] = {}

    for eval_idx, eval_dir in enumerate(sorted(search_dir.glob("eval-*"))):
        metadata_path = eval_dir / "eval_metadata.json"
        if metadata_path.exists():
            try:
                with open(metadata_path) as mf:
                    eval_id = json.load(mf).get("eval_id", eval_idx)
            except (json.JSONDecodeError, OSError):
                eval_id = eval_idx
        else:
            try:
                eval_id = int(eval_dir.name.split("-")[1])
            except ValueError:
                eval_id = eval_idx

        for config_dir in sorted(eval_dir.iterdir()):
            if not config_dir.is_dir():
                continue
            if not list(config_dir.glob("run-*")):
                continue
            config = config_dir.name
            if config not in results:
                results[config] = []

            for run_dir in sorted(config_dir.glob("run-*")):
                run_number = int(run_dir.name.split("-")[1])
                grading_file = run_dir / "grading.json"

                if not grading_file.exists():
                    continue

                try:
                    with open(grading_file) as f:
                        grading = json.load(f)
                except json.JSONDecodeError:
                    continue

                result = {
                    "eval_id": eval_id,
                    "run_number": run_number,
                    "pass_rate": grading.get("summary", {}).get("pass_rate", 0.0),
                    "passed": grading.get("summary", {}).get("passed", 0),
                    "failed": grading.get("summary", {}).get("failed", 0),
                    "total": grading.get("summary", {}).get("total", 0),
                }

                timing = grading.get("timing", {})
                result["time_seconds"] = timing.get("total_duration_seconds", 0.0)
                timing_file = run_dir / "timing.json"
                if result["time_seconds"] == 0.0 and timing_file.exists():
                    try:
                        with open(timing_file) as tf:
                            timing_data = json.load(tf)
                        result["time_seconds"] = timing_data.get("total_duration_seconds", 0.0)
                        result["tokens"] = timing_data.get("total_tokens", 0)
                    except json.JSONDecodeError:
                        pass

                metrics = grading.get("execution_metrics", {})
                result["tool_calls"] = metrics.get("total_tool_calls", 0)
                if not result.get("tokens"):
                    result["tokens"] = metrics.get("output_chars", 0)
                result["errors"] = metrics.get("errors_encountered", 0)
                result["expectations"] = grading.get("expectations", [])

                notes_summary = grading.get("user_notes_summary", {})
                notes = []
                notes.extend(notes_summary.get("uncertainties", []))
                notes.extend(notes_summary.get("needs_review", []))
                notes.extend(notes_summary.get("workarounds", []))
                result["notes"] = notes

                results[config].append(result)

    return results


def aggregate_results(results: dict) -> dict:
    """Aggregate run results into summary statistics."""
    run_summary = {}
    configs = list(results.keys())

    for config in configs:
        runs = results.get(config, [])
        if not runs:
            run_summary[config] = {
                "pass_rate": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "time_seconds": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "tokens": {"mean": 0, "stddev": 0, "min": 0, "max": 0},
            }
            continue

        run_summary[config] = {
            "pass_rate": calculate_stats([r["pass_rate"] for r in runs]),
            "time_seconds": calculate_stats([r["time_seconds"] for r in runs]),
            "tokens": calculate_stats([r.get("tokens", 0) for r in runs]),
        }

    if len(configs) >= 2:
        primary = run_summary.get(configs[0], {})
        baseline = run_summary.get(configs[1], {})
    else:
        primary = run_summary.get(configs[0], {}) if configs else {}
        baseline = {}

    run_summary["delta"] = {
        "pass_rate": f"{primary.get('pass_rate', {}).get('mean', 0) - baseline.get('pass_rate', {}).get('mean', 0):+.2f}",
        "time_seconds": f"{primary.get('time_seconds', {}).get('mean', 0) - baseline.get('time_seconds', {}).get('mean', 0):+.1f}",
        "tokens": f"{primary.get('tokens', {}).get('mean', 0) - baseline.get('tokens', {}).get('mean', 0):+.0f}",
    }

    return run_summary


def generate_benchmark(benchmark_dir: Path, skill_name: str = "", skill_path: str = "") -> dict:
    """Generate complete benchmark.json from run results."""
    results = load_run_results(benchmark_dir)
    run_summary = aggregate_results(results)

    runs = []
    for config in results:
        for result in results[config]:
            runs.append({
                "eval_id": result["eval_id"],
                "configuration": config,
                "run_number": result["run_number"],
                "result": {
                    "pass_rate": result["pass_rate"],
                    "passed": result["passed"],
                    "failed": result["failed"],
                    "total": result["total"],
                    "time_seconds": result["time_seconds"],
                    "tokens": result.get("tokens", 0),
                    "tool_calls": result.get("tool_calls", 0),
                    "errors": result.get("errors", 0),
                },
                "expectations": result["expectations"],
                "notes": result["notes"],
            })

    eval_ids = sorted(set(
        r["eval_id"] for config in results.values() for r in config
    ))

    return {
        "metadata": {
            "skill_name": skill_name or "<skill-name>",
            "skill_path": skill_path or "<path/to/skill>",
            "executor_model": "<model-name>",
            "analyzer_model": "<model-name>",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": eval_ids,
            "runs_per_configuration": 3,
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": [],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aggregate benchmark results")
    parser.add_argument("benchmark_dir", type=Path)
    parser.add_argument("--skill-name", default="")
    parser.add_argument("--skill-path", default="")
    parser.add_argument("--output", "-o", type=Path, default=None)
    args = parser.parse_args()

    if not args.benchmark_dir.exists():
        print(f"Directory not found: {args.benchmark_dir}")
        sys.exit(1)

    benchmark = generate_benchmark(args.benchmark_dir, args.skill_name, args.skill_path)
    output_json = args.output or (args.benchmark_dir / "benchmark.json")

    with open(output_json, "w") as f:
        json.dump(benchmark, f, indent=2)
    print(f"Generated: {output_json}")
'''


def _get_script_generate_report() -> str:
    """Return scripts/generate_report.py content."""
    return '''\
#!/usr/bin/env python3
"""Generate an HTML report from run_loop.py output.

Takes the JSON output from run_loop.py and generates a visual HTML report
showing each description attempt with check/x for each test case.
"""

import argparse
import html
import json
import sys
from pathlib import Path


def generate_html(data: dict, auto_refresh: bool = False, skill_name: str = "") -> str:
    """Generate HTML report from loop output data."""
    history = data.get("history", [])
    title_prefix = html.escape(skill_name + " -- ") if skill_name else ""

    train_queries: list[dict] = []
    test_queries: list[dict] = []
    if history:
        for r in history[0].get("train_results", history[0].get("results", [])):
            train_queries.append({"query": r["query"], "should_trigger": r.get("should_trigger", True)})
        if history[0].get("test_results"):
            for r in history[0].get("test_results", []):
                test_queries.append({"query": r["query"], "should_trigger": r.get("should_trigger", True)})

    refresh_tag = '    <meta http-equiv="refresh" content="5">\\n' if auto_refresh else ""

    html_parts = [f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
{refresh_tag}    <title>{title_prefix}Skill Description Optimization</title>
    <style>
        body {{ font-family: Georgia, serif; max-width: 100%; margin: 0 auto; padding: 20px; background: #faf9f5; color: #141413; }}
        h1 {{ font-family: sans-serif; }}
        .summary {{ background: white; padding: 15px; border-radius: 6px; margin-bottom: 20px; border: 1px solid #e8e6dc; }}
        .best {{ color: #788c5d; font-weight: bold; }}
        table {{ border-collapse: collapse; background: white; border: 1px solid #e8e6dc; font-size: 12px; min-width: 100%; }}
        th, td {{ padding: 8px; text-align: left; border: 1px solid #e8e6dc; }}
        th {{ background: #141413; color: #faf9f5; font-weight: 500; }}
        th.test-col {{ background: #6a9bcc; }}
        td.description {{ font-family: monospace; font-size: 11px; max-width: 400px; word-wrap: break-word; }}
        td.result {{ text-align: center; font-size: 16px; min-width: 40px; }}
        .pass {{ color: #788c5d; }}
        .fail {{ color: #c44; }}
        .score {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 11px; }}
        .score-good {{ background: #eef2e8; color: #788c5d; }}
        .score-ok {{ background: #fef3c7; color: #d97706; }}
        .score-bad {{ background: #fceaea; color: #c44; }}
        .best-row {{ background: #f5f8f2; }}
    </style>
</head>
<body>
    <h1>{title_prefix}Skill Description Optimization</h1>
    <div class="summary">
        <p><strong>Original:</strong> {html.escape(data.get('original_description', 'N/A'))}</p>
        <p class="best"><strong>Best:</strong> {html.escape(data.get('best_description', 'N/A'))}</p>
        <p><strong>Best Score:</strong> {data.get('best_score', 'N/A')}</p>
        <p><strong>Iterations:</strong> {data.get('iterations_run', 0)} | <strong>Train:</strong> {data.get('train_size', '?')} | <strong>Test:</strong> {data.get('test_size', '?')}</p>
    </div>
    <div style="overflow-x: auto;">
    <table>
        <thead><tr><th>Iter</th><th>Train</th><th>Test</th><th>Description</th>"""]

    for qinfo in train_queries:
        html_parts.append(f'<th>{html.escape(qinfo["query"])}</th>')
    for qinfo in test_queries:
        html_parts.append(f'<th class="test-col">{html.escape(qinfo["query"])}</th>')

    html_parts.append("</tr></thead><tbody>")

    if test_queries:
        best_iter = max(history, key=lambda h: h.get("test_passed") or 0).get("iteration")
    else:
        best_iter = max(history, key=lambda h: h.get("train_passed", h.get("passed", 0))).get("iteration") if history else None

    for h in history:
        iteration = h.get("iteration", "?")
        train_results = h.get("train_results", h.get("results", []))
        test_results = h.get("test_results", [])
        description = h.get("description", "")

        train_by_query = {r["query"]: r for r in train_results}
        test_by_query = {r["query"]: r for r in test_results} if test_results else {}

        def aggregate_runs(results):
            correct = total = 0
            for r in results:
                runs = r.get("runs", 0)
                triggers = r.get("triggers", 0)
                total += runs
                correct += triggers if r.get("should_trigger", True) else runs - triggers
            return correct, total

        train_correct, train_runs = aggregate_runs(train_results)
        test_correct, test_runs = aggregate_runs(test_results)

        def score_class(c, t):
            if t > 0:
                ratio = c / t
                if ratio >= 0.8: return "score-good"
                elif ratio >= 0.5: return "score-ok"
            return "score-bad"

        row_class = "best-row" if iteration == best_iter else ""
        html_parts.append(f'<tr class="{row_class}"><td>{iteration}</td>')
        html_parts.append(f'<td><span class="score {score_class(train_correct, train_runs)}">{train_correct}/{train_runs}</span></td>')
        html_parts.append(f'<td><span class="score {score_class(test_correct, test_runs)}">{test_correct}/{test_runs}</span></td>')
        html_parts.append(f'<td class="description">{html.escape(description)}</td>')

        for qinfo in train_queries:
            r = train_by_query.get(qinfo["query"], {})
            icon = "\\u2713" if r.get("pass", False) else "\\u2717"
            css = "pass" if r.get("pass", False) else "fail"
            html_parts.append(f'<td class="result {css}">{icon}</td>')

        for qinfo in test_queries:
            r = test_by_query.get(qinfo["query"], {})
            icon = "\\u2713" if r.get("pass", False) else "\\u2717"
            css = "pass" if r.get("pass", False) else "fail"
            html_parts.append(f'<td class="result {css}">{icon}</td>')

        html_parts.append("</tr>")

    html_parts.append("</tbody></table></div></body></html>")
    return "".join(html_parts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HTML report from run_loop output")
    parser.add_argument("input", help="Path to JSON output from run_loop.py (or - for stdin)")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--skill-name", default="")
    args = parser.parse_args()

    data = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text())
    html_output = generate_html(data, skill_name=args.skill_name)

    if args.output:
        Path(args.output).write_text(html_output)
    else:
        print(html_output)
'''


def _get_script_package_skill() -> str:
    """Return scripts/package_skill.py content."""
    return '''\
#!/usr/bin/env python3
"""Skill Packager - Creates a distributable .skill file of a skill folder.

Usage:
    python package_skill.py <path/to/skill-folder> [output-directory]
"""

import fnmatch
import sys
import zipfile
from pathlib import Path
from scripts.quick_validate import validate_skill

EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def package_skill(skill_path, output_dir=None):
    """Package a skill folder into a .skill file."""
    skill_path = Path(skill_path).resolve()

    if not skill_path.exists() or not skill_path.is_dir():
        print(f"Error: Invalid skill folder: {skill_path}")
        return None

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"Error: SKILL.md not found in {skill_path}")
        return None

    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"Validation failed: {message}")
        return None

    skill_name = skill_path.name
    output_path = Path(output_dir).resolve() if output_dir else Path.cwd()
    output_path.mkdir(parents=True, exist_ok=True)
    skill_filename = output_path / f"{skill_name}.skill"

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in skill_path.rglob("*"):
                if not file_path.is_file():
                    continue
                arcname = file_path.relative_to(skill_path.parent)
                if should_exclude(arcname):
                    continue
                zipf.write(file_path, arcname)

        print(f"Packaged skill to: {skill_filename}")
        return skill_filename
    except Exception as e:
        print(f"Error creating .skill file: {e}")
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python package_skill.py <path/to/skill-folder> [output-directory]")
        sys.exit(1)

    result = package_skill(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    sys.exit(0 if result else 1)
'''


def _get_script_generate_review() -> str:
    """Return eval-viewer/generate_review.py content."""
    return '''\
#!/usr/bin/env python3
"""Generate and serve a review page for eval results.

Reads the workspace directory, discovers runs, embeds all output data
into a self-contained HTML page, and serves it via a tiny HTTP server.

Usage:
    python generate_review.py <workspace-path> [--port PORT] [--skill-name NAME]
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
import webbrowser
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


def get_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def find_runs(workspace: Path) -> list[dict]:
    """Recursively find directories that contain an outputs/ subdirectory."""
    runs: list[dict] = []
    _find_runs_recursive(workspace, workspace, runs)
    runs.sort(key=lambda r: (r.get("eval_id", float("inf")), r["id"]))
    return runs


def _find_runs_recursive(root: Path, current: Path, runs: list[dict]) -> None:
    if not current.is_dir():
        return
    outputs_dir = current / "outputs"
    if outputs_dir.is_dir():
        run = build_run(root, current)
        if run:
            runs.append(run)
        return
    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs"}
    for child in sorted(current.iterdir()):
        if child.is_dir() and child.name not in skip:
            _find_runs_recursive(root, child, runs)


def build_run(root: Path, run_dir: Path) -> dict | None:
    """Build a run dict with prompt, outputs, and grading data."""
    prompt = ""
    eval_id = None

    for candidate in [run_dir / "eval_metadata.json", run_dir.parent / "eval_metadata.json"]:
        if candidate.exists():
            try:
                metadata = json.loads(candidate.read_text())
                prompt = metadata.get("prompt", "")
                eval_id = metadata.get("eval_id")
            except (json.JSONDecodeError, OSError):
                pass
            if prompt:
                break

    if not prompt:
        for candidate in [run_dir / "transcript.md", run_dir / "outputs" / "transcript.md"]:
            if candidate.exists():
                try:
                    text = candidate.read_text()
                    match = re.search(r"## Eval Prompt\\n\\n([\\s\\S]*?)(?=\\n##|$)", text)
                    if match:
                        prompt = match.group(1).strip()
                except OSError:
                    pass
                if prompt:
                    break

    if not prompt:
        prompt = "(No prompt found)"

    run_id = str(run_dir.relative_to(root)).replace("/", "-").replace("\\\\", "-")

    outputs_dir = run_dir / "outputs"
    output_files: list[dict] = []
    if outputs_dir.is_dir():
        for f in sorted(outputs_dir.iterdir()):
            if f.is_file() and f.name not in METADATA_FILES:
                output_files.append(embed_file(f))

    grading = None
    for candidate in [run_dir / "grading.json", run_dir.parent / "grading.json"]:
        if candidate.exists():
            try:
                grading = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError):
                pass
            if grading:
                break

    return {
        "id": run_id,
        "prompt": prompt,
        "eval_id": eval_id,
        "outputs": output_files,
        "grading": grading,
    }


def embed_file(path: Path) -> dict:
    """Read a file and return an embedded representation."""
    ext = path.suffix.lower()
    mime = get_mime_type(path)

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(errors="replace")
        except OSError:
            content = "(Error reading file)"
        return {"name": path.name, "type": "text", "content": content}
    elif ext in IMAGE_EXTENSIONS:
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {"name": path.name, "type": "image", "mime": mime, "data_uri": f"data:{mime};base64,{b64}"}
    else:
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {"name": path.name, "type": "binary", "mime": mime, "data_uri": f"data:{mime};base64,{b64}"}


def generate_html(
    runs: list[dict],
    skill_name: str,
    previous: dict[str, dict] | None = None,
    benchmark: dict | None = None,
) -> str:
    """Generate standalone HTML page with embedded data."""
    previous_feedback: dict[str, str] = {}
    previous_outputs: dict[str, list[dict]] = {}
    if previous:
        for run_id, data in previous.items():
            if data.get("feedback"):
                previous_feedback[run_id] = data["feedback"]
            if data.get("outputs"):
                previous_outputs[run_id] = data["outputs"]

    embedded = {
        "skill_name": skill_name,
        "runs": runs,
        "previous_feedback": previous_feedback,
        "previous_outputs": previous_outputs,
    }
    if benchmark:
        embedded["benchmark"] = benchmark

    data_json = json.dumps(embedded)

    # Minimal viewer template (the full viewer.html would be loaded from assets in production)
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Eval Review - {skill_name}</title>
    <style>
        body {{ font-family: sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .run {{ border: 1px solid #ddd; margin: 20px 0; padding: 20px; border-radius: 8px; }}
        .prompt {{ background: #f5f5f5; padding: 10px; border-radius: 4px; margin-bottom: 10px; }}
        .output-file {{ margin: 10px 0; }}
        pre {{ background: #f5f5f5; padding: 10px; overflow-x: auto; border-radius: 4px; }}
        .grading {{ margin-top: 10px; }}
        .pass {{ color: green; }}
        .fail {{ color: red; }}
        textarea {{ width: 100%; height: 80px; margin-top: 10px; }}
        button {{ padding: 10px 20px; background: #141413; color: white; border: none; border-radius: 4px; cursor: pointer; margin-top: 20px; }}
    </style>
</head>
<body>
    <h1>Eval Review: {skill_name}</h1>
    <div id="app"></div>
    <button onclick="submitFeedback()">Submit All Reviews</button>
    <script>
    const DATA = {data_json};
    const feedbackMap = {{}};

    function renderRuns() {{
        const app = document.getElementById('app');
        let html = '';
        for (const run of DATA.runs) {{
            html += '<div class="run">';
            html += '<h3>' + run.id + '</h3>';
            html += '<div class="prompt"><strong>Prompt:</strong> ' + escapeHtml(run.prompt) + '</div>';
            for (const f of run.outputs) {{
                html += '<div class="output-file"><strong>' + escapeHtml(f.name) + '</strong>';
                if (f.type === 'text') html += '<pre>' + escapeHtml(f.content) + '</pre>';
                else if (f.type === 'image') html += '<img src="' + f.data_uri + '" style="max-width:100%">';
                html += '</div>';
            }}
            if (run.grading) {{
                html += '<div class="grading"><h4>Grading</h4>';
                for (const exp of (run.grading.expectations || [])) {{
                    const cls = exp.passed ? 'pass' : 'fail';
                    html += '<p class="' + cls + '">' + (exp.passed ? '\\u2713' : '\\u2717') + ' ' + escapeHtml(exp.text) + '</p>';
                }}
                html += '</div>';
            }}
            const prev = DATA.previous_feedback[run.id] || '';
            if (prev) html += '<p><em>Previous feedback: ' + escapeHtml(prev) + '</em></p>';
            html += '<textarea placeholder="Your feedback..." onchange="feedbackMap[\\'' + run.id + '\\'] = this.value"></textarea>';
            html += '</div>';
        }}
        app.innerHTML = html;
    }}

    function escapeHtml(s) {{
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }}

    function submitFeedback() {{
        const reviews = DATA.runs.map(r => ({{
            run_id: r.id,
            feedback: feedbackMap[r.id] || '',
            timestamp: new Date().toISOString()
        }}));
        const blob = new Blob([JSON.stringify({{reviews, status: 'complete'}}, null, 2)], {{type: 'application/json'}});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'feedback.json';
        a.click();
    }}

    renderRuns();
    </script>
</body>
</html>"""


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split("\\n"):
            if pid_str.strip():
                try:
                    os.kill(int(pid_str.strip()), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
        if result.stdout.strip():
            time.sleep(0.5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review HTML and handles feedback saves."""

    def __init__(self, workspace, skill_name, feedback_path, previous, benchmark_path, *args, **kwargs):
        self.workspace = workspace
        self.skill_name = skill_name
        self.feedback_path = feedback_path
        self.previous = previous
        self.benchmark_path = benchmark_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            runs = find_runs(self.workspace)
            benchmark = None
            if self.benchmark_path and self.benchmark_path.exists():
                try:
                    benchmark = json.loads(self.benchmark_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            html = generate_html(runs, self.skill_name, self.previous, benchmark)
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/feedback":
            data = self.feedback_path.read_bytes() if self.feedback_path.exists() else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/feedback":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                self.feedback_path.write_text(json.dumps(data, indent=2) + "\\n")
                resp = b'{"ok":true}'
                self.send_response(200)
            except (json.JSONDecodeError, OSError) as e:
                resp = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and serve eval review")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--port", "-p", type=int, default=3117)
    parser.add_argument("--skill-name", "-n", type=str, default=None)
    parser.add_argument("--previous-workspace", type=Path, default=None)
    parser.add_argument("--benchmark", type=Path, default=None)
    parser.add_argument("--static", "-s", type=Path, default=None)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"Error: {workspace} is not a directory", file=sys.stderr)
        sys.exit(1)

    runs = find_runs(workspace)
    if not runs:
        print(f"No runs found in {workspace}", file=sys.stderr)
        sys.exit(1)

    skill_name = args.skill_name or workspace.name.replace("-workspace", "")
    feedback_path = workspace / "feedback.json"

    previous: dict[str, dict] = {}
    benchmark_path = args.benchmark.resolve() if args.benchmark else None
    benchmark = None
    if benchmark_path and benchmark_path.exists():
        try:
            benchmark = json.loads(benchmark_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if args.static:
        html = generate_html(runs, skill_name, previous, benchmark)
        args.static.parent.mkdir(parents=True, exist_ok=True)
        args.static.write_text(html)
        print(f"Static viewer written to: {args.static}")
        sys.exit(0)

    port = args.port
    _kill_port(port)
    handler = partial(ReviewHandler, workspace, skill_name, feedback_path, previous, benchmark_path)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]

    url = f"http://localhost:{port}"
    print(f"\\n  Eval Viewer: {url}\\n  Workspace: {workspace}\\n")
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
'''


def _get_asset_eval_review_html() -> str:
    """Return the eval review HTML template asset."""
    return '''\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Eval Set Review - __SKILL_NAME_PLACEHOLDER__</title>
    <style>
        body { font-family: sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #faf9f5; }
        h1 { color: #141413; }
        .description { background: white; padding: 15px; border-radius: 6px; margin-bottom: 20px; border: 1px solid #e8e6dc; }
        table { width: 100%; border-collapse: collapse; background: white; border: 1px solid #e8e6dc; }
        th, td { padding: 10px; text-align: left; border: 1px solid #e8e6dc; }
        th { background: #141413; color: white; }
        .trigger-yes { color: #788c5d; font-weight: bold; }
        .trigger-no { color: #c44; font-weight: bold; }
        input[type="text"] { width: 100%; padding: 5px; box-sizing: border-box; }
        button { padding: 8px 16px; margin: 5px; cursor: pointer; border-radius: 4px; border: 1px solid #ddd; }
        .btn-primary { background: #141413; color: white; border: none; }
        .btn-danger { background: #c44; color: white; border: none; }
        .btn-add { background: #788c5d; color: white; border: none; }
    </style>
</head>
<body>
    <h1>Eval Set Review: __SKILL_NAME_PLACEHOLDER__</h1>
    <div class="description">
        <strong>Current Description:</strong> __SKILL_DESCRIPTION_PLACEHOLDER__
    </div>
    <table id="eval-table">
        <thead>
            <tr>
                <th style="width:50px">#</th>
                <th>Query</th>
                <th style="width:120px">Should Trigger</th>
                <th style="width:80px">Actions</th>
            </tr>
        </thead>
        <tbody id="eval-body"></tbody>
    </table>
    <div style="margin-top: 15px;">
        <button class="btn-add" onclick="addRow()">+ Add Query</button>
        <button class="btn-primary" onclick="exportEvalSet()">Export Eval Set</button>
    </div>
    <script>
    let evalData = __EVAL_DATA_PLACEHOLDER__;

    function render() {
        const tbody = document.getElementById('eval-body');
        tbody.innerHTML = '';
        evalData.forEach((item, i) => {
            const tr = document.createElement('tr');
            const cls = item.should_trigger ? 'trigger-yes' : 'trigger-no';
            tr.innerHTML = `
                <td>${i + 1}</td>
                <td><input type="text" value="${escapeAttr(item.query)}" onchange="evalData[${i}].query = this.value"></td>
                <td class="${cls}" style="cursor:pointer" onclick="toggleTrigger(${i})">
                    ${item.should_trigger ? 'YES' : 'NO'}
                </td>
                <td><button class="btn-danger" onclick="removeRow(${i})">Remove</button></td>
            `;
            tbody.appendChild(tr);
        });
    }

    function toggleTrigger(i) {
        evalData[i].should_trigger = !evalData[i].should_trigger;
        render();
    }

    function addRow() {
        evalData.push({query: '', should_trigger: true});
        render();
    }

    function removeRow(i) {
        evalData.splice(i, 1);
        render();
    }

    function exportEvalSet() {
        const filtered = evalData.filter(e => e.query.trim());
        const blob = new Blob([JSON.stringify(filtered, null, 2)], {type: 'application/json'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'eval_set.json';
        a.click();
    }

    function escapeAttr(s) {
        return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    render();
    </script>
</body>
</html>
'''


# ======================================================================
# Helpers
# ======================================================================

def _bind_skill_to_group(
    db: Session,
    skill: AgentSkill,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """Bind an AgentSkill to the Default Resource Group (idempotent)."""
    default_group = db.query(ResourceGroup).filter(
        ResourceGroup.id == default_group_id
    ).first()

    if default_group and skill not in default_group.agent_skills:
        default_group.agent_skills.append(skill)
        db.flush()

        logger.info("Bound skill to Default resource group", context={
            "skill_id": skill.id, "group_id": default_group_id,
        })
    elif default_group:
        pass
