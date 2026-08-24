# Security

This library gives a language model the ability to read files, run commands and
query databases. The threat model is therefore not incidental to it — it is the
design.

## What this library promises

**It does not promise that a language model will never be persuaded by injected
text.** No prompt technique achieves that, and any library claiming it is
mistaken.

**It promises, and tests, that being fooled cannot _escalate_.** The model's
judgement never authorises anything. Every action is classified by
`agentsuite.core.policy` on what the action *is* — not on what the model believes
about it. A fully persuaded model still cannot:

- write outside the project root, or read a deny-listed credential or state file
- run a command that is not on the allowlist, or reach a shell
- take a destructive action without the operator's confirmation callback saying yes
- obtain permissions the session was not given — including through routing, which
  selects a specialism and never a permission

**What it does _not_ promise:** that a session you granted write access will never
write. It was given that access and asked to act. A session that may write may
write; the guarantee is about the boundary, not about the model doing its granted
job well. This distinction is load-bearing — grant the least access that works,
and supply a confirmation handler you trust.

Verified live, not just with stubs: `python scripts/live_check.py` runs a poisoned
file past a real model in both a read-only and a write-enabled session, and
asserts each half of the above.

## Defences

| Layer | What it does |
|---|---|
| Skills cannot act | Only registered tools can. Tools enforce policy regardless of what any skill or document says. This is load-bearing; the rest is depth |
| Permission tiers | `read` / `write` / `destructive`, fail-closed on anything unclassifiable |
| Confirmation gate | Defaults to **refusing**. A session with no handler cannot do anything destructive |
| Workspace boundary | Paths checked for containment *after* symlink resolution. Credentials, keys and Terraform state are deny-listed |
| No shell | Commands are argv lists with `shell=False`, from an allowlist. Shell metacharacters are inert rather than filtered |
| Content fencing | Untrusted output wrapped with a per-run nonce a payload cannot guess; protocol mimicry neutralised; detection is robust to line decoration |
| Read before overwrite | Replacing an unread file is destructive and needs confirmation |
| Routing is capability-neutral | A prompt selects the specialism, never the permissions |
| MCP trust is operator-assigned | A server's claim about its own tools is not what decides whether they run |
| Audit | Every action, refusal and injection attempt recorded, with secrets redacted on write |

## Defaults you should not change without thinking

- **`write=False`.** Enable it only with a confirmation handler you trust.
- **`allow_plugins=False`.** Third-party skill packs are instructions the agent
  follows; an installed package should not inject them silently.
- **Deny lists extend, never replace.** A project cannot widen its own boundary.
- **`-auto-approve` is refused unconditionally** for Terraform. It exists to skip
  exactly the gate that makes this safe.

## Using it safely

1. Start read-only. Move to `write=True` once you have seen what it does.
2. Supply a real confirmation handler. The default refuses everything, which is
   safe but means destructive work simply does not happen.
3. Point it at a git repository, so writes are reversible.
4. Set `audit_path` and keep the log. It is the record of what happened.
5. Review `agent.injection_attempts` and `agent.refusals` after any run over
   content you did not author.
6. Give the credentials the *least* privilege that works. This library governs
   what the agent asks for; it cannot govern what your database grants.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub's **Report a vulnerability** button, on the
Security tab of https://github.com/sumit-gupta03/agentsuite. Include a
description, the version, and a reproduction if you have one.

That channel is private and does not require publishing an email address.

Expect an acknowledgement within a few days and an assessment within two weeks.
Fixes for confirmed issues ship as a patch release, with credit unless you would
rather not be named.

### In scope

- Any path by which a tool acts outside the permissions the session was given
- Escaping the workspace boundary, or reading a deny-listed file
- Executing a command not on the allowlist
- A destructive action reaching the warehouse or filesystem without confirmation
- Secrets reaching the audit log unredacted
- An MCP server obtaining a tier it was not assigned

### Out of scope

- A model being persuaded by injected text *without* exceeding its permissions.
  That is expected, is surfaced in the audit log, and is what the architecture is
  built to contain.
- A write-enabled session writing inside its own project because a prompt talked
  it into doing so. That is the access you granted being used. Grant less, or gate
  it with a confirmation handler.
- Anything a caller enabled deliberately (`write=True` plus a handler that
  approves, `allow_destructive=True`, an allowlisted command doing what it does).
- Third-party skill packs or MCP servers behaving badly after explicit opt-in.
  Trusting them is a decision the operator makes.

## Supported versions

Pre-1.0, only the latest minor version receives fixes.
