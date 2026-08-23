"""The workspace boundary.

If any test here regresses, the agent can touch a file outside the project or
read a credential. These are the tests that matter most in the code domain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkart.domains.code.errors import WorkspaceError
from agentkart.domains.code.workspace import Workspace, open_workspace


@pytest.fixture
def project(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_main():\n    pass\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=sk-live-do-not-read\n", encoding="utf-8")
    (root / "secret.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (root / "terraform.tfstate").write_text('{"secret": "hunter2"}', encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "app.pyc").write_text("junk", encoding="utf-8")
    # Something outside the project that must never be reachable.
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    return Workspace(root=root)


class TestBoundary:
    @pytest.mark.parametrize(
        "path",
        [
            "../outside.txt",
            "../../outside.txt",
            "src/../../outside.txt",
            "./src/./../../outside.txt",
        ],
    )
    def test_relative_escape_is_refused(self, project: Workspace, path: str) -> None:
        with pytest.raises(WorkspaceError, match="outside the project root"):
            project.resolve(path)

    def test_absolute_path_outside_is_refused(self, project: Workspace, tmp_path: Path) -> None:
        with pytest.raises(WorkspaceError, match="outside the project root"):
            project.resolve(str(tmp_path / "outside.txt"))

    def test_absolute_path_inside_is_allowed(self, project: Workspace) -> None:
        resolved = project.resolve(str(project.root / "src" / "app.py"))
        assert project.relative(resolved) == "src/app.py"

    def test_symlink_out_of_the_tree_is_refused(self, project: Workspace, tmp_path: Path) -> None:
        link = project.root / "escape"
        try:
            link.symlink_to(tmp_path / "outside.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted in this environment")
        with pytest.raises(WorkspaceError, match="outside the project root"):
            project.resolve("escape")

    def test_root_itself_resolves(self, project: Workspace) -> None:
        assert project.resolve("") == project.root
        assert project.resolve(".") == project.root

    def test_relative_never_leaks_the_absolute_path(self, project: Workspace) -> None:
        assert project.relative(project.root / "src" / "app.py") == "src/app.py"


class TestDenyList:
    @pytest.mark.parametrize(
        "path", [".env", "secret.pem", "terraform.tfstate"]
    )
    def test_credentials_are_refused(self, project: Workspace, path: str) -> None:
        with pytest.raises(WorkspaceError, match="denied pattern"):
            project.resolve(path)

    def test_denied_files_are_not_listed(self, project: Workspace) -> None:
        listed = {project.relative(p) for p in project.walk("")}
        assert ".env" not in listed
        assert "secret.pem" not in listed
        assert "terraform.tfstate" not in listed
        assert "src/app.py" in listed

    def test_reading_a_denied_file_is_refused(self, project: Workspace) -> None:
        with pytest.raises(WorkspaceError, match="denied pattern"):
            project.read(".env")


class TestReading:
    def test_reads_a_file(self, project: Workspace) -> None:
        assert "def main" in project.read("src/app.py")

    def test_missing_file_says_so(self, project: Workspace) -> None:
        with pytest.raises(WorkspaceError, match="does not exist"):
            project.read("src/nope.py")

    def test_directory_is_not_a_file(self, project: Workspace) -> None:
        with pytest.raises(WorkspaceError, match="is a directory"):
            project.read("src")

    def test_oversized_file_is_refused(self, project: Workspace) -> None:
        big = project.root / "big.txt"
        big.write_text("x" * 1000, encoding="utf-8")
        project.max_read_bytes = 100
        with pytest.raises(WorkspaceError, match="over the"):
            project.read("big.txt")

    def test_binary_file_is_refused(self, project: Workspace) -> None:
        (project.root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        with pytest.raises(WorkspaceError, match="not UTF-8"):
            project.read("blob.bin")


class TestWalking:
    def test_ignores_noise_directories(self, project: Workspace) -> None:
        listed = {project.relative(p) for p in project.walk("")}
        assert not any(p.startswith("__pycache__") for p in listed)

    def test_glob_filters(self, project: Workspace) -> None:
        listed = {project.relative(p) for p in project.walk("", pattern="**/*.py")}
        assert "src/app.py" in listed
        assert all(p.endswith(".py") for p in listed)

    def test_walk_below_a_subdirectory(self, project: Workspace) -> None:
        listed = {project.relative(p) for p in project.walk("src")}
        assert listed == {"src/app.py"}


class TestConstruction:
    def test_missing_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(WorkspaceError, match="does not exist"):
            Workspace(root=tmp_path / "nope")

    def test_open_workspace_passthrough(self, project: Workspace) -> None:
        assert open_workspace(project) is project
        assert open_workspace(None) is None

    def test_prints_as_its_name(self, project: Workspace) -> None:
        assert str(project) == "project"


class TestStackDetection:
    def test_detects_from_dependencies(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pyspark>=3.5", "scikit-learn", "torch"]\n',
            encoding="utf-8",
        )
        assert Workspace(root=root).detect_stacks() == {"pyspark", "ml", "pytorch"}

    def test_detects_terraform_from_files(self, tmp_path: Path) -> None:
        root = tmp_path / "infra"
        root.mkdir()
        (root / "main.tf").write_text('resource "null_resource" "x" {}\n', encoding="utf-8")
        assert "terraform" in Workspace(root=root).detect_stacks()

    def test_empty_project_detects_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        assert Workspace(root=root).detect_stacks() == set()
