"""The permission layer, demonstrated without a model.

Two halves, shown separately:

* :mod:`agentsuite.core.policy` owns the *decision* -- tiers, the write gate, the
  confirmation gate, fail-closed. Every domain inherits it unchanged.
* the domain owns the *classifier* -- here, what kind of SQL statement this is.

Adding a domain means writing the second half only.

    python examples/02_permissions.py
"""

from __future__ import annotations

from agentsuite.core.policy import Action, Policy
from agentsuite.domains.dataengineering.policy import SqlPolicy

STATEMENTS = [
    "SELECT * FROM orders",
    "SELECT COUNT(*) FROM orders",
    "SELECT * FROM orders LIMIT 5",
    "INSERT INTO orders VALUES (1, 2, 3)",
    "UPDATE orders SET status = 'x' WHERE order_id = 1",
    "UPDATE orders SET status = 'x'",
    "DELETE FROM orders WHERE order_id = 1",
    "DELETE FROM orders",
    "DROP TABLE orders",
    "TRUNCATE TABLE orders",
    "SELECT 1; DROP TABLE orders",
    "$$$ not valid sql $$$",
]

POLICIES = {
    "read-only (default)": SqlPolicy(),
    "write enabled": SqlPolicy(write=True),
    "write + destructive confirmed": SqlPolicy(write=True, allow_destructive=True),
}


class FileWritePolicy(Policy):
    """A sketch of what a second domain's classifier looks like.

    Fifteen lines. That is the entire cost of giving a new domain the same
    tier machinery, write gate, confirmation gate and fail-closed behaviour.
    """

    def classify(self, request: str, **context: object) -> list[Action]:
        if request.startswith("read "):
            return [Action("file", request, "read", "READ")]
        if request.startswith("write "):
            if ".." in request:
                return [
                    Action("file", request, "destructive", "WRITE", "path escapes the workspace")
                ]
            return [Action("file", request, "write", "WRITE")]
        if request.startswith("delete "):
            return [Action("file", request, "destructive", "DELETE", "removes a file")]
        return [Action("file", request, "destructive", "UNKNOWN", "unrecognised operation")]


def show(title: str, policy: Policy, requests: list[str]) -> None:
    print("=" * 78)
    print(f"POLICY: {title}")
    print("=" * 78)
    for request in requests:
        verdict = policy.check(request, dialect="postgres")
        if verdict.allowed:
            note = f"tier={verdict.tier}"
            if verdict.rewritten:
                note += f" | rewritten: {verdict.rewritten}"
            print(f"  ALLOW   {request}")
        else:
            note = verdict.reason
            print(f"  REFUSE  {request}")
        print(f"          {note}")
    print()


def main() -> None:
    for title, policy in POLICIES.items():
        show(title, policy, STATEMENTS)

    show(
        "a different domain, same machinery (read-only)",
        FileWritePolicy(),
        ["read src/app.py", "write src/app.py", "write ../../etc/passwd", "delete src/app.py"],
    )

    print("=" * 78)
    print("Three things worth noticing:")
    print("=" * 78)
    print(
        "1. Unparseable input is classified DESTRUCTIVE, never READ. The layer\n"
        "   fails closed -- what it cannot understand is never assumed safe.\n"
        "2. 'SELECT 1; DROP TABLE orders' is refused as a batch. The highest tier\n"
        "   present governs, so a read cannot smuggle a drop past the classifier.\n"
        "3. FileWritePolicy above is ~15 lines and inherits every rule. That is\n"
        "   what the shared core buys: the next domain does not re-litigate\n"
        "   permissions, it just says what its actions are."
    )


if __name__ == "__main__":
    main()
