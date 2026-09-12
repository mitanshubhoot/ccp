# ccp

Teaching-scale reference build of an AI-native compliance automation platform, on
production technology. The design is `spec.md` (v1.3); the review that shaped it is
`docs/eng-review-2026-09-11.md`; deferred work is `TODOS.md`.

## Quickstart (Phase 0)

```
cp .env.example .env
docker compose up -d            # Postgres 17 with pgvector, nothing else
uv sync --group dev             # provisions Python and the virtualenv
uv run ccp db migrate           # creates the schema and the RLS-bound app role
uv run ccp db check
uv run ccp tenants list
uv run pytest                   # db-marked tests skip when Postgres is unreachable
```

## Layout

See `spec.md` section 2. Packages are added phase by phase; the import-linter contract
in `pyproject.toml` and `tests/unit/test_layering.py` enforce the layer order as they
appear.

## Data processing note

Evidence text, prompts, and HR fixture data leave the laptop for the configured
processors: Anthropic (generation), Voyage AI or OpenAI (embeddings and reranking), and
Langfuse Cloud (traces) when keys are set. Nothing leaves when no keys are set.
