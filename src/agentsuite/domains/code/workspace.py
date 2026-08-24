"""The workspace: a project directory the agent may look at, and nothing beyond it.

This is the code domain's connection object, the counterpart to a warehouse.
Its one job is turning a model-supplied path string into a real path that is
provably inside the project, or refusing.

Every path that reaches a tool goes through :meth:`Workspace.resolve`. There is
no second route.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import WorkspaceError

#: Never read, never write, never list. Credentials and history live here.
DEFAULT_DENY = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "service-account*.json",
    "*.tfstate",
    "*.tfstate.backup",
    "terraform.tfvars",
    "*.auto.tfvars",
    ".git/config",
)

#: Skipped when walking, for noise rather than secrecy.
DEFAULT_IGNORE = (
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".terraform",
    "dist",
    "build",
    ".ipynb_checkpoints",
    ".tox",
    ".eggs",
)

#: Files above this size are refused rather than dumped into the context window.
MAX_READ_BYTES = 400_000


@dataclass
class Workspace:
    """A project directory, with a hard boundary at its root."""

    root: Path
    deny: tuple[str, ...] = DEFAULT_DENY
    ignore: tuple[str, ...] = DEFAULT_IGNORE
    max_read_bytes: int = MAX_READ_BYTES
    _resolved_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser()
        if not root.is_dir():
            raise WorkspaceError(f"project directory does not exist: {root}")
        self._resolved_root = root.resolve()
        self.root = self._resolved_root

    @property
    def name(self) -> str:
        return self._resolved_root.name

    # -- the boundary -------------------------------------------------------

    def resolve(self, relpath: str, *, must_exist: bool = False) -> Path:
        """Resolve ``relpath`` against the root, refusing anything outside it.

        Symlinks are resolved *before* the containment check, so a link pointing
        out of the tree is caught rather than followed.
        """
        if not relpath or relpath.strip() in {".", ""}:
            return self._resolved_root

        candidate = Path(relpath).expanduser()
        if candidate.is_absolute():
            target = candidate.resolve()
        else:
            target = (self._resolved_root / candidate).resolve()

        if not self._within(target):
            raise WorkspaceError(
                f"{relpath!r} resolves outside the project root ({self._resolved_root}). "
                "Tools may only touch files inside the project."
            )
        if self.is_denied(target):
            raise WorkspaceError(
                f"{relpath!r} matches a denied pattern (credentials, keys, state files). "
                "It cannot be read or written, and its contents must not be requested."
            )
        if must_exist and not target.exists():
            raise WorkspaceError(f"{self.relative(target)} does not exist")
        return target

    def _within(self, path: Path) -> bool:
        try:
            path.relative_to(self._resolved_root)
        except ValueError:
            return False
        return True

    def is_denied(self, path: Path) -> bool:
        """True when any component of ``path`` matches a deny pattern."""
        try:
            parts = path.relative_to(self._resolved_root).parts
        except ValueError:  # pragma: no cover - guarded by resolve()
            return True
        for index, part in enumerate(parts):
            for pattern in self.deny:
                if Path(part).match(pattern):
                    return True
                # Deny patterns may name a path, e.g. ".git/config".
                if "/" in pattern and "/".join(parts[index:]) == pattern:
                    return True
        return False

    def is_ignored(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self._resolved_root).parts
        except ValueError:  # pragma: no cover
            return True
        return any(part in self.ignore for part in parts)

    def relative(self, path: Path) -> str:
        """Path relative to the root, for display. Never leaks the absolute path."""
        try:
            return path.relative_to(self._resolved_root).as_posix() or "."
        except ValueError:  # pragma: no cover - guarded by resolve()
            return str(path)

    # -- reading ------------------------------------------------------------

    def read(self, relpath: str) -> str:
        path = self.resolve(relpath, must_exist=True)
        if path.is_dir():
            raise WorkspaceError(f"{self.relative(path)} is a directory, not a file")
        size = path.stat().st_size
        if size > self.max_read_bytes:
            raise WorkspaceError(
                f"{self.relative(path)} is {size:,} bytes, over the {self.max_read_bytes:,} "
                "byte limit. Use grep to find the part you need."
            )
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{self.relative(path)} is not UTF-8 text") from exc

    def walk(self, relpath: str = "", *, pattern: str = "*") -> list[Path]:
        """Files under ``relpath`` matching ``pattern``, ignoring noise and secrets."""
        base = self.resolve(relpath)
        if base.is_file():
            return [base]
        found = [
            path
            for path in sorted(base.rglob(pattern))
            if path.is_file() and not self.is_ignored(path) and not self.is_denied(path)
        ]
        return found

    # -- environment --------------------------------------------------------

    def describe_environment(self) -> list[str]:
        """Lines about this workspace for the system prompt."""
        lines = [
            f"Project root: `{self._resolved_root}` (all paths are relative to it)"
        ]
        markers = {
            "pyproject.toml": "Python project (pyproject.toml)",
            "setup.py": "Python project (setup.py)",
            "requirements.txt": "Python project (requirements.txt)",
            "environment.yml": "conda environment",
            "main.tf": "Terraform root module",
            "dbt_project.yml": "dbt project",
        }
        present = [
            label for name, label in markers.items() if (self._resolved_root / name).exists()
        ]
        if present:
            lines.append("Detected: " + ", ".join(present))
        lines.append(
            "Credentials, key material and Terraform state are blocked from every tool."
        )
        return lines

    def detect_stacks(self) -> set[str]:
        """Guess which stacks this project uses, from its dependency manifests."""
        text = ""
        for name in ("pyproject.toml", "requirements.txt", "environment.yml", "setup.py"):
            candidate = self._resolved_root / name
            if candidate.is_file():
                try:
                    text += candidate.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:  # pragma: no cover
                    continue

        found: set[str] = set()
        signals = {
            "pyspark": ("pyspark", "delta-spark", "databricks"),
            "ml": ("scikit-learn", "sklearn", "xgboost", "lightgbm", "catboost"),
            "pytorch": ("torch", "pytorch-lightning", "transformers", "accelerate"),
            "rag": ("langchain", "llama-index", "llama_index", "chromadb", "faiss", "pinecone",
                    "qdrant", "weaviate", "sentence-transformers"),
        }
        for stack, needles in signals.items():
            if any(needle in text for needle in needles):
                found.add(stack)

        if any(self._resolved_root.glob("*.tf")):
            found.add("terraform")
        return found

    def close(self) -> None:
        """Nothing to release; present so the Agent lifecycle is uniform."""

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Workspace {self._resolved_root}>"


def open_workspace(target: str | os.PathLike[str] | Workspace | None) -> Workspace | None:
    """Build a :class:`Workspace` from whatever the caller supplied."""
    if target is None:
        return None
    if isinstance(target, Workspace):
        return target
    return Workspace(root=Path(target))


__all__ = ["DEFAULT_DENY", "DEFAULT_IGNORE", "MAX_READ_BYTES", "Workspace", "open_workspace"]
