"""MCP configuration and the governance layer.

The MCP client is not exercised against a live server here -- that needs the
optional ``mcp`` package and a real subprocess. What *is* tested is everything
that decides whether an MCP call is allowed, which is the part that matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkart.core.audit import AuditLog, RunManifest, redact
from agentkart.core.errors import ConfigError
from agentkart.core.mcp import DEFAULT_TIER, MCPServer, MCPToolPolicy, servers_from_config


class TestMCPConfiguration:
    def test_defaults_to_read_tier(self) -> None:
        """Read is the only safe default for code you did not write."""
        assert DEFAULT_TIER == "read"
        assert MCPServer.from_config("x", {"command": "srv"}).tier == "read"

    def test_rejects_an_unknown_tier(self) -> None:
        with pytest.raises(ConfigError, match="expected read, write or destructive"):
            MCPServer.from_config("x", {"command": "srv", "tier": "trusted"})

    def test_parses_a_full_definition(self) -> None:
        server = MCPServer.from_config(
            "github",
            {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "tier": "write",
                "deny_tools": ["delete_repository"],
                "timeout": 30,
            },
        )
        assert server.tier == "write"
        assert server.args[0] == "-y"
        assert server.timeout == 30

    def test_allow_and_deny_lists(self) -> None:
        server = MCPServer("s", allow_tools=("search", "read"), deny_tools=("read",))
        assert server.exposes("search")
        assert not server.exposes("read"), "deny beats allow"
        assert not server.exposes("write"), "not on the allow list"

    def test_no_allow_list_exposes_everything_not_denied(self) -> None:
        server = MCPServer("s", deny_tools=("dangerous",))
        assert server.exposes("anything")
        assert not server.exposes("dangerous")

    def test_missing_command_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no command"):
            MCPServer("s").validate()

    def test_command_not_on_path_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not on PATH"):
            MCPServer("s", command="definitely-not-a-real-binary-xyz").validate()

    def test_servers_from_config(self) -> None:
        servers = servers_from_config(
            {"a": {"command": "x"}, "b": {"command": "y", "tier": "write"}}
        )
        assert {s.name for s in servers} == {"a", "b"}

    def test_non_mapping_config_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="table"):
            servers_from_config(["not", "a", "mapping"])

    def test_no_servers_means_no_servers(self) -> None:
        assert servers_from_config(None) == []


class TestMCPPolicy:
    def test_uses_the_operator_assigned_tier(self) -> None:
        policy = MCPToolPolicy(tiers={"docs": "read"})
        assert policy.check("docs.search", server="docs", tool="search").allowed

    def test_write_tier_needs_a_write_session(self) -> None:
        policy = MCPToolPolicy(tiers={"gh": "write"})
        verdict = policy.check("gh.create_issue", server="gh", tool="create_issue")
        assert not verdict.allowed
        assert "read-only" in verdict.reason

    def test_destructive_tier_needs_confirmation(self) -> None:
        policy = MCPToolPolicy(write=True, tiers={"infra": "destructive"})
        verdict = policy.check("infra.delete", server="infra", tool="delete")
        assert not verdict.allowed
        assert verdict.needs_confirmation

    def test_unconfigured_server_fails_closed(self) -> None:
        """A server the operator never configured gets no trust at all."""
        policy = MCPToolPolicy(write=True, tiers={"known": "read"})
        verdict = policy.check("rogue.exfiltrate", server="rogue", tool="exfiltrate")
        assert not verdict.allowed
        assert "not configured" in (verdict.primary.reason if verdict.primary else "")


class TestRedaction:
    """Every string below is a synthetic placeholder, never a real credential.

    They must look credential-shaped or they would not exercise the redaction
    they exist to prove. Each is deliberately self-identifying -- an EXAMPLE or
    FAKE marker, or AWS's own published documentation key. A secret scanner
    flagging this file is a false positive; see ``.gitleaks.toml``.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "ANTHROPIC_API_KEY=sk-ant-api03-EXAMPLE-NOT-A-REAL-KEY-00000000",
            "token: ghp_EXAMPLENOTAREALTOKEN000000000000",
            # AWS's own documentation example key. Ends in EXAMPLE by design.
            "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
            "password: fake-placeholder-value",
            "postgresql://user:FAKEPASSWORDEXAMPLE@host/db",
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
    )
    def test_secrets_are_redacted(self, text: str) -> None:
        cleaned = redact(text)
        assert "REDACTED" in cleaned, f"missed a secret in {text!r}"

    def test_secret_value_does_not_survive(self) -> None:
        dsn = "postgresql://user:FAKEPASSWORDEXAMPLE@host/db"
        assert "FAKEPASSWORDEXAMPLE" not in redact(dsn)
        assert "fake-placeholder-value" not in redact("password: fake-placeholder-value")

    def test_context_survives_redaction(self) -> None:
        """A reviewer needs to see *what* was redacted, not a blank line."""
        cleaned = redact("postgresql://user:FAKEPASSWORDEXAMPLE@host/db")
        assert "postgresql://" in cleaned
        assert "host/db" in cleaned

    def test_ordinary_text_is_untouched(self) -> None:
        text = "def main():\n    return compute(rows)"
        assert redact(text) == text


class TestAuditLog:
    def test_records_and_sequences(self) -> None:
        log = AuditLog()
        log.record("action", detail="one")
        log.record("action", detail="two")
        assert [e.seq for e in log.events] == [1, 2]

    def test_redacts_on_the_way_in(self, tmp_path: Path) -> None:
        """Secrets must never reach the file, not merely be hidden on display."""
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path=path)
        log.record("action", detail="connecting with password: hunter2000")
        written = path.read_text(encoding="utf-8")
        assert "hunter2000" not in written
        assert "REDACTED" in written

    def test_truncates_huge_details(self) -> None:
        log = AuditLog(max_detail=50)
        log.record("action", detail="x" * 500)
        assert "more characters" in log.events[0].detail

    def test_bounded_buffer(self) -> None:
        log = AuditLog(max_events=10)
        for i in range(50):
            log.record("action", detail=str(i))
        assert len(log.events) == 10
        assert log.events[-1].detail == "49"

    def test_refusals_include_unavailable_tools(self) -> None:
        log = AuditLog()
        log.record("tool_call", outcome="refused", tool="run_query")
        log.record("tool_call", outcome="unknown_tool", tool="write_file")
        log.record("tool_call", outcome="ok", tool="read_file")
        assert {e.tool for e in log.refusals} == {"run_query", "write_file"}

    def test_a_broken_sink_does_not_stop_the_run(self) -> None:
        def explode(event: object) -> None:
            raise RuntimeError("sink is down")

        log = AuditLog(sink=explode)
        log.record("action", detail="still recorded")
        assert log.events[0].detail == "still recorded"

    def test_jsonl_round_trips(self) -> None:
        import json

        log = AuditLog()
        log.record("action", detail="one", tier="read")
        parsed = [json.loads(line) for line in log.to_jsonl().splitlines()]
        assert parsed[0]["kind"] == "action"
        assert parsed[0]["tier"] == "read"


class TestManifest:
    def test_summary_names_what_was_permitted(self) -> None:
        manifest = RunManifest(
            domain="code",
            model="claude-opus-5",
            policy="read-only",
            tools=["read_file", "write_file"],
            destructive_tools=["write_file"],
            system_prompt_sha256="a" * 64,
        )
        summary = manifest.summary()
        assert "code" in summary
        assert "2 (1 destructive)" in summary

    def test_flags_enabled_plugins(self) -> None:
        assert "ENABLED" in RunManifest(plugins_allowed=True).summary()

    def test_lists_mcp_servers(self) -> None:
        assert "github" in RunManifest(mcp_servers=["github"]).summary()
