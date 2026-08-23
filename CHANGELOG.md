# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `Config.max_tokens` no longer carries an Anthropic-shaped default of 16,000.
  It defaults to `None`, meaning "whatever this backend's default is" -- Bedrock
  models cap far lower (Nova Pro at 10k), so a shared default failed every
  Bedrock session on the first call.
- A missing AWS region is reported immediately and clearly, instead of surfacing
  as botocore's `NoRegionError` six frames deep naming neither Bedrock nor this
  library.
- `agent.auto(model="bedrock:...")` now resolves the spec for routing too.
  Previously the string was discarded and classification silently fell back to
  the default provider, which usually has no credentials.
- CI: the `dev` extra now includes `boto3` and `openai`, so the suite exercises
  every backend it constructs rather than passing only on a machine that happens
  to have them installed.

### Added

- `scripts/live_check.py` -- end-to-end verification against a real endpoint,
  real files and a real database. Seven checks covering the warehouse and code
  domains, reconciliation, routing, governance, and injection containment.


## [0.2.0]

First public release.

### Added

- **Core** (`agentkart.core`) — agent loop, skill loader with seven-tier precedence,
  tool registry, `Model` protocol, permission layer, audit log, MCP client.
  Domain-agnostic; adding a domain does not change it.
- **Data engineering domain** — SQL policy, five warehouse adapters (SQLite,
  DuckDB, Postgres, Snowflake, BigQuery), query and profiling tools,
  reconciliation tools, dbt tools.
- **Code domain** — workspace-sandboxed file tools, allowlisted command
  execution, lint/typecheck/coverage, Terraform tools.
- **Nineteen presets** across the two domains, including `snowflake`, `dbt`,
  `sql`, `reconciliation`, `pyspark`, `pytorch`, `rag`, `testing`, `terraform`.
- **Twenty bundled skills**, overridable by name from a project or user directory.
- **Routing** (`agent.auto()`) — plain-English requests dispatched to the right
  preset. Capability-neutral by design: the prompt selects the specialism and
  never the permissions.
- **Governance** — per-session run manifest, append-only JSONL audit trail,
  secret redaction on write.
- **Prompt-injection defences** — per-run fencing nonce, protocol-mimicry
  neutralisation, decoration-robust detection, and the architectural guarantee
  that a compromised model cannot exceed session permissions.
- **MCP connectivity** — namespaced third-party tools, operator-assigned trust
  tiers, results fenced and scanned.

### Known limitations

- Only the Bedrock backend has been exercised against a live endpoint. The
  Anthropic and OpenAI encodings are unit-tested; their transport is not.
- Snowflake, BigQuery and Postgres adapters have not been run against real
  instances.
- The MCP client's transport is untested against a live server; everything that
  decides whether an MCP call is permitted is covered.
- Claude is the only implemented `Model` backend.

[Unreleased]: https://github.com/sumit-gupta03/agentkart/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sumit-gupta03/agentkart/releases/tag/v0.2.0
