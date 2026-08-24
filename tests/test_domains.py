"""Domain resolution, presets, and the lazy top-level namespace.

The point of this file: adding a domain or a preset must cost nothing in
:mod:`agentsuite.core`, and ``import agent`` must stay free however many exist.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import agentsuite as lib
from agentsuite.core.domain import Domain, DomainRegistry, resolve_preset
from agentsuite.core.errors import ConfigError


class TestRegistry:
    def test_resolves_the_builtin_domain(self) -> None:
        assert DomainRegistry().get("dataengineering").name == "dataengineering"

    @pytest.mark.parametrize("alias", ["de", "data", "DataEngineering", "data-engineering"])
    def test_aliases_and_normalisation(self, alias: str) -> None:
        assert DomainRegistry().get(alias).name == "dataengineering"

    def test_unknown_domain_lists_the_known_ones(self) -> None:
        with pytest.raises(ConfigError, match="Available"):
            DomainRegistry().get("nonsense")

    def test_a_runtime_domain_can_be_registered(self) -> None:
        registry = DomainRegistry()
        custom = Domain(name="mlops", description="models in production", package="agent.core")
        registry.register(custom)
        assert registry.get("mlops") is custom

    def test_describe_returns_name_and_description(self) -> None:
        described = dict(DomainRegistry().describe())
        assert "dataengineering" in described
        assert described["dataengineering"]


class TestPresets:
    def test_preset_supplies_defaults(self) -> None:
        domain = DomainRegistry().get("dataengineering")
        merged = resolve_preset(domain, "snowflake", {})
        assert merged["warehouse"] == "snowflake"

    def test_explicit_arguments_beat_the_preset(self) -> None:
        """A preset is a starting point, not a constraint."""
        domain = DomainRegistry().get("dataengineering")
        merged = resolve_preset(domain, "snowflake", {"warehouse": "duckdb"})
        assert merged["warehouse"] == "duckdb"

    def test_unknown_preset_lists_the_known_ones(self) -> None:
        domain = DomainRegistry().get("dataengineering")
        with pytest.raises(ConfigError, match="Available"):
            resolve_preset(domain, "nope", {})

    def test_no_preset_is_a_passthrough(self) -> None:
        domain = DomainRegistry().get("dataengineering")
        assert resolve_preset(domain, None, {"a": 1}) == {"a": 1}


class TestNamespace:
    def test_domain_is_reachable_as_an_attribute(self) -> None:
        assert callable(lib.dataengineering)

    def test_alias_is_reachable(self) -> None:
        assert callable(lib.de)

    def test_preset_is_reachable_as_an_attribute(self) -> None:
        """agent.duckdb() is the data domain with one option pinned."""
        assert callable(lib.duckdb)
        assert "preset" in (lib.duckdb.__doc__ or "")

    def test_preset_builds_the_right_agent(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = lib.sql(model=fake_model(["ok"]))
        # The 'sql' preset drops the pipeline-authoring skills.
        assert "dbt-model-authoring" not in de.skills
        assert "incremental-backfill" not in de.skills
        assert "sql-review" in de.skills

    def test_unknown_attribute_raises(self) -> None:
        with pytest.raises(AttributeError, match="no attribute"):
            lib.definitely_not_a_domain  # noqa: B018

    def test_domains_and_presets_are_listable(self) -> None:
        assert ("dataengineering", "warehouses, pipelines, SQL and dbt") in lib.list_domains()
        assert "snowflake" in lib.list_presets()["dataengineering"]

    def test_domains_subpackage_does_not_shadow_the_listing(self) -> None:
        """`agentsuite.domains` is the subpackage; the listing function is list_domains.

        Importing any domain sets ``agentsuite.domains`` to the subpackage, so a
        function of that name would silently stop being callable.
        """
        import agentsuite.domains.dataengineering  # noqa: F401

        assert callable(lib.list_domains)
        assert lib.list_domains()

    def test_dir_includes_domains_and_presets(self) -> None:
        listed = dir(lib)
        assert "dataengineering" in listed
        assert "snowflake" in listed
        assert "Agent" in listed


class TestImportPurity:
    def test_importing_the_package_pulls_in_nothing(self) -> None:
        """`import agent` must not construct anything or connect anywhere.

        This is what keeps import cost flat as domains are added.

        Run in a subprocess: clearing ``sys.modules`` in-process would rebind
        every class the already-imported test modules hold references to, so
        later tests would compare a fresh ToolError against a stale one.
        """
        script = (
            "import sys, json; import agentsuite; "
            "print(json.dumps({"
            "'submodules': sorted(m for m in sys.modules if m.startswith('agentsuite.')), "
            "'version': agentsuite.__version__}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["submodules"] == [], f"import pulled in {payload['submodules']}"
        assert payload["version"]

    def test_attribute_access_resolves_lazily(self) -> None:
        import agentsuite as agent

        assert agent.Agent is not None
        assert "agentsuite.core.loop" in sys.modules


class TestDbtGating:
    """dbt tools must only appear for a directory that really is a dbt project."""

    def test_a_directory_without_dbt_project_yml_is_not_a_dbt_project(
        self, fake_model, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        de = lib.dataengineering(
            warehouse="sqlite", dbt_project_dir=str(tmp_path), model=fake_model(["ok"])
        )
        assert not any(name.startswith("dbt_") for name in de.tools.names())
        assert "dbt" not in de.capabilities
        assert "dbt-model-authoring" not in de.skills
        de.close()

    def test_a_real_dbt_project_loads_the_tools(self, fake_model, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
        de = lib.dbt(
            warehouse="sqlite", dbt_project_dir=str(tmp_path), model=fake_model(["ok"])
        )
        assert "dbt_run" in de.tools
        assert "dbt-model-authoring" in de.skills
        de.close()

    def test_the_dbt_preset_insists_on_a_project(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ConfigError, match="dbt_project.yml"):
            lib.dbt(warehouse="sqlite", model=fake_model(["ok"]))

    def test_dbt_run_is_destructive(self, fake_model, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
        de = lib.dbt(
            warehouse="sqlite",
            dbt_project_dir=str(tmp_path),
            write=True,
            model=fake_model(["ok"]),
        )
        assert de.tools.get("dbt_run").destructive is True
        assert "dbt_run" in de.audit.manifest.destructive_tools
        de.close()
