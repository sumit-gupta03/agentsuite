"""The code domain: reading, writing and verifying source in a project.

One domain covers Python, PySpark, ML, data science, PyTorch, RAG and Terraform,
because they need the same *tools* -- read a file, write a file, run a script,
run the tests, lint, type-check. What differs between them is knowledge of what
good code looks like in that stack, and that lives in skills.

So the stacks are **presets**, not domains::

    agent.pyspark(project="./etl")      # code domain + PySpark skills
    agent.pytorch(project="./model")    # code domain + PyTorch skills
    agent.terraform(project="./infra")  # code domain + Terraform tools and skills

Adding another stack is a skill file and a dict entry. That is the whole point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentkart.core.config import Config
from agentkart.core.domain import Domain, resolve_preset
from agentkart.core.errors import ToolError
from agentkart.core.loop import Agent, AgentContext
from agentkart.core.policy import Policy

from .errors import WorkspaceError
from .policy import DEFAULT_COMMANDS, WorkspacePolicy
from .workspace import Workspace, open_workspace

#: Stacks a preset can select. Skills gate on these through ``requires:``.
STACKS = ("python", "pyspark", "ml", "pytorch", "rag", "terraform", "testing")

SYSTEM_PROMPT = """\
## Writing code

You are writing code other engineers will maintain and depend on. Clever is not
the goal; correct, readable and verified is.

1. **Read before you write.** `list_files` and `grep` to find things, `read_file`
   before editing. Match the conventions already in the project rather than
   importing your own. Editing a file you have not read is refused.
2. **Prefer `edit_file` to `write_file`** for existing code -- it cannot discard
   the rest of the file by accident.
3. **Verify what you changed.** Run `run_tests`, then `lint`, then `typecheck`.
   Report what they actually said.
4. **Check the environment** with `python_environment` before assuming a library
   is installed or an API exists at the version in use.

## What you must not claim

Never say tests pass, code lints, or a type check is clean unless you ran the
tool and saw it in the result. If a tool is not installed, say so and say the
check was skipped -- do not treat absence of a failure as a pass.

## What good looks like

- Handle the error path. A function that only works on the happy path is not done.
- Name things for what they are. Comments explain *why*, never *what*.
- New behaviour comes with a test that fails without the change.
- Type hints on public functions; no bare `except:`; no mutable default arguments.
- Small, single-purpose functions over long ones with sections.
- Do not reformat, rename or "tidy" code the task did not ask you to touch.\
"""


class WorkspaceContext(AgentContext):
    """Agent context with the workspace narrowed, plus read-tracking.

    The read set is what makes "you must read a file before replacing it"
    enforceable rather than advisory.
    """

    _read_paths: set[str]

    def __post_init__(self) -> None:  # dataclass hook on the parent
        self._read_paths = set()

    @property
    def workspace(self) -> Workspace:
        """The project, or a tool error explaining that there isn't one."""
        if not isinstance(self.connection, Workspace):
            raise ToolError(
                "this session has no project directory. Pass project=... to use code tools."
            )
        return self.connection

    def mark_read(self, path: str) -> None:
        self._read_paths.add(self._key(path))

    def has_read(self, path: str) -> bool:
        return self._key(path) in getattr(self, "_read_paths", set())

    def _key(self, path: str) -> str:
        try:
            return self.workspace.relative(self.workspace.resolve(path))
        except (WorkspaceError, ToolError):
            return path

    def safe_relative(self, path: str, *, must_exist: bool = False) -> str:
        """Validate a model-supplied path and return it relative to the root.

        Commands run with the workspace as their working directory, so passing a
        relative path keeps absolute paths -- and anything outside the project --
        out of the argument list entirely.
        """
        try:
            resolved = self.workspace.resolve(path, must_exist=must_exist)
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from exc
        return self.workspace.relative(resolved)


def _build_policy(config: Config) -> Policy:
    """Built entirely from config -- nothing here is fixed in the class.

    ``allow_commands`` extends the allowlist, ``deny_commands`` removes from it.
    Neither can be reached by a skill file or a prompt: they are the operator's
    settings, read once at construction.
    """
    commands = dict(DEFAULT_COMMANDS)
    for name, tier in (config.option("allow_commands") or {}).items():
        commands[str(name)] = str(tier)
    for name in config.option("deny_commands") or ():
        commands.pop(str(name), None)

    return WorkspacePolicy(
        write=config.write,
        allow_destructive=False,
        timeout=int(config.option("timeout", 900)),
        commands=commands,
    )


def _build_connection(config: Config) -> Workspace | None:
    """The workspace, with its limits and deny list taken from config.

    ``deny`` and ``ignore`` *extend* the defaults rather than replacing them, so
    a project cannot widen its own boundary by supplying a shorter list.
    """
    target = config.option("project")
    if not target:
        return None
    workspace = open_workspace(target)
    if workspace is None:  # pragma: no cover - open_workspace only returns None for None
        return None

    extra_deny = tuple(str(p) for p in (config.option("deny_patterns") or ()))
    extra_ignore = tuple(str(p) for p in (config.option("ignore_patterns") or ()))
    workspace.deny = (*workspace.deny, *extra_deny)
    workspace.ignore = (*workspace.ignore, *extra_ignore)
    workspace.max_read_bytes = int(config.option("max_read_bytes", workspace.max_read_bytes))
    return workspace


def _capabilities(config: Config, connection: Any) -> set[str]:
    caps: set[str] = set()
    if not isinstance(connection, Workspace):
        return caps
    caps.add("workspace")

    # An explicit stacks= wins; otherwise infer from the project's manifests, and
    # fall back to everything so a bare agent.code() is not artificially blinkered.
    declared = config.option("stacks")
    if isinstance(declared, str):
        declared = [s.strip() for s in declared.split(",") if s.strip()]
    stacks = set(declared) if declared else (connection.detect_stacks() or set(STACKS))

    caps |= {s for s in stacks if s in STACKS}
    caps.add("python")  # python-craft applies to every stack here
    return caps


DOMAIN = Domain(
    name="code",
    description="reading, writing and verifying source in a project",
    package=__name__,
    tool_modules=("tools.files", "tools.execute", "tools.quality", "tools.terraform"),
    policy_factory=_build_policy,
    connection_factory=_build_connection,
    capability_factory=_capabilities,
    aliases=("dev", "software"),
    presets={
        # Each preset pins one stack, so the skill index stays focused. Adding a
        # stack is a skill file plus one line here -- no new tools, no new domain.
        "python": {"stacks": ["python"]},
        "testing": {"stacks": ["testing", "python"]},
        "unittest": {"stacks": ["testing", "python"]},
        "pyspark": {"stacks": ["pyspark", "python"]},
        "bigdata": {"stacks": ["pyspark", "python"]},
        "datascience": {"stacks": ["ml", "python"]},
        "ml": {"stacks": ["ml", "python"]},
        "deeplearning": {"stacks": ["pytorch", "python"]},
        "pytorch": {"stacks": ["pytorch", "python"]},
        "rag": {"stacks": ["rag", "python"]},
        "terraform": {"stacks": ["terraform"]},
    },
    preset_descriptions={
        "python": "Write, refactor or debug general Python: modules, CLIs, services, packaging.",
        "testing": (
            "Write unit tests, improve a test suite, investigate a failing or flaky "
            "test, or check and improve coverage."
        ),
        "pyspark": (
            "Write or fix PySpark and Spark jobs: skew, shuffles, partitioning, "
            "joins, caching, slow or failing big-data pipelines."
        ),
        "bigdata": "Large-scale distributed data processing with Spark.",
        "datascience": (
            "Exploratory analysis, feature engineering, statistics, and moving "
            "notebook work into production modules."
        ),
        "ml": (
            "Build or review a machine learning training pipeline: scikit-learn, "
            "XGBoost, LightGBM, cross-validation, leakage, metric choice, evaluation."
        ),
        "deeplearning": "Train or debug neural networks: training loops, loss curves, GPUs.",
        "pytorch": (
            "Write or debug PyTorch: training loops, Dataset and DataLoader, "
            "mixed precision, CUDA out-of-memory, performance."
        ),
        "rag": (
            "Build or fix retrieval-augmented generation: chunking, embeddings, "
            "vector search, reranking, and evaluating retrieval quality."
        ),
        "terraform": (
            "Write, review or apply Terraform and infrastructure as code: plans, "
            "state, modules, providers."
        ),
    },
)


def create(
    project: str | Path | Workspace | None = None,
    *,
    preset: str | None = None,
    stacks: list[str] | str | None = None,
    timeout: int | None = None,
    **kwargs: Any,
) -> Agent:
    """Build a code agent.

    ::

        import agentkart as agent
        dev = agent.pyspark(project="./etl", write=True, confirm=my_handler)
        dev.run("The nightly job skews on customer_id. Find out why and fix it.")

    Args:
        project: The directory the agent may touch. Nothing outside it is
            reachable by any tool.
        preset: A stack preset -- see ``agent.list_presets()``.
        stacks: Explicit stack list, overriding both the preset and detection.
    """
    options = resolve_preset(
        DOMAIN,
        preset,
        {
            "project": (
                None if isinstance(project, Workspace) else (str(project) if project else None)
            ),
            "stacks": stacks,
            "timeout": timeout,
        },
    )
    options = {k: v for k, v in options.items() if v is not None}

    return Agent(
        domain=DOMAIN,
        connection=project if isinstance(project, Workspace) else None,
        context_class=WorkspaceContext,
        **options,
        **kwargs,
    )


__all__ = ["DOMAIN", "STACKS", "SYSTEM_PROMPT", "Workspace", "WorkspaceContext", "create"]
