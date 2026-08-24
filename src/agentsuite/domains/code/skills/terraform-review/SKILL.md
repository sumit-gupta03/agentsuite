---
name: terraform-review
description: >-
  Use when writing, reviewing or applying Terraform. Covers reading a plan
  safely, the changes that silently destroy and recreate resources, state
  handling, and module structure. Read before proposing any apply.
requires: [terraform]
---

# Terraform

Terraform's failure mode is not an error. It is a plan that says `1 to change`
when it means "delete the database and make a new empty one".

## Read the plan. Always. In full.

`terraform_plan` first, every time. Then find this line:

```
Plan: 3 to add, 1 to change, 2 to destroy.
```

**Any non-zero destroy count stops everything** until you have named each
resource being destroyed and confirmed it is intended. Report them explicitly —
never summarise a destroy as "some changes".

In the plan body, these markers matter:

| Marker | Meaning |
|---|---|
| `+` | create |
| `~` | update in place — usually safe |
| `-/+` | **destroy and recreate** — the dangerous one |
| `+/-` | create then destroy (`create_before_destroy`) |
| `-` | destroy |
| `# forces replacement` | the attribute causing a `-/+` |

`-/+` on a database, a disk, a stateful set or anything holding data means data
loss. It is trivially easy to trigger by accident.

## What forces replacement

Changing any of these on an existing resource destroys and recreates it:

- `name` on most resources — including a rename you thought was cosmetic
- `availability_zone`, `subnet_id`, region
- `engine_version` on some databases (major versions especially)
- disk `type`, sometimes `size` when shrinking
- anything the provider documents as "Forces new resource"

`terraform plan` tells you which attribute is responsible — it prints
`# forces replacement` next to it. Read that line before assuming a change is
in-place.

## `count` versus `for_each`

The most common self-inflicted outage in Terraform.

With `count`, resources are addressed by **index**. Removing the middle element
of a list shifts every subsequent resource's address, and Terraform destroys and
recreates all of them:

```hcl
# Dangerous with a changing list
resource "aws_instance" "web" {
  count = length(var.names)
  tags  = { Name = var.names[count.index] }
}
```

With `for_each`, resources are addressed by **key**. Removing one element affects
only that resource:

```hcl
resource "aws_instance" "web" {
  for_each = toset(var.names)
  tags     = { Name = each.key }
}
```

Use `count` only for a genuine on/off toggle (`count = var.enabled ? 1 : 0`).
Use `for_each` for anything that is a collection.

## State

- **State contains secrets in plaintext.** Database passwords, generated keys,
  certificate material. It is never committed, and it is not readable by tools
  here for that reason.
- **Remote backend with locking**, always. Two concurrent applies against local
  state corrupt it.
- **`terraform state` subcommands are destructive** and are gated accordingly.
  `state rm` forgets a resource without deleting it — leaving something running
  that nothing manages any more.
- **Never edit state by hand.** Use `moved` blocks to refactor addresses safely:
  ```hcl
  moved {
    from = aws_instance.web
    to   = aws_instance.frontend
  }
  ```
  This renames in state with no destroy. It is the correct tool for a refactor
  and almost nobody reaches for it.

## Writing modules

- **Variables carry `type`, `description`, and validation.** An untyped variable
  accepts anything and fails deep inside a provider.
  ```hcl
  variable "instance_count" {
    type        = number
    description = "Number of application instances."
    validation {
      condition     = var.instance_count > 0 && var.instance_count <= 20
      error_message = "instance_count must be between 1 and 20."
    }
  }
  ```
- **Mark secrets `sensitive = true`** so they are redacted from plan output.
- **Pin provider versions** with `~>`. An unpinned provider means a plan that
  differs from yesterday's for reasons unrelated to your change.
- **Prefer implicit dependencies** (referencing another resource's attribute) to
  `depends_on`. Implicit dependencies are precise; `depends_on` is a blunt
  ordering hint that hides the real relationship.
- **`lifecycle { prevent_destroy = true }`** on anything holding data. It turns an
  accidental destroy into an error instead of an outage.

## Before proposing an apply

State all of this, in the answer:

1. What the plan adds, changes and **destroys**, by name.
2. Whether anything is being replaced (`-/+`) and whether it holds data.
3. What the blast radius is if it goes wrong.
4. How to roll back — and if the answer is "restore from backup", whether that
   backup exists and has been restored from before.

Then use `terraform_apply`, which requires a write-enabled session and explicit
operator confirmation. `-auto-approve` is refused unconditionally: it exists to
skip precisely the gate that makes this safe.

## Common review findings

- Hardcoded account ids, regions or ARNs that should be variables or data sources
- Security group rules open to `0.0.0.0/0` on anything other than 80/443
- Secrets in `.tfvars` committed to the repo, or in `default` values
- No `prevent_destroy` on databases or storage buckets holding data
- `count` where `for_each` belongs
- Unpinned module or provider versions
- Missing tags — usually a compliance requirement, and always a cost-attribution one
