"""Terraform tools -- loaded only when the project contains ``.tf`` files.

Terraform is the sharpest risk surface in this library: a wrong `apply` deletes
production, and unlike a bad query there is no rollback. So:

* ``validate``, ``fmt`` and ``plan`` are read-tier and freely available;
* ``apply`` and ``destroy`` are classified destructive by the policy layer and
  refused outright unless the session is write-enabled **and** the operator
  confirms;
* ``-auto-approve`` is refused unconditionally -- it exists to skip exactly the
  gate that makes this safe;
* state files are in the workspace deny list, so no tool can read them. They
  contain plaintext secrets.

Read the plan. Never trust a summary of a plan.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentsuite.core.errors import ToolError
from agentsuite.core.tools import tool

from .execute import run_argv

if TYPE_CHECKING:
    from .. import WorkspaceContext

#: Terraform resource addresses, for -target. Conservative on purpose.
_ADDRESS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]\"-]{0,200}$")


@tool(
    name="terraform_validate",
    description=(
        "Check the Terraform configuration for syntax and internal consistency. "
        "Does not contact any provider and changes nothing. Run this first."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Module directory. Empty for the root."}
        },
        "required": ["path"],
    },
    requires=["terraform"],
)
def terraform_validate(context: WorkspaceContext, path: str = "") -> str:
    argv = ["terraform"]
    if path:
        argv.append(f"-chdir={context.safe_relative(path)}")
    argv.append("validate")
    return run_argv(context, argv, purpose="validate terraform configuration")


@tool(
    name="terraform_fmt_check",
    description=(
        "Report Terraform files whose formatting differs from canonical style. "
        "Reports only; it does not rewrite anything."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to check. Empty for the root."}
        },
        "required": ["path"],
    },
    requires=["terraform"],
)
def terraform_fmt_check(context: WorkspaceContext, path: str = "") -> str:
    argv = ["terraform", "fmt", "-check", "-diff", "-recursive"]
    if path:
        argv.append(context.safe_relative(path))
    return run_argv(context, argv, purpose="check terraform formatting")


@tool(
    name="terraform_plan",
    description=(
        "Show what Terraform WOULD change, without changing anything. This is the "
        "single most important tool here: read the full plan and report the counts "
        "of resources to add, change and destroy. Never summarise a plan you have "
        "not read, and never describe a destroy as a change."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Module directory. Empty for the root."},
            "target": {
                "type": "string",
                "description": "Optional single resource address to scope the plan to.",
            },
        },
        "required": ["path", "target"],
    },
    requires=["terraform"],
)
def terraform_plan(context: WorkspaceContext, path: str = "", target: str = "") -> str:
    argv = ["terraform"]
    if path:
        argv.append(f"-chdir={context.safe_relative(path)}")
    argv += ["plan", "-no-color", "-input=false", "-lock=false"]
    if target:
        if not _ADDRESS.match(target):
            raise ToolError(f"{target!r} is not a valid Terraform resource address")
        argv.append(f"-target={target}")

    output = run_argv(context, argv, purpose="plan terraform changes")
    return output + "\n\n" + _plan_warning(output)


def _plan_warning(output: str) -> str:
    """Make destruction impossible to skim past."""
    match = re.search(
        r"Plan:\s*(\d+)\s*to add,\s*(\d+)\s*to change,\s*(\d+)\s*to destroy", output
    )
    if not match:
        return "_No plan summary line found. Report that, rather than assuming no changes._"
    add, change, destroy = (int(g) for g in match.groups())
    if destroy:
        return (
            f"**This plan DESTROYS {destroy} resource(s)** (and adds {add}, changes {change}). "
            "List every resource being destroyed by name in your answer, and say plainly "
            "whether the destruction looks intended."
        )
    return f"_Plan: {add} to add, {change} to change, none destroyed._"


@tool(
    name="terraform_show_plan_json",
    description=(
        "Produce a machine-readable plan and summarise the resource changes by type "
        "and action. Useful when the text plan is too long to read in full. Still "
        "read the text plan for anything being destroyed."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Module directory. Empty for the root."}
        },
        "required": ["path"],
    },
    requires=["terraform"],
)
def terraform_show_plan_json(context: WorkspaceContext, path: str = "") -> str:
    argv = ["terraform"]
    if path:
        argv.append(f"-chdir={context.safe_relative(path)}")
    argv += ["show", "-json"]
    return run_argv(context, argv, purpose="read the terraform plan as json")


@tool(
    name="terraform_apply",
    description=(
        "Apply Terraform changes to REAL INFRASTRUCTURE. Irreversible. Requires a "
        "write-enabled session and explicit operator confirmation, and will be "
        "refused otherwise. Always run terraform_plan first and report exactly what "
        "the plan destroys before proposing this."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Module directory. Empty for the root."},
            "purpose": {
                "type": "string",
                "description": "Why this must be applied now. Shown to the operator and audited.",
            },
        },
        "required": ["path", "purpose"],
    },
    destructive=True,
    requires=["terraform", "write"],
)
def terraform_apply(context: WorkspaceContext, path: str = "", purpose: str = "") -> str:
    argv = ["terraform"]
    if path:
        argv.append(f"-chdir={context.safe_relative(path)}")
    # -auto-approve is deliberately absent: Terraform's own prompt is a second
    # gate, and the policy layer refuses the flag outright if it ever appears.
    argv += ["apply", "-no-color", "-input=false"]
    if not purpose:
        raise ToolError("state why this apply is needed; it is recorded in the audit log")
    return run_argv(context, argv, purpose=purpose)
