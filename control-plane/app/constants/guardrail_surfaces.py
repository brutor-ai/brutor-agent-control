# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The surfaces a guardrail check can be put on.

Each check of a guardrail configuration has a list of surfaces. The gateway
asks that list for the name of the surface a call is on; these are the
names it asks for, and the only ones a list may hold: any other entry would
be stored and never matched. A check with no list (SQL NULL) runs on the
gateway's default set for that check.

Every validator, default, seed and template of surface names uses this
tuple; `tests/test_guardrail_surfaces_are_the_gateways.py` holds it to the
gateway's source.
"""

GUARDRAIL_CHECK_SURFACES = (
    # One per LLM route and direction.
    "chat_input", "chat_output",
    "embeddings_input",
    "image_gen_input", "image_gen_output",
    "audio_tts_input", "audio_tts_output",
    "audio_stt_input", "audio_stt_output",
    "video_gen_input", "video_gen_output",
    "batch_input", "batch_output",
    "moderation_input",
    # MCP tool calls, skill executions, agent-to-agent calls.
    "mcp_input", "mcp_output",
    "skill_input", "skill_output",
    "a2a_inbound", "a2a_outbound",
)


def unknown_surfaces_message(unknown) -> str:
    """The refusal of a surface list that names something else."""
    return (
        f"Unknown surfaces: {', '.join(repr(s) for s in unknown)}. "
        f"A check runs only on: {', '.join(GUARDRAIL_CHECK_SURFACES)}."
    )
