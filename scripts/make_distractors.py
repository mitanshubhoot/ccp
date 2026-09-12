"""Install generated distractor chunks for a tenant (spec.md D25).

    uv run python scripts/make_distractors.py --tenant acme --count 300

The same generator backs `ccp index distractors`; this script exists so the eval runner and
CI can call it without the CLI. Distractors are deterministic per tenant and are never
retrieved by an evaluator.
"""

from __future__ import annotations

import argparse

from ccp.config import Settings
from ccp.retrieval import distractors
from ccp.retrieval.embeddings import build_embedding_client
from ccp.storage.db import app_pool
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--clear", action="store_true", help="remove existing distractors first")
    args = parser.parse_args()
    settings = Settings()
    pool = app_pool(settings)
    try:
        db = ScopedDB(pool, TenantContext.for_id(settings, args.tenant))
        if args.clear:
            print(f"cleared {distractors.clear(db)} distractor chunks")
        total = distractors.install(db, build_embedding_client(settings), args.count)
        print(f"{args.tenant}: {total} distractor chunks present")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
