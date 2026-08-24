"""Running things: tests, scripts, linters, type checkers.

Every command is assembled as an **argument list** and run with ``shell=False``.
There is no code path where a model-supplied string reaches a shell, so shell
metacharacters are inert rather than filtered. The executable must also be on the
policy's allowlist; anything else is classified destructive and refused.

This is the half of the domain that lets the agent check its own work. A model
that can run the tests stops guessing whether the code is right.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from agentsuite.core.errors import ToolError
from agentsuite.core.tools import tool

if TYPE_CHECKING:
    from .. import WorkspaceContext

MAX_OUTPUT = 20_000


def run_argv(context: WorkspaceContext, argv: list[str], *, purpose: str = "") -> str:
    """Check ``argv`` against the policy, then run it inside the workspace."""
    policy = context.policy
    verdict = policy.check(" ".join(argv), kind="command", argv=argv)

    if not verdict.allowed and not verdict.needs_confirmation:
        raise ToolError(verdict.reason)
    if verdict.needs_confirmation:
        label = verdict.primary.label if verdict.primary else "COMMAND"
        if not context.confirm(action=label, detail=" ".join(argv), purpose=purpose):
            raise ToolError("refused: the operator declined this command.")

    executable = shutil.which(argv[0])
    if executable is None:
        raise ToolError(f"{argv[0]!r} is not installed or not on PATH")

    # A deliberately minimal environment: no inherited credentials, no proxy
    # settings the agent could be talked into using for exfiltration.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "PATHEXT"}
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            [executable, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=getattr(policy, "timeout", 900),
            cwd=str(context.workspace.root),
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{argv[0]} timed out after {getattr(policy, 'timeout', 900)}s") from exc
    except OSError as exc:
        raise ToolError(f"could not run {argv[0]}: {exc}") from exc

    context.record(" ".join(argv), verdict.tier, purpose, kind="command")

    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + f"\n... [{len(output) - MAX_OUTPUT:,} more characters]"
    status = "exit 0" if completed.returncode == 0 else f"exit {completed.returncode}"
    return f"$ {' '.join(argv)}\n[{status}]\n\n{output or '(no output)'}"


@tool(
    name="run_tests",
    description=(
        "Run the project's tests with pytest and report the result. Run this after "
        "every change you make -- claiming code works without running the tests is "
        "not acceptable. Narrow with a path or -k expression while iterating."
    ),
    schema={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Test path or node id, e.g. 'tests/test_api.py::test_login'. "
                    "Empty for all."
                ),
            },
            "keyword": {
                "type": "string",
                "description": "pytest -k expression to select tests by name. Empty for none.",
            },
        },
        "required": ["target", "keyword"],
    },
    requires=["workspace"],
)
def run_tests(context: WorkspaceContext, target: str = "", keyword: str = "") -> str:
    argv = ["pytest", "-q", "--no-header"]
    if target:
        argv.append(context.safe_relative(target))
    if keyword:
        argv += ["-k", keyword]
    return run_argv(context, argv, purpose="run tests")


@tool(
    name="run_coverage",
    description=(
        "Run the tests under coverage and report which lines are not covered. "
        "Use this to find untested branches before writing tests, and to check "
        "your new tests actually reach the code you meant. Coverage is a signal, "
        "not a target -- report the uncovered lines, not just the percentage."
    ),
    schema={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Package or directory to measure, e.g. 'src'. Empty for the project."
                ),
            },
            "target": {
                "type": "string",
                "description": "Tests to run. Empty for all of them.",
            },
        },
        "required": ["source", "target"],
    },
    requires=["workspace", "testing"],
)
def run_coverage(context: WorkspaceContext, source: str = "", target: str = "") -> str:
    if shutil.which("pytest") is None:
        return "pytest is not installed, so coverage was not measured."
    argv = [
        "pytest",
        "-q",
        "--no-header",
        f"--cov={context.safe_relative(source) if source else '.'}",
        "--cov-report=term-missing:skip-covered",
    ]
    if target:
        argv.append(context.safe_relative(target))
    output = run_argv(context, argv, purpose="measure test coverage")
    if "unrecognized arguments" in output or "no such option" in output:
        return (
            "pytest-cov is not installed, so coverage was not measured. "
            'Install it with: pip install pytest-cov. Do not report a coverage figure.'
        )
    return output


@tool(
    name="run_python",
    description=(
        "Run a Python file from the project and return its output. Use this to "
        "execute a training script, a Spark job, a data check, or a scratch script "
        "you wrote. The script runs with the project as its working directory."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Python file relative to the project root."},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments passed to the script.",
            },
            "purpose": {"type": "string", "description": "One sentence on why. Audited."},
        },
        "required": ["path", "args", "purpose"],
    },
    requires=["workspace"],
)
def run_python(
    context: WorkspaceContext, path: str, args: list[str] | None = None, purpose: str = ""
) -> str:
    relative = context.safe_relative(path, must_exist=True)
    argv = ["python", relative, *[str(a) for a in (args or [])]]
    return run_argv(context, argv, purpose=purpose or f"run {relative}")


@tool(
    name="python_environment",
    description=(
        "Report the Python version and the installed packages relevant to this "
        "project. Check this before assuming a library is available or guessing at "
        "an API that changed between versions."
    ),
    schema={"type": "object", "properties": {}, "required": []},
    requires=["workspace"],
)
def python_environment(context: WorkspaceContext) -> str:
    script = (
        "import sys, importlib.metadata as meta\n"
        "print('python', sys.version.split()[0])\n"
        "names = ['pyspark','pandas','polars','numpy','scikit-learn','torch',"
        "'lightning','transformers','datasets','xgboost','lightgbm','langchain',"
        "'llama-index','chromadb','faiss-cpu','sentence-transformers','pytest',"
        "'ruff','mypy','terraform-compliance']\n"
        "for name in names:\n"
        "    try:\n"
        "        print(name, meta.version(name))\n"
        "    except Exception:\n"
        "        pass\n"
    )
    return run_argv(context, ["python", "-c", script], purpose="inspect the environment")
