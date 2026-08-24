"""Skill parsing, precedence and sandboxing."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentsuite.core.errors import SkillError
from agentsuite.core.skills import (
    Skill,
    discover_dir,
    find_project_skills_dir,
    parse_skill_file,
    render_index,
    resolve_skills,
)
from agentsuite.domains.dataengineering import DOMAIN as DE_DOMAIN

BUNDLED = DE_DOMAIN.skills_dir


def write_skill(root: Path, name: str, description: str, body: str = "Body.", **front) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in front.items())
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return directory


class TestParsing:
    def test_parses_frontmatter_and_body(self, skill_dir: Path) -> None:
        skill = parse_skill_file(skill_dir / "house-style" / "SKILL.md", source="explicit")
        assert skill.name == "house-style"
        assert skill.description == "Team conventions for SQL and naming."
        assert "Every table carries" in skill.body
        assert skill.source == "explicit"

    def test_description_is_whitespace_normalised(self, tmp_path: Path) -> None:
        path = tmp_path / "s" / "SKILL.md"
        path.parent.mkdir()
        path.write_text(
            "---\nname: s\ndescription: >-\n  one\n  two\n  three\n---\n\nBody\n", encoding="utf-8"
        )
        assert parse_skill_file(path, source="explicit").description == "one two three"

    def test_missing_frontmatter_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "s" / "SKILL.md"
        path.parent.mkdir()
        path.write_text("# Just markdown\n", encoding="utf-8")
        with pytest.raises(SkillError, match="missing YAML frontmatter"):
            parse_skill_file(path, source="explicit")

    def test_missing_description_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "s" / "SKILL.md"
        path.parent.mkdir()
        path.write_text("---\nname: s\n---\n\nBody\n", encoding="utf-8")
        with pytest.raises(SkillError, match="description"):
            parse_skill_file(path, source="explicit")

    def test_unterminated_frontmatter_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "s" / "SKILL.md"
        path.parent.mkdir()
        path.write_text("---\nname: s\ndescription: d\n\nBody\n", encoding="utf-8")
        with pytest.raises(SkillError, match="not terminated"):
            parse_skill_file(path, source="explicit")

    def test_requires_accepts_a_bare_string(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "s", "d", requires="dbt")
        assert parse_skill_file(tmp_path / "s" / "SKILL.md", source="explicit").requires == ("dbt",)

    def test_unknown_frontmatter_lands_in_metadata(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "s", "d", version="2")
        skill = parse_skill_file(tmp_path / "s" / "SKILL.md", source="explicit")
        assert skill.metadata["version"] == 2


class TestDiscovery:
    def test_skips_directories_without_a_skill_file(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "real", "d")
        (tmp_path / "not-a-skill").mkdir()
        (tmp_path / ".hidden").mkdir()
        assert [s.name for s in discover_dir(tmp_path, source="explicit")] == ["real"]

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert discover_dir(tmp_path / "nope", source="explicit") == []

    def test_bundled_library_parses(self) -> None:
        bundled = discover_dir(BUNDLED, source="bundled")
        names = {s.name for s in bundled}
        assert "incremental-backfill" in names
        assert "sql-review" in names
        # Every bundled skill must have a usable description -- it is the only
        # thing the model sees before deciding to load it.
        for skill in bundled:
            assert len(skill.description) > 40, skill.name


class TestPrecedence:
    def test_project_overrides_bundled(self, tmp_path: Path) -> None:
        project = tmp_path / ".agentlib" / "skills"
        write_skill(project, "sql-review", "Local override.", body="Our rules.")
        resolved = resolve_skills(bundled_dir=BUNDLED, project_dir=project, capabilities=None)
        assert resolved["sql-review"].source == "project"
        assert resolved["sql-review"].body == "Our rules."

    def test_explicit_overrides_project(self, tmp_path: Path) -> None:
        project = tmp_path / ".agentlib" / "skills"
        write_skill(project, "shared", "From project.")
        explicit = tmp_path / "explicit"
        write_skill(explicit, "shared", "From explicit.")
        resolved = resolve_skills(
            project_dir=project, extra_dirs=[explicit], capabilities=None
        )
        assert resolved["shared"].source == "explicit"

    def test_user_loses_to_project(self, tmp_path: Path) -> None:
        user = tmp_path / "user"
        project = tmp_path / "project"
        write_skill(user, "x", "From user.")
        write_skill(project, "x", "From project.")
        resolved = resolve_skills(user_dir=user, project_dir=project, capabilities=None)
        assert resolved["x"].source == "project"

    def test_domain_tier_beats_shared_tier(self, tmp_path: Path) -> None:
        """A skill under skills/<domain>/ outranks the same name in skills/."""
        user = tmp_path / "user"
        write_skill(user, "x", "Shared version.")
        write_skill(user / "dataengineering", "x", "Domain version.")
        resolved = resolve_skills(user_dir=user, domain="dataengineering", capabilities=None)
        assert resolved["x"].source == "user:domain"
        assert resolved["x"].description == "Domain version."

    def test_domain_subdirectory_is_not_parsed_as_a_skill(self, tmp_path: Path) -> None:
        user = tmp_path / "user"
        write_skill(user / "dataengineering", "inner", "Domain-scoped.")
        resolved = resolve_skills(user_dir=user, domain="dataengineering", capabilities=None)
        assert set(resolved) == {"inner"}

    def test_explicit_accepts_a_single_skill_directory(self, tmp_path: Path) -> None:
        directory = write_skill(tmp_path, "solo", "One skill, passed directly.")
        assert "solo" in resolve_skills(extra_dirs=[directory], capabilities=None)

    def test_missing_explicit_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SkillError, match="does not exist"):
            resolve_skills(extra_dirs=[tmp_path / "nope"])

    def test_plugins_are_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = False

        def spy() -> list[Skill]:
            nonlocal called
            called = True
            return []

        monkeypatch.setattr("agentsuite.core.skills.discover_plugins", spy)
        resolve_skills(capabilities=None)
        assert not called, "installed packages must not inject skills without opt-in"


class TestProjectSearch:
    def test_finds_a_project_directory(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / ".agentlib" / "skills").mkdir(parents=True)
        found = find_project_skills_dir(tmp_path / "repo" / "src")
        assert found == tmp_path / "repo" / ".agentlib" / "skills"

    def test_stops_at_the_home_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The home directory is the *user* tier and must not be found as a project.

        Without this guard, running from anywhere under ~ picks up
        ~/.agentlib/skills and relabels it 'project' -- the same skills counted
        twice, at the wrong precedence.
        """
        home = tmp_path / "home"
        (home / ".agentlib" / "skills").mkdir(parents=True)
        work = home / "code" / "project"
        work.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert find_project_skills_dir(work) is None


class TestFiltering:
    def test_disable_removes_a_skill(self) -> None:
        resolved = resolve_skills(bundled_dir=BUNDLED, disable=["sql-review"], capabilities=None)
        assert "sql-review" not in resolved

    def test_requires_filters_on_capabilities(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "needs-dbt", "Requires dbt.", requires="[dbt]")
        assert "needs-dbt" not in resolve_skills(
            extra_dirs=[tmp_path], capabilities={"warehouse"}
        )
        assert "needs-dbt" in resolve_skills(
            extra_dirs=[tmp_path], capabilities={"warehouse", "dbt"}
        )

    def test_none_capabilities_disables_filtering(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "needs-dbt", "Requires dbt.", requires="[dbt]")
        assert "needs-dbt" in resolve_skills(extra_dirs=[tmp_path], capabilities=None)


class TestSupportingFiles:
    def test_lists_and_reads_bundled_files(self, skill_dir: Path) -> None:
        skill = parse_skill_file(skill_dir / "house-style" / "SKILL.md", source="explicit")
        assert skill.list_files() == ["reference/naming.md"]
        assert skill.read_file("reference/naming.md") == "snake_case only."

    def test_path_traversal_is_refused(self, skill_dir: Path) -> None:
        skill = parse_skill_file(skill_dir / "house-style" / "SKILL.md", source="explicit")
        with pytest.raises(SkillError, match="escapes"):
            skill.read_file("../../../etc/passwd")

    def test_missing_file_is_an_error(self, skill_dir: Path) -> None:
        skill = parse_skill_file(skill_dir / "house-style" / "SKILL.md", source="explicit")
        with pytest.raises(SkillError, match="no file"):
            skill.read_file("reference/absent.md")


def test_render_index_is_one_line_per_skill(tmp_path: Path) -> None:
    write_skill(tmp_path, "a", "First.")
    write_skill(tmp_path, "b", "Second.")
    resolved = resolve_skills(extra_dirs=[tmp_path], capabilities=None)
    rendered = render_index(resolved.values())
    assert rendered.count("\n") == 1
    assert "**a**" in rendered


def test_render_index_handles_an_empty_library() -> None:
    assert "No skills" in render_index([])


def test_resolution_is_deterministic(tmp_path: Path) -> None:
    """Byte-stable ordering is what lets the prompt cache hit."""
    for name in ("zeta", "alpha", "mid"):
        write_skill(tmp_path, name, f"Skill {name} for testing precedence order.")
    first = list(resolve_skills(extra_dirs=[tmp_path], capabilities=None))
    second = list(resolve_skills(extra_dirs=[tmp_path], capabilities=None))
    assert first == second == ["alpha", "mid", "zeta"]
