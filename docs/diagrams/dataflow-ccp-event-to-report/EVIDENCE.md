# Evidence: ccp Event Dataflow, Landing to Report

Diagram: `ccp-event-to-report.html` (source `candidate.json`).
Code revision: `3386899764f95dfc77030e010c829838198318d1` on `main`. The repository has no git remote, so the
diagram carries no source links; this file holds the evidence instead. Paths are relative to the repository
root and line numbers are inclusive.

Current receipts: `review-3/` (the root and `review-2/` receipts belong to earlier runs, before card fixes).

The diagram follows one connector event on the per-event path (the impact step). It also draws the
full-run enqueue that `ccp run` uses instead, because that is the path the README's quickstart takes.

## Nodes

| Node | What the code shows | Evidence |
|---|---|---|
| Connector pull | A connector yields `SourceEvent`s (tenant, connector, source id, UTC timestamp, payload) | `ccp/connectors/base.py:26-54`, `ccp/connectors/registry.py:28-53` |
| Landing, "raw_events dedup" | File first, then `INSERT INTO raw_events ... ON CONFLICT DO NOTHING`; only a new row appends to `event_log` | `ccp/ingest/landing.py:39-79` |
| Raw zone file | Written to a temp name and renamed; a second copy goes under `dups/` | `ccp/ingest/raw_zone.py:42-72` |
| Normalize, "pure mapper" | One mapper per connector, chosen by `event.connector` | `ccp/normalize/consumer.py:32-41`, `ccp/normalize/consumer.py:53-88` |
| Entity rows | Append-only observation rows keyed on `(entity_id, source_event_id)` | `ccp/normalize/repository.py:1-8`, `ccp/normalize/repository.py:50-142` |
| Full-run enqueue | One request per (test, subject), trigger = newest observation, reason `full_run` | `ccp/evaluators/runner.py:190-216`, `ccp/compliance_state/run.py:68` |
| Impact step | Maps an entity change to (test, subject) pairs; coalesces pending requests for 60 s | `ccp/evaluators/impact.py:1-26`, `ccp/evaluators/impact.py:81-113`, `ccp/evaluators/impact.py:149-249` |
| Evidence chunks | Chunks with a vector and a full-text index; hybrid search with RRF | `ccp/retrieval/index.py:160-186`, `ccp/retrieval/hybrid.py:1-12`, `ccp/retrieval/hybrid.py:113-149` |
| Evaluator, "budget gate" | Daily budget check, then route by tier; AI tiers park when no model key is set | `ccp/evaluators/runner.py:63-90`, `ccp/evaluators/runner.py:151-185`, `ccp/evaluators/budget_gate.py:47-55` |
| Knowledge graph | Frameworks, controls and tests loaded from YAML under `ccp/knowledge/` | `ccp/knowledge/graph.py:163-174`, `ccp/evaluators/runner.py:43-45` |
| Verdict row | The shared `Verdict` schema, persisted insert-or-ignore on `evaluation_key` | `ccp/evaluators/verdict.py:35-64`, `ccp/evaluators/verdict.py:102-138`, `ccp/storage/migrations/0005_state.sql:66-68` |
| State writer, "reason + supersede" | Picks the reason (`evaluation`, `late_event`, `stale_evidence`, `human_override`), records against the chain head | `ccp/compliance_state/writer.py:1-13`, `ccp/compliance_state/writer.py:44-78` |
| compliance_state | Bitemporal row under a per-subject advisory lock and a per-tenant sequence | `ccp/storage/bitemporal.py:21-25`, `ccp/storage/bitemporal.py:106-155` |
| ccp report, "worst state wins" | Requirement state = worst of its tests' current rows; empty tenant says so | `ccp/knowledge/rollup.py:1-10`, `ccp/knowledge/rollup.py:63-106`, `ccp/cli.py:430-446` |

## Flows

| Flow | Evidence |
|---|---|
| Connector pull → Landing, "SourceEvent" | `ccp/ingest/landing.py:92-100` |
| Landing → Raw zone file, "file + sha256" | `ccp/ingest/landing.py:50`, `ccp/ingest/raw_zone.py:62-72` |
| Landing → Normalize, "raw.* topic row" | `ccp/ingest/landing.py:70-77`, `ccp/normalize/consumer.py:47-50`, `ccp/ingest/consumers.py:61-72` (batch sorted by `source_ts`) |
| Raw zone file → Normalize, "payload by ref" | `ccp/normalize/consumer.py:53-66`, `ccp/ingest/raw_zone.py:74-78` |
| Normalize → Entity rows, "observation rows" | `ccp/normalize/consumer.py:73-77` |
| Normalize → Impact step, "entities topic" | `ccp/normalize/consumer.py:78-85`, `ccp/evaluators/impact.py:221-242` |
| Entity rows → Impact step, "current views" | `ccp/evaluators/impact.py:116-209` |
| Impact step → Evaluator, "evaluations topic" | `ccp/evaluators/impact.py:105-112`, `ccp/evaluators/runner.py:151-167`, `ccp/ingest/event_log.py:85-101` (claim with `SKIP LOCKED`) |
| Full-run enqueue → Evaluator, "every pair" | `ccp/evaluators/runner.py:198-216` (appends to the same `evaluations` topic) |
| Entity rows → Evaluator, "entity context" | `ccp/evaluators/runner.py:71-74`, `ccp/evaluators/context.py:108-129` |
| Evidence chunks → Evaluator, "fenced, redacted" | `ccp/evaluators/tier2.py:155-229`, `ccp/security/prompt_guard.py:105-118` |
| Knowledge graph → Evaluator, "test spec, tier" | `ccp/evaluators/runner.py:71`, `ccp/evaluators/runner.py:75-77` |
| Evaluator → Verdict row, "persist, keyed" | `ccp/evaluators/runner.py:93-96`, `ccp/evaluators/verdict.py:72-90` |
| Verdict row → State writer, "verdicts topic" | `ccp/evaluators/runner.py:97-105`, `ccp/compliance_state/writer.py:90-104` (loads the verdict by id) |
| State writer → compliance_state, "append, supersede" | `ccp/compliance_state/writer.py:59-78`, `ccp/storage/bitemporal.py:123-155` |
| compliance_state → ccp report, "chain heads" | `ccp/storage/bitemporal.py:203-210`, `ccp/cli.py:441-446` |
| Knowledge graph → ccp report, "requirement map" | `ccp/cli.py:438-446`, `ccp/knowledge/rollup.py:63-70` |

## Card claims

| Claim | Evidence |
|---|---|
| File first, then `raw_events` and `event_log` in one short transaction; a duplicate stops as a dedup hit | `ccp/ingest/landing.py:1-9`, `ccp/ingest/landing.py:50-69` |
| Idempotent keys: entity rows, verdicts, state rows | `ccp/normalize/repository.py:3-5`, `ccp/evaluators/verdict.py:1-5`, `ccp/compliance_state/writer.py:11`, `ccp/compliance_state/writer.py:54-64` |
| Impact step callers | `ccp/cli.py:194-198`, `ccp/evaluators/impact.py:252`, `ccp/remediation/reevaluate.py:41-43`; `reevaluate()` is called only at `tests/e2e/test_remediation_loop.py:86` |
| `ccp run` enqueues every pair instead | `ccp/compliance_state/run.py:62-69`, `ccp/evaluators/runner.py:198-200` |
| Clock producer enqueues overdue or stale subjects | `ccp/evaluators/clock.py:1-7`, `ccp/evaluators/clock.py:33-66` |
| Tier 1 is rule code; tier 2 extracts, verifies quotes, then a rule decides | `ccp/evaluators/deterministic/__init__.py:1-3`, `ccp/evaluators/tier2.py:1-9`, `ccp/evaluators/tier2.py:297-319` |
| Tier 3 runs as a graph and can pause as `tier3_interrupt` | `ccp/agent/graphs/tier3.py:1-11`, `ccp/agent/tier3.py:192-209` |
| Identity links are announced on the entities topic | `ccp/identity/resolver.py:92-126` |
| The Investigator consumes `verdicts`, fail and needs_review only | `ccp/investigation/runner.py:41`, `ccp/investigation/runner.py:208-239` |

## Where the code differs from spec.md and README.md

- README.md:54-55 says the impact step enqueues the tests an event touches. That is true on the per-event
  path drawn here, but `ccp run` (the quickstart command) does not run the impact step; it enqueues every
  pair (`ccp/compliance_state/run.py:62-69`).
- README.md:25 runs `ccp report --tenant acme`; the command requires `--framework` (`ccp/cli.py:432`).
- spec.md:1510 places the rollup in `ccp/compliance_state/rollup.py`; it is `ccp/knowledge/rollup.py`.

## Left out of this diagram

- The model call itself (tier 2 `extract` through `LLMClient` to Anthropic, `ccp/evaluators/tier2.py:262-295`)
  is folded into the Evaluator node; the architecture diagram shows the provider.
- Identity resolution, the Investigator, the clock producer, parking and dead-lettering
  (`ccp/ingest/consumers.py:98-136`), the step log, and the index refresh that writes the chunks.
