"""dbt tools -- loaded only when a dbt project is configured.

These shell out to the dbt CLI rather than importing ``dbt-core``: dbt's Python
API is not stable across minor versions, and the CLI is what the user's CI runs
anyway. Commands are assembled as argument lists, never as shell strings, so a
model-supplied selector cannot smuggle in a second command.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agentkart.core.errors import ToolError
from agentkart.core.tools import tool

if TYPE_CHECKING:
    from .. import WarehouseContext

#: dbt subcommands this package is willing to run.
_ALLOWED = {"compile", "run", "test", "build", "seed", "snapshot", "ls", "parse", "docs"}

#: dbt subcommands that write to the warehouse.
_MUTATING = {"run", "build", "seed", "snapshot"}

#: A conservative selector grammar: model names, paths, tags, graph operators.
_SELECTOR = re.compile(r"^[A-Za-z0-9_.,:+@*/ ^-]{0,500}$")


def _dbt_binary() -> str:
    found = shutil.which("dbt")
    if not found:
        raise ToolError(
            "the dbt CLI is not on PATH. Install it with: pip install \"agent[dbt]\""
        )
    return found


def _run_dbt(project_dir: Path, args: list[str], timeout: int = 900) -> tuple[int, str]:
    command = [_dbt_binary(), *args, "--project-dir", str(project_dir)]
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_dir),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"dbt timed out after {timeout}s: {' '.join(args)}") from exc
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output[-20_000:]


@tool(
    name="dbt_list",
    description=(
        "List the dbt resources matching a selector, without running anything. Use this "
        "to establish what a selector actually covers before running or building it."
    ),
    schema={
        "type": "object",
        "properties": {
            "select": {
                "type": "string",
                "description": "A dbt selector, e.g. 'stg_orders+' or 'tag:nightly'.",
            }
        },
        "required": ["select"],
    },
    requires=["dbt"],
)
def dbt_list(context: WarehouseContext, select: str = "") -> str:
    _check_selector(select)
    args = ["ls", "--resource-type", "all"]
    if select:
        args += ["--select", select]
    code, output = _run_dbt(context.dbt_dir, args)
    if code != 0:
        raise ToolError(f"dbt ls failed:\n{output}")
    return f"```\n{output.strip()}\n```"


@tool(
    name="dbt_compile",
    description=(
        "Compile dbt models to their final SQL without executing them. This is the safe "
        "way to inspect what a model will actually do, including how its Jinja and "
        "incremental logic resolve."
    ),
    schema={
        "type": "object",
        "properties": {
            "select": {"type": "string", "description": "Selector for models to compile."}
        },
        "required": ["select"],
    },
    requires=["dbt"],
)
def dbt_compile(context: WarehouseContext, select: str = "") -> str:
    _check_selector(select)
    args = ["compile"]
    if select:
        args += ["--select", select]
    code, output = _run_dbt(context.dbt_dir, args)
    status = "succeeded" if code == 0 else f"failed (exit {code})"
    return f"dbt compile {status}\n\n```\n{output.strip()}\n```"


@tool(
    name="dbt_show_compiled",
    description=(
        "Return the compiled SQL for one model from the dbt target directory. Run "
        "dbt_compile first. Prefer this over reading the raw .sql file when you need to "
        "reason about what will really execute."
    ),
    schema={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Model name, e.g. 'fct_orders'."}
        },
        "required": ["model"],
    },
    requires=["dbt"],
)
def dbt_show_compiled(context: WarehouseContext, model: str) -> str:
    if not re.match(r"^[A-Za-z0-9_]+$", model):
        raise ToolError(f"{model!r} is not a valid model name")
    target = context.dbt_dir / "target" / "compiled"
    if not target.is_dir():
        raise ToolError("no target/compiled directory. Run dbt_compile first.")
    matches = sorted(target.rglob(f"{model}.sql"))
    if not matches:
        raise ToolError(f"no compiled SQL for {model!r}. Check the model name and recompile.")
    return f"```sql\n{matches[0].read_text(encoding='utf-8')}\n```"


@tool(
    name="dbt_test",
    description=(
        "Run dbt tests for the selected models and report failures. Tests are read-only "
        "against your data, so this is safe to run freely -- do it after any model change."
    ),
    schema={
        "type": "object",
        "properties": {"select": {"type": "string", "description": "Selector for tests to run."}},
        "required": ["select"],
    },
    requires=["dbt"],
)
def dbt_test(context: WarehouseContext, select: str = "") -> str:
    _check_selector(select)
    args = ["test"]
    if select:
        args += ["--select", select]
    code, output = _run_dbt(context.dbt_dir, args)
    status = "all tests passed" if code == 0 else f"tests failed (exit {code})"
    return f"{status}\n\n```\n{output.strip()}\n```"


@tool(
    name="dbt_run",
    description=(
        "Materialise dbt models in the warehouse. This WRITES. It requires a "
        "write-enabled session and operator confirmation. Always run dbt_compile and "
        "dbt_list first so you know exactly what will be rebuilt."
    ),
    schema={
        "type": "object",
        "properties": {
            "select": {"type": "string", "description": "Selector for models to run."},
            "full_refresh": {
                "type": "boolean",
                "description": "Rebuild incremental models from scratch. Expensive; be sure.",
            },
        },
        "required": ["select", "full_refresh"],
    },
    destructive=True,
    requires=["dbt"],
)
def dbt_run(context: WarehouseContext, select: str = "", full_refresh: bool = False) -> str:
    _check_selector(select)
    if not context.policy.write:
        raise ToolError(
            "refused: this session is read-only. Construct the agent with write=True to run dbt."
        )
    approved = context.confirm(
        action="dbt run" + (" --full-refresh" if full_refresh else ""),
        detail=f"selector: {select or '(all models)'}",
        purpose="materialise dbt models",
    )
    if not approved:
        raise ToolError("refused: the operator declined the dbt run.")

    args = ["run"]
    if select:
        args += ["--select", select]
    if full_refresh:
        args.append("--full-refresh")
    code, output = _run_dbt(context.dbt_dir, args)
    context.record(f"dbt run {select}", "write", "dbt materialisation", kind="dbt")
    status = "succeeded" if code == 0 else f"failed (exit {code})"
    return f"dbt run {status}\n\n```\n{output.strip()}\n```"


@tool(
    name="dbt_lineage",
    description=(
        "Return the upstream and downstream dependencies of a model from the dbt "
        "manifest. Use this before changing or dropping anything, to see what breaks."
    ),
    schema={
        "type": "object",
        "properties": {"model": {"type": "string", "description": "Model name to trace."}},
        "required": ["model"],
    },
    requires=["dbt"],
)
def dbt_lineage(context: WarehouseContext, model: str) -> str:
    manifest_path = context.dbt_dir / "target" / "manifest.json"
    if not manifest_path.is_file():
        raise ToolError("no target/manifest.json. Run dbt_compile first to generate it.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"could not read the dbt manifest: {exc}") from exc

    nodes = manifest.get("nodes", {})
    node_id = next((k for k, v in nodes.items() if v.get("name") == model), None)
    if node_id is None:
        raise ToolError(f"{model!r} is not in the manifest")

    upstream = nodes[node_id].get("depends_on", {}).get("nodes", [])
    downstream = [
        k for k, v in nodes.items() if node_id in v.get("depends_on", {}).get("nodes", [])
    ]

    def names(ids: list[str]) -> str:
        listed = sorted(nodes.get(i, {}).get("name", i) for i in ids)
        return "\n".join(f"- {n}" for n in listed) or "- (none)"

    return (
        f"### Lineage for `{model}`\n\n"
        f"**Upstream (this model reads from):**\n{names(upstream)}\n\n"
        f"**Downstream (these break if you change it):**\n{names(downstream)}"
    )


def _check_selector(select: str) -> None:
    if select and not _SELECTOR.match(select):
        raise ToolError(
            f"selector {select!r} contains characters that are not allowed. "
            "Use model names, paths, tags and dbt graph operators only."
        )
