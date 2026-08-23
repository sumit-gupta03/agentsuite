"""Provider selection and optional skill retrieval.

Two features that exist so the library fits the environment it lands in: use
whichever LLM you actually have, and pay for only the skills a request needs.
"""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import pytest

import agentkart as lib
from agentkart.core.errors import ConfigError
from agentkart.core.providers import available_providers, resolve_model, split_spec
from agentkart.core.retrieval import (
    EmbeddingSelector,
    KeywordSelector,
    describe_selection,
    worth_retrieving,
)
from agentkart.core.skills import Skill

#: These construct a real backend, so they need the optional SDK present.
needs_boto3 = pytest.mark.skipif(
    find_spec("boto3") is None, reason="boto3 not installed"
)


class TestSpecParsing:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("claude-opus-5", ("anthropic", "claude-opus-5")),
            ("anthropic:claude-opus-5", ("anthropic", "claude-opus-5")),
            ("claude:claude-sonnet-5", ("anthropic", "claude-sonnet-5")),
            ("bedrock:amazon.nova-pro-v1:0", ("bedrock", "amazon.nova-pro-v1:0")),
            ("nova:amazon.nova-lite-v1:0", ("bedrock", "amazon.nova-lite-v1:0")),
            ("openai:gpt-4o", ("openai", "gpt-4o")),
            ("gpt-4o", ("openai", "gpt-4o")),
            ("o3-mini", ("openai", "o3-mini")),
        ],
    )
    def test_explicit_and_aliased_providers(self, spec: str, expected: tuple) -> None:
        assert split_spec(spec) == expected

    @pytest.mark.parametrize(
        ("spec", "provider"),
        [
            ("amazon.nova-pro-v1:0", "bedrock"),
            ("us.anthropic.claude-sonnet-4-5-v1:0", "bedrock"),
            ("meta.llama3-70b-instruct-v1:0", "bedrock"),
            ("mistral.mistral-large-2407-v1:0", "bedrock"),
        ],
    )
    def test_bare_bedrock_ids_are_recognised(self, spec: str, provider: str) -> None:
        """A Bedrock id contains a colon; it must not be read as a provider prefix."""
        assert split_spec(spec) == (provider, spec)

    def test_an_unrecognised_id_defaults_to_anthropic_rather_than_guessing(self) -> None:
        assert split_spec("some-new-model-2027") == ("anthropic", "some-new-model-2027")

    def test_an_empty_spec_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):
            split_spec("   ")

    def test_an_unknown_provider_lists_the_known_ones(self) -> None:
        with pytest.raises(ConfigError, match="Available"):
            resolve_model("nosuchprovider:model-x")


class TestResolution:
    def test_a_constructed_model_passes_through(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        built = fake_model(["ok"])
        assert resolve_model(built) is built

    def test_builds_a_bedrock_model_from_a_spec(self) -> None:
        model = resolve_model("bedrock:amazon.nova-pro-v1:0", client=object())
        assert type(model).__name__ == "BedrockModel"
        assert model.model_id == "amazon.nova-pro-v1:0"

    def test_options_a_backend_cannot_use_are_dropped(self) -> None:
        """One config can carry settings for several providers."""
        model = resolve_model(
            "bedrock:amazon.nova-pro-v1:0",
            client=object(),
            max_tokens=1234,
            effort="high",          # Anthropic-only; must not raise here
            warehouse="snowflake",  # not a model setting at all
        )
        assert model.max_tokens == 1234

    def test_available_providers_checks_the_sdk_not_the_backend(self) -> None:
        """Backends import their SDK lazily; importing one proves nothing."""
        status = available_providers()
        assert set(status) == {"anthropic", "bedrock", "openai"}
        assert status["anthropic"] is True  # a hard dependency of this package

    def test_list_providers_is_exposed_for_users_to_choose_from(self) -> None:
        listed = lib.list_providers()
        names = {name for name, _about, _installed in listed}
        assert names == {"anthropic", "bedrock", "openai"}
        assert all(about for _n, about, _i in listed)


class TestAgentAcceptsAnyProvider:
    @needs_boto3
    def test_a_model_spec_string_selects_the_backend(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        dev = lib.code(project=tmp_path, model="bedrock:amazon.nova-pro-v1:0")
        assert type(dev.model).__name__ == "BedrockModel"
        assert dev.audit.manifest.model == "amazon.nova-pro-v1:0"
        dev.close()

    def test_the_default_is_still_claude(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        dev = lib.code(project=tmp_path)
        assert type(dev.model).__name__ == "ClaudeModel"
        dev.close()

    @needs_boto3
    def test_config_can_select_the_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_MODEL changes the provider with no code change."""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_MODEL", "bedrock:amazon.nova-lite-v1:0")
        dev = lib.code(project=tmp_path)
        assert type(dev.model).__name__ == "BedrockModel"
        dev.close()


def _skill(name: str, description: str) -> Skill:
    return Skill(name=name, description=description, body="B", path=Path(name), source="bundled")


@pytest.fixture
def library() -> dict[str, Skill]:
    return {
        s.name: s
        for s in [
            _skill("pyspark-performance", "Spark job skew, shuffles, partitioning and joins."),
            _skill("pytorch-training", "PyTorch training loops, dataloaders and gradients."),
            _skill("rag-retrieval", "Chunking, embeddings, vector search and reranking."),
            _skill("terraform-review", "Terraform plans, state, modules and providers."),
            _skill("python-craft", "General Python: errors, typing, structure and tests."),
            _skill("ml-evaluation", "Choosing a metric, thresholds and calibration."),
        ]
    }


class TestKeywordSelector:
    def test_narrows_to_the_relevant_skills(self, library: dict[str, Skill]) -> None:
        chosen = KeywordSelector(limit=2).select("the spark job skews on customer_id", library)
        assert "pyspark-performance" in chosen
        assert len(chosen) <= 2

    def test_a_small_library_is_returned_whole(self, library: dict[str, Skill]) -> None:
        """Below the limit there is nothing to gain by narrowing."""
        assert KeywordSelector(limit=20).select("anything", library) == library

    def test_always_skills_survive_a_low_score(self, library: dict[str, Skill]) -> None:
        chosen = KeywordSelector(limit=2, always=("python-craft",)).select(
            "terraform state is locked", library
        )
        assert "python-craft" in chosen

    def test_ranking_is_deterministic(self, library: dict[str, Skill]) -> None:
        """A stable selection is what lets the prompt cache keep hitting."""
        selector = KeywordSelector(limit=3)
        first = list(selector.select("pytorch training loop", library))
        second = list(selector.select("pytorch training loop", library))
        assert first == second

    def test_an_empty_prompt_does_not_crash(self, library: dict[str, Skill]) -> None:
        assert isinstance(KeywordSelector(limit=2).select("", library), dict)


class TestEmbeddingSelector:
    def test_uses_the_supplied_embedder(self, library: dict[str, Skill]) -> None:
        calls: list[int] = []

        def embed(texts):  # type: ignore[no-untyped-def]
            calls.append(len(texts))
            # One dimension per skill; the query leans towards pytorch.
            table = {
                "pyspark performance": [1, 0, 0],
                "pytorch training": [0, 1, 0],
                "rag retrieval": [0, 0, 1],
            }
            out = []
            for text in texts:
                vector = [0.0, 1.0, 0.0] if "pytorch" in text.lower() else [1.0, 0.0, 0.0]
                for key, value in table.items():
                    if text.lower().startswith(key):
                        vector = [float(v) for v in value]
                out.append(vector)
            return out

        selector = EmbeddingSelector(embed=embed, limit=1)
        chosen = selector.select("debug my pytorch training loop", library)
        assert "pytorch-training" in chosen
        assert calls, "the embedder must actually be called"

    def test_the_library_is_embedded_once_not_per_call(self, library: dict[str, Skill]) -> None:
        batches: list[int] = []

        def embed(texts):  # type: ignore[no-untyped-def]
            batches.append(len(texts))
            return [[1.0, 0.0] for _ in texts]

        selector = EmbeddingSelector(embed=embed, limit=2)
        selector.select("first request", library)
        selector.select("second request", library)
        # One batch for the library, then one per query.
        assert batches[0] == len(library)
        assert batches[1:] == [1, 1]

    def test_a_failing_embedder_falls_back_rather_than_breaking_the_run(
        self, library: dict[str, Skill]
    ) -> None:
        def broken(texts):  # type: ignore[no-untyped-def]
            raise RuntimeError("embedding service is down")

        chosen = EmbeddingSelector(embed=broken, limit=2).select("spark skew", library)
        assert chosen, "a retrieval outage must not empty the skill index"
        assert "pyspark-performance" in chosen

    def test_an_external_store_is_used_when_given(self, library: dict[str, Skill]) -> None:
        class FakeStore:
            def __init__(self) -> None:
                self.rows: list[tuple] = []

            def upsert(self, records):  # type: ignore[no-untyped-def]
                self.rows.extend(records)

            def search(self, vector, *, limit):  # type: ignore[no-untyped-def]
                return [("terraform-review", 0.9)]

        store = FakeStore()
        chosen = EmbeddingSelector(
            embed=lambda texts: [[1.0] for _ in texts], store=store, limit=1
        ).select("anything at all", library)
        assert list(chosen) == ["terraform-review"]
        assert len(store.rows) == len(library), "the library was written to the store"


class TestIntegration:
    def test_narrowing_says_what_is_still_reachable(self, library: dict[str, Skill]) -> None:
        """A retrieval miss must cost one tool call, never a lost capability."""
        note = describe_selection(["python-craft"], library)
        assert "5 further skill(s)" in note
        assert "load_skill" in note

    def test_no_note_when_nothing_was_hidden(self, library: dict[str, Skill]) -> None:
        assert describe_selection(library, library) == ""

    def test_selection_is_off_by_default(self, tmp_path: Path, fake_model) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        dev = lib.code(project=tmp_path, model=fake_model(["ok"]))
        dev.run("anything")
        assert dev.advertised_skills == dev.skills
        dev.close()

    def test_a_selector_narrows_the_prompt(self, tmp_path: Path, fake_model) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pyspark", "torch"]\n', encoding="utf-8"
        )
        dev = lib.code(
            project=tmp_path,
            model=fake_model(["ok"]),
            skill_selector=KeywordSelector(limit=2),
        )
        dev.run("the spark job skews on customer_id")
        assert len(dev.advertised_skills) < len(dev.skills)
        assert "further skill(s) are available" in dev.system_prompt
        dev.close()

    def test_narrowing_is_recorded(self, tmp_path: Path, fake_model) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pyspark", "torch"]\n', encoding="utf-8"
        )
        dev = lib.code(
            project=tmp_path, model=fake_model(["ok"]), skill_selector=KeywordSelector(limit=2)
        )
        dev.run("spark skew")
        assert dev.audit.of_kind("skills_selected")
        dev.close()


def test_worth_retrieving_is_honest_about_small_libraries() -> None:
    assert not worth_retrieving({str(i): None for i in range(10)})
    assert worth_retrieving({str(i): None for i in range(200)})
