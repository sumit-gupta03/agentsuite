"""Config precedence, domain options, and warehouse resolution."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from agentkart.core.config import Config, find_project_config, load_config
from agentkart.core.errors import ConfigError
from agentkart.domains.dataengineering.errors import WarehouseError
from agentkart.domains.dataengineering.warehouse import connect, load_adapter
from agentkart.domains.dataengineering.warehouse.base import TableRef


class TestConfigPrecedence:
    def test_defaults(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, user_config=tmp_path / "absent.toml")
        assert config.model == "claude-opus-5"
        assert config.write is False
        assert config.max_turns == 25

    def test_project_file_overrides_user_file(self, tmp_path: Path) -> None:
        user = tmp_path / "user.toml"
        user.write_text('[profile.default]\nmax_turns = 10\nmodel = "a"\n', encoding="utf-8")
        project = tmp_path / "proj" / ".agentlib"
        project.mkdir(parents=True)
        (project / "config.toml").write_text(
            "[profile.default]\nmax_turns = 20\n", encoding="utf-8"
        )

        config = load_config(cwd=tmp_path / "proj", user_config=user)
        assert config.max_turns == 20
        assert config.model == "a"

    def test_env_overrides_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        user = tmp_path / "user.toml"
        user.write_text("[profile.default]\nmax_turns = 10\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_MAX_TURNS", "99")
        assert load_config(cwd=tmp_path, user_config=user).max_turns == 99

    def test_kwargs_override_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_MAX_TURNS", "99")
        config = load_config(cwd=tmp_path, user_config=tmp_path / "x.toml", max_turns=5)
        assert config.max_turns == 5

    def test_named_profile(self, tmp_path: Path) -> None:
        user = tmp_path / "user.toml"
        user.write_text(
            textwrap.dedent(
                """\
                [profile.default]
                model = "claude-opus-5"

                [profile.prod]
                model = "claude-sonnet-5"
                max_turns = 50
                """
            ),
            encoding="utf-8",
        )
        assert load_config("prod", cwd=tmp_path, user_config=user).max_turns == 50
        assert load_config(cwd=tmp_path, user_config=user).model == "claude-opus-5"

    def test_boolean_env_parsing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_WRITE", "yes")
        assert load_config(cwd=tmp_path, user_config=tmp_path / "x.toml").write is True
        monkeypatch.setenv("AGENT_WRITE", "0")
        assert load_config(cwd=tmp_path, user_config=tmp_path / "x.toml").write is False

    def test_unknown_core_option_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="unknown config option"):
            load_config(cwd=tmp_path, user_config=tmp_path / "x.toml", nonsense=1)

    def test_bad_toml_names_the_file(self, tmp_path: Path) -> None:
        user = tmp_path / "user.toml"
        user.write_text("this is not [ valid toml", encoding="utf-8")
        with pytest.raises(ConfigError, match="user.toml"):
            load_config(cwd=tmp_path, user_config=user)

    def test_merged_rejects_unknown_keys(self) -> None:
        with pytest.raises(ConfigError):
            Config().merged(nonsense=1)


class TestDomainOptions:
    """Domain settings must never require a new field in core config."""

    def test_flat_keys_become_options(self, tmp_path: Path) -> None:
        user = tmp_path / "user.toml"
        user.write_text(
            textwrap.dedent(
                """\
                [profile.default]
                warehouse = "duckdb"
                max_rows = 50
                """
            ),
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path, user_config=user)
        assert config.option("warehouse") == "duckdb"
        assert config.option("max_rows") == 50

    def test_domain_subsection_is_merged(self, tmp_path: Path) -> None:
        user = tmp_path / "user.toml"
        user.write_text(
            textwrap.dedent(
                """\
                [profile.default]
                write = false

                [profile.default.dataengineering]
                warehouse = "snowflake"
                max_rows = 500
                """
            ),
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path, user_config=user, domain="dataengineering")
        assert config.option("warehouse") == "snowflake"
        assert config.write is False

    def test_other_domains_subsections_are_ignored(self, tmp_path: Path) -> None:
        """Two domains can share a profile without their settings colliding."""
        user = tmp_path / "user.toml"
        user.write_text(
            textwrap.dedent(
                """\
                [profile.default.dataengineering]
                warehouse = "snowflake"

                [profile.default.backenddevelopment]
                framework = "fastapi"
                """
            ),
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path, user_config=user, domain="dataengineering")
        assert config.option("warehouse") == "snowflake"
        assert config.option("framework") is None

    def test_env_sets_options_without_core_plumbing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_OPT_WAREHOUSE", "duckdb")
        config = load_config(cwd=tmp_path, user_config=tmp_path / "x.toml")
        assert config.option("warehouse") == "duckdb"

    def test_explicit_options_win(self, tmp_path: Path) -> None:
        user = tmp_path / "user.toml"
        user.write_text('[profile.default]\nwarehouse = "duckdb"\n', encoding="utf-8")
        config = load_config(cwd=tmp_path, user_config=user, options={"warehouse": "sqlite"})
        assert config.option("warehouse") == "sqlite"


class TestProjectSearch:
    def test_stops_at_the_home_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".agentlib").mkdir(parents=True)
        (home / ".agentlib" / "config.toml").write_text("[profile.default]\n", encoding="utf-8")
        work = home / "code"
        work.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert find_project_config(work) is None


class TestWarehouseResolution:
    def test_bare_name(self) -> None:
        with connect("sqlite") as wh:
            assert wh.name == "sqlite"

    def test_dsn_scheme_selects_the_adapter(self) -> None:
        with connect("sqlite://:memory:") as wh:
            assert wh.dialect == "sqlite"

    def test_mapping_config(self) -> None:
        with connect({"type": "sqlite", "database": ":memory:"}) as wh:
            assert wh.name == "sqlite"

    def test_mapping_without_type_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="'type'"):
            connect({"database": ":memory:"})

    def test_unknown_adapter_lists_the_known_ones(self) -> None:
        with pytest.raises(ConfigError, match="Known adapters"):
            load_adapter("oracle")

    def test_missing_driver_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "duckdb", None)
        monkeypatch.delitem(
            sys.modules, "agentkart.domains.dataengineering.warehouse.duckdb_adapter", raising=False
        )
        with pytest.raises(WarehouseError, match=r"agent\[duckdb\]"):
            load_adapter("duckdb")

    def test_an_existing_warehouse_passes_through(self, warehouse) -> None:  # type: ignore[no-untyped-def]
        assert connect(warehouse) is warehouse

    def test_warehouse_prints_as_its_name(self, warehouse) -> None:  # type: ignore[no-untyped-def]
        assert str(warehouse) == "sqlite"
        assert "SQLiteWarehouse sqlite" in repr(warehouse)


class TestTableRef:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("orders", ("orders", None, None)),
            ("raw.orders", ("orders", "raw", None)),
            ("db.raw.orders", ("orders", "raw", "db")),
            ('"raw"."orders"', ("orders", "raw", None)),
        ],
    )
    def test_parse(self, raw: str, expected: tuple) -> None:
        ref = TableRef.parse(raw)
        assert (ref.table, ref.schema, ref.database) == expected

    def test_too_many_parts_is_rejected(self) -> None:
        with pytest.raises(WarehouseError, match="too many parts"):
            TableRef.parse("a.b.c.d")

    def test_qualified_quotes_every_part(self) -> None:
        assert TableRef.parse("raw.orders").qualified('"') == '"raw"."orders"'


class TestIdentifierValidation:
    def test_injection_attempt_is_refused(self, warehouse) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(WarehouseError, match="cannot be safely interpolated"):
            warehouse.describe_table('orders"; DROP TABLE orders; --')
