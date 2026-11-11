# ccp

A teaching-scale but faithful build of an AI-native compliance automation platform
(the DSALTA, Vanta, Drata shape) on production technology: connectors land immutable raw
events, normalization and identity resolution produce canonical entities, deterministic
and model-backed evaluators produce verdicts with verified citations, a bitemporal state
table answers "what is true" and "what did we believe", and three agents (Investigator,
Policy Agent, Copilot) share one read-only tool surface and one human approval stage.

The design is `spec.md` (v1.3, seventeen layers, seventeen build phases). The review that
shaped it is `docs/eng-review-2026-09-11.md`; deferred work is `TODOS.md`. Every phase
appended its assumptions to `spec.md` section 22 (A-1 to A-92); read those when a design
choice looks surprising, because they say why.

## Quickstart

```
cp .env.example .env
docker compose up -d                 # Postgres 17 with pgvector, nothing else
uv sync --group dev                  # provisions Python 3.12 and the virtualenv
uv run ccp db migrate                # schema, RLS policies, the app role, checkpoint tables
uv run ccp db check
uv run ccp knowledge load            # SOC 2 and ISO 27001 requirements, controls, tests
uv run ccp run --tenant acme         # ingest, normalize, resolve, index, evaluate, write state
uv run ccp report --tenant acme      # the framework rollup with blocking tests
uv run ccp evals run                 # the golden suite; keyed cases skip without keys
uv run pytest                        # database tests skip when Postgres is unreachable
```

Two fixture tenants ship in `fixtures/` (acme and globex). Nothing here needs a key: the
deterministic tier, ingestion, identity resolution, state, remediation dry runs, and every
stubbed agent path run offline. Set `ANTHROPIC_API_KEY` for tiers 2 and 3, the agents, the
Copilot, the judge, and the adversarial suite; `VOYAGE_API_KEY` (or `OPENAI_API_KEY`) for
hosted embeddings and reranking (a local hashing embedder and a lexical reranker stand in
otherwise; Voyage also needs `VOYAGE_TRAINING_OPT_OUT=true`, set after opting out of
training in the Voyage organization settings); `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for traces.

## What the commands are

| Area | Commands |
|---|---|
| Ingest and normalize | `ccp ingest`, `ccp normalize`, `ccp identity resolve` and `review`, `ccp worker` |
| Evaluate | `ccp evaluate run` and `clock`, `ccp state current`, `history`, `asof`, `ccp report` |
| Retrieval | `ccp index rebuild`, `refresh`, `stats`, `distractors`, `calibrate`, `search` |
| Agents | `ccp agent pending` and `resume`, `ccp investigate run`, `ccp policy gap` and `draft`, `ccp copilot --tenant acme [--ask "..."]` |
| Human decisions | `ccp approvals list`, `show`, `approve`, `reject`, `resume`, `audit`; `ccp remediate propose`, `show`, `execute`, `rollback`, `audit` (a rollback is a proposal of its own, with its own approval) |
| Evals | `ccp evals run [--suite all]`, `calibrate`, `regress`, `capture <trace_id>`, `threshold` |
| Replay | `ccp replay <id> [--execute]` for a verdict, investigation, gap report, or copilot answer |

Every command takes `--tenant` (or `CCP_TENANT`). A decision proves who makes it with a
sign-in token (`--token`, or `CCP_TOKEN`), verified on the server, from a member of the
tenant holding the role it needs (A-129, A-130). Offline, `ccp auth dev-token` mints one
from a local dev issuer, and `ccp members grant` gives the role.

## How a verdict is made, in one paragraph

An event lands in the raw zone exactly once (deduplicated by content, ordered by source
time), is mapped to canonical entities, and the impact step enqueues the tests it touches.
Tier 1 tests decide from entities alone. Tier 2 tests retrieve evidence chunks (hybrid
lexical and vector search, reciprocal-rank fusion, a reranker), wrap them in a
nonce-fenced block that the prompt declares to be data, ask the model for a fixed schema
of fields each with a verbatim citation, verify every citation against the chunk text,
apply a deterministic rule to the verified fields, and compute confidence as a product of
coverage, citation rate, and retrieval quality, with forcing rules (no evidence, an
injection marker, a provider outage) that cap it. Tier 3 tests run the same steps as a
LangGraph workflow with a human gate. The verdict row stores the prompt version, the chunk
ids, the fence nonce, and the request and response as sent, so `ccp replay` can rebuild
the request and show an empty diff.

## Security guardrails

`spec.md` Layer 13 tables twenty-one guardrails, each with the test that proves it under
`tests/security/` (and `tests/unit/test_layering.py`, `tests/component/test_budgets.py`,
`tests/component/test_pdf_limits.py`, `evals/adversarial/`). The load-bearing ones:

- Tenant isolation is row-level security on every tenant table (forced, with an app role
  that cannot bypass it) behind a `ScopedDB` that sets the tenant per transaction; the
  agents' tools are bound to that closure and take no tenant argument; checkpoint tables
  are under the same policy; thread ids name their tenant.
- Retrieved text is data: one framing sentence in every prompt, a per-call random fence,
  chunk rejection on fence collision, an injection-marker scan that caps confidence.
- Citations are verified after generation; a failed citation is dropped and a verdict left
  without one becomes `needs_review`.
- Secrets are redacted before text reaches a model, through the fence and through tool
  results alike, and before it reaches the step log or a trace.
- No graph has a write path. Remediation needs five gates (an environment flag, a `--live`
  flag, an allowlist, a typed confirmation, and an approved item whose payload hash still
  matches), then the single-use grant that approval minted and the precondition the
  proposal was made under, checked again just before it acts; every step lands in an
  append-only journal, and a proposal is never edited (A-131). The Copilot's "fix this"
  ends in an open approval, never a change.

## Evaluation

`evals/golden/` holds exact-graded cases per target; `evals/rubrics/` the judge rubrics
with a second wording each; `evals/labeled/` twenty human-labelled cases for judge
calibration; `evals/adversarial/` the injection suite with an attack-success-rate per
class. `ccp evals run` prints four level rows (unit, component, workflow, e2e), the
reliability curve and expected calibration error, latency percentiles against
`config/slos.yaml`, and cost; `ccp evals regress` fails on a pass-to-fail flip, an
attack-success-rate increase, or a p95 breach, and a run that fails is written under its
own id but never becomes `latest.json`, so the next run is still measured against the
last good baseline; `ccp evals capture <trace_id>` turns any run into a golden case.
The nightly keyed job regresses against the last good baseline with `--strict`, where a
case that first runs and fails, or a new case that fails, also fails the job.
`ccp evals threshold` reads the human-interrupt threshold off the reliability curve, never
below 0.5, and `--apply` refuses to lower the gate unless `--allow-lower` is passed.
`evals/labeled/README.md` says how to read a kappa at n = 20.

## Honest limits

- Two fixture tenants, a mock HRIS, a GitHub fixture with an optional live pull. The manual
  upload path is the only way a customer document enters.
- Keyless runs prove the mechanisms with stub models that read the same fenced prompts a
  model would; they do not prove that a live model behaves. The keyed golden, judged, and
  adversarial cases exist for that and run nightly, not on every push.
- The calibration set is twenty cases labelled by the same person who wrote them, with no
  second annotator yet, so the kappa intervals are wide and the purpose is to catch a
  judge that drifts lenient on the injection criterion, not to certify the judge.
- Replay rebuilds a request exactly from its references (the request diff is asserted
  empty); re-execution reproduces the response only approximately, because models are not
  deterministic. What is guaranteed is that the stored response is the one the verdict was
  built from.
- The latency SLOs are provisional laptop numbers; the first case of a target also pays
  for preparing its tenant.
- Sign-in is verified against a real identity provider only once one is configured
  (`CCP_AUTH_MODE=oidc`); until then the local dev issuer stands in. The Copilot has no
  cross-session memory.
- Cost figures use the published list prices on the date in
  `ccp/observability/cost.py` and mis-state spend if those change; they never change a
  verdict.

## Data processing

When keys are set, evidence text (including the HR fixture data), prompts, and model
outputs leave the machine for these processors: Anthropic (generation and the judge),
Voyage AI or OpenAI (embeddings and reranking), and Langfuse Cloud (traces: step inputs,
outputs, and metadata, after secret redaction). The raw zone, the database, and the step
log stay local. Nothing leaves when no keys are set. Secrets matching the pattern set in
`ccp/security/secrets_patterns.py` are redacted before any text reaches a model, the step
log, or a trace (the Langfuse client is built with the same patterns as its mask, so every
payload it serialises passes through them too); the raw zone keeps the original because it
is an immutable audit record.

## Layout

See `spec.md` section 2. The import-linter contract in `pyproject.toml` and
`tests/unit/test_layering.py` enforce the layer order: `cli` and `evals` on top, then
`replay`, `copilot`, `investigation`, `policy`, `remediation`, the approval actions, the
compliance state, the agent runtime, the evaluators, retrieval, the model seam, knowledge,
identity, normalization, ingestion, connectors, the approval store, storage, security,
observability, tenancy, config, and types at the bottom.
