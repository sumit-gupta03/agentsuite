"""A real LLM-driven run against a real (if small) warehouse.

This is the example that shows the thing is actually AI-powered: Claude decides
which skills to load, which tools to call in what order, writes the SQL itself,
reads the results and re-plans. Nothing below tells it what to do -- the prompt
states a goal, and everything after that is the model's choice.

Needs credentials: either ANTHROPIC_API_KEY, or an OAuth profile from
`ant auth login`.

    python examples/03_live_run.py
"""

from __future__ import annotations

import os
import sys

import agentsuite as agent
from agentsuite.domains.dataengineering.warehouse.sqlite_adapter import SQLiteWarehouse

SEED = """
CREATE TABLE orders (
    order_id     INTEGER,
    customer_id  INTEGER,
    amount_cents INTEGER,
    status       TEXT,
    loaded_at    TEXT
);
INSERT INTO orders VALUES
    (1, 10, 1500, 'complete', '2026-08-01T00:00:00'),
    (2, 11, 2500, 'complete', '2026-08-01T00:00:00'),
    (3, 12, 3000, 'complete', '2026-08-02T00:00:00'),
    (3, 12, 3000, 'complete', '2026-08-02T00:00:00'),   -- duplicate key
    (4, 13,   -1, 'pending',  '2026-08-02T00:00:00'),   -- sentinel amount
    (5, 14,   -1, 'pending',  '2026-08-02T00:00:00'),
    (6, 15, 9900, 'complete', NULL),                    -- null watermark
    (7, 16,  500, 'refunded', '2026-08-03T00:00:00');
"""

PROMPT = """\
Profile the orders table. I am about to join it to a customers table on
customer_id and sum amount_cents for revenue reporting.

Tell me specifically what would break, and what I should do about it.
"""


def main() -> int:
    warehouse = SQLiteWarehouse(":memory:")
    warehouse.connection.executescript(SEED)

    # Read-only by default. The model cannot modify anything here however it is
    # prompted -- that is enforced below the model, in the policy layer.
    de = agent.dataengineering(warehouse=warehouse)

    print(repr(de))
    print(f"model: {de.model.model_id}\n")

    with de:
        try:
            result = de.run(
                PROMPT,
                on_tool=lambda call: print(
                    f"  -> {call.name}({call.input})", file=sys.stderr
                ),
            )
        except Exception as exc:  # noqa: BLE001 - this is a demo entry point
            print(f"\nRun failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print(
                    "\nANTHROPIC_API_KEY is not set. Set it, or run `ant auth login`.",
                    file=sys.stderr,
                )
            return 1

        print("\n" + "=" * 72)
        print(result.text)
        print("=" * 72)
        print(f"turns:       {result.turns}")
        print(f"tokens:      {result.usage.input_tokens + result.usage.output_tokens:,}")
        print(f"cache reads: {result.usage.cache_read_tokens:,}")
        print(f"skills used: {', '.join(de.skills_used) or '(none)'}")
        print("\nEverything it actually did:")
        for record in de.actions:
            print(f"  [{record.tier}] {record.detail}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
