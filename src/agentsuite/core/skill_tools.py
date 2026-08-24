"""Tools that implement progressive disclosure over the skill library.

The system prompt carries only names and descriptions. When the model decides a
skill applies, it calls ``load_skill`` and the body arrives as a tool result.
That keeps a large skill library affordable and, usefully, leaves an audit trail
of which guidance actually influenced a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import SkillError, ToolError
from .tools import tool

if TYPE_CHECKING:
    from .loop import AgentContext


@tool(
    name="list_skills",
    description=(
        "List every skill available in this session with its description and source. "
        "Use this when you are unsure whether guidance exists for the task at hand; "
        "the system prompt already contains the same index, so prefer that first."
    ),
    schema={"type": "object", "properties": {}, "required": []},
    trusted_output=True,
)
def list_skills(context: AgentContext) -> str:
    skills = context.skills
    if not skills:
        return "No skills are loaded."
    lines = ["| skill | source | description |", "| --- | --- | --- |"]
    for skill in skills.values():
        description = skill.description.replace("|", "\\|")
        lines.append(f"| {skill.name} | {skill.source} | {description} |")
    return "\n".join(lines)


@tool(
    name="load_skill",
    description=(
        "Load the full text of a named skill. Call this as soon as you judge a skill "
        "relevant, before planning the work -- skills carry the house rules and the "
        "failure modes that generic SQL knowledge does not. Returns the skill body "
        "and a list of any supporting reference files it bundles."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The exact skill name as shown in the skill index.",
            }
        },
        "required": ["name"],
    },
    trusted_output=True,
)
def load_skill(context: AgentContext, name: str) -> str:
    skill = context.skills.get(name)
    if skill is None:
        known = ", ".join(sorted(context.skills)) or "(none)"
        raise ToolError(f"no skill named {name!r}. Available skills: {known}")

    context.record_skill_use(name)

    parts = [f"# Skill: {skill.name}", f"_source: {skill.source} ({skill.origin})_", "", skill.body]
    files = skill.list_files()
    if files:
        listing = "\n".join(f"- {f}" for f in files)
        parts += [
            "",
            "## Supporting files",
            "Read one with `read_skill_file`:",
            "",
            listing,
        ]
    return "\n".join(parts)


@tool(
    name="read_skill_file",
    description=(
        "Read a supporting file bundled inside a skill directory, such as a reference "
        "document or an example script. Paths are relative to the skill directory and "
        "cannot escape it."
    ),
    schema={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "The skill that owns the file."},
            "path": {
                "type": "string",
                "description": "Relative path as listed by load_skill, e.g. 'reference/scd2.md'.",
            },
        },
        "required": ["skill", "path"],
    },
    trusted_output=True,
)
def read_skill_file(context: AgentContext, skill: str, path: str) -> str:
    found = context.skills.get(skill)
    if found is None:
        raise ToolError(f"no skill named {skill!r}")
    try:
        return found.read_file(path)
    except SkillError as exc:
        raise ToolError(str(exc)) from exc
