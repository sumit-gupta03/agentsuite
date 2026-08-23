# Security

This library gives a language model the ability to read files, run commands and
query databases. The threat model is therefore not incidental to it — it is the
design.

## What this library promises

**It does not promise that a language model will never be persuaded by injected
text.** No prompt technique achieves that, and any library claiming it is
mistaken.

**It promises, and tests, that being fooled does not grant privilege.** The
model's judgement never authorises anything. Every action is classified by
`agentkart.core.policy` on what the action *is* — not on what the model believes
about it — so a fully compromised model produces refusals and audit entries
rather than damage.

`examples/05_governance.py` demonstrates this against a scripted model that has
read an injected instruction and is obeying it verbatim, with writes enabled.

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
Security tab of https://github.com/sumit-gupta03/agentkart. Include a
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
- Anything a caller enabled deliberately (`write=True` plus a handler that
  approves, `allow_destructive=True`, an allowlisted command doing what it does).
- Third-party skill packs or MCP servers behaving badly after explicit opt-in.
  Trusting them is a decision the operator makes.

## Supported versions

Pre-1.0, only the latest minor version receives fixes.
