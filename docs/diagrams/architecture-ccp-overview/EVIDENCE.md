# Evidence: ccp Compliance Platform Architecture

Diagram: `ccp-overview.html` (source `candidate.json`).
Code revision: `3386899764f95dfc77030e010c829838198318d1` on `main`. The repository has no git remote, so the
diagram carries no source links; this file holds the evidence instead. Paths are relative to the repository
root and line numbers are inclusive. Only `.vscode/` and `docs/explainers/` were untracked when this was
drawn, so every cited file matches the commit.

Current receipts: `review-2/` (the files at the folder root are from the first run, before a card fix).

## Boxes

| Box | What the code shows | Evidence |
|---|---|---|
| Sources | Fixture folders per tenant, the uploads folder, and the GitHub API (live client only with `--source live` and `GITHUB_TOKEN`) | `ccp/connectors/registry.py:28-53`, `ccp/config.py:119-135` |
| Connectors | Three connectors (`github`, `hris`, `manual`), each built as fixture or live; each yields `SourceEvent`s | `ccp/connectors/registry.py:16-17`, `ccp/connectors/registry.py:28-53`, `ccp/connectors/base.py:26-54` |
| Landing | One short transaction per event; dedup through `raw_events` `ON CONFLICT DO NOTHING` | `ccp/ingest/landing.py:39-79`, `ccp/ingest/landing.py:82-103` |
| Normalize (+ identity resolver) | Ordered consumer per `raw.<connector>` topic running a pure mapper; the resolver blocks, gates, scores and routes person pairs | `ccp/normalize/consumer.py:32-41`, `ccp/normalize/consumer.py:44-88`, `ccp/identity/resolver.py:66-149` |
| Evaluators | Impact step (entities topic to evaluations topic), then the evaluations consumer routes by tier: tier 1 rule modules, tier 2 retrieve and extract, tier 3 graph | `ccp/evaluators/impact.py:149-249`, `ccp/evaluators/runner.py:63-90`, `ccp/evaluators/runner.py:109-185`, `ccp/evaluators/deterministic/__init__.py:17-36`, `ccp/evaluators/tier2.py:126-358`, `ccp/agent/tier3.py:143-220` |
| Evaluators tag "tier 2·3 → LLMClient" | Tier 2 calls `providers.llm.extract`; the client is built only when `ANTHROPIC_API_KEY` is set | `ccp/evaluators/tier2.py:278-280`, `ccp/llm/client.py:283-287` |
| State writer | Ordered consumer of the `verdicts` topic; one state row per verdict, reason chosen from the trigger | `ccp/compliance_state/writer.py:44-122` |
| Raw zone | `data/raw/{tenant}/{connector}/{date}/{id}.json`, written to a temp name and renamed; duplicates go under `dups/` | `ccp/ingest/raw_zone.py:1-9`, `ccp/ingest/raw_zone.py:52-72` |
| event_log | Postgres table used as the queue; seven declared topics | `ccp/storage/migrations/0001_schema.sql:36`, `ccp/ingest/event_log.py:40-101`, `ccp/ingest/topics.py:19-27` |
| Entity tables | Observation tables for six entities plus `resolution_decisions`, `person_links`, `review_queue` | `ccp/storage/migrations/0003_entities.sql:5-80`, `ccp/storage/migrations/0004_identity.sql:4-36`, `ccp/normalize/repository.py:32-39` |
| chunks | `chunks` with a `vector` column and a full-text index; hybrid search fuses both with RRF in one query | `ccp/storage/migrations/0008_chunks.sql:10-33`, `ccp/retrieval/hybrid.py:113-149` |
| verdicts | Insert-or-ignore on `evaluation_key`; the app role has `SELECT, INSERT` only | `ccp/evaluators/verdict.py:102-138`, `ccp/storage/migrations/0005_state.sql:6`, `ccp/storage/migrations/0005_state.sql:66-68` |
| compliance_state | Bitemporal rows with `supersedes_row_id`; no `UPDATE` in the module and none granted | `ccp/storage/migrations/0005_state.sql:44-68`, `ccp/storage/bitemporal.py:1-25`, `ccp/storage/bitemporal.py:106-155` |
| ccp report | Rolls chain heads up to a framework's requirements, worst state wins | `ccp/cli.py:430-446`, `ccp/knowledge/rollup.py:63-106` |
| Read-only tools | 13 tool specs and functions; tenant comes from the bound `ToolContext`, arguments forbid extra fields | `ccp/agent/tools.py:1-8`, `ccp/agent/tools.py:109-176`, `ccp/agent/tools.py:484-547` |
| Read-only tools tag "13 tools" and allowlists | Tier 3 gets 7, the Investigator 11, the Policy Agent 4, the Copilot all 13 | `ccp/agent/allowlists.py:9-30` |
| Agent core | Builds every graph, wraps nodes with retry and budget, runs the bounded tool loop (8 calls) | `ccp/agent/core.py:1-15`, `ccp/agent/core.py:146-160`, `ccp/agent/core.py:279-384` |
| Agent core tag "PostgresSaver" | Runners compile graphs with a `PostgresSaver` on a tenant-scoped pool | `ccp/investigation/runner.py:64-65`, `ccp/storage/checkpoints.py:72-85` |
| Investigator | Runner plus graph `load_context → gather → diagnose → verify_citations → bound_actions → finalize` | `ccp/investigation/runner.py:68-127`, `ccp/investigation/runner.py:208-248`, `ccp/agent/graphs/investigator.py:291-312` |
| Policy Agent | Gap or draft mode for one control, started from the CLI | `ccp/policy/runner.py:189-250`, `ccp/policy/cli.py:57-60`, `ccp/agent/graphs/policy.py:339-366` |
| Copilot | Terminal REPL session over the copilot graph; `confirm` opens approvals | `ccp/copilot/runner.py:92-157`, `ccp/copilot/runner.py:413-466`, `ccp/copilot/cli.py:73-115` |
| Operator | The CLI user; approvals record a typed reviewer name, no user identity | `ccp/approvals/cli.py:83-128`, `README.md:49-50` |
| Approval stage | Append-only `approvals` and `approval_events`; state is the latest event; four kinds | `ccp/approvals/store.py:16-18`, `ccp/approvals/store.py:69-115`, `ccp/storage/migrations/0011_remediation_approvals.sql:32-54` |
| Remediation | Dry run by default; live only when all five gates hold | `ccp/remediation/runner.py:101-202`, `ccp/remediation/gates.py:1-9`, `ccp/remediation/gates.py:53-84` |
| GitHub API | `DELETE /repos/{org}/{repo}/collaborators/{login}` through the live admin client | `ccp/remediation/github_admin.py:22-23`, `ccp/remediation/github_admin.py:46-66` |
| Anthropic API | The only `anthropic` import is in the client behind the `LLMClient` seam | `ccp/llm/anthropic_client.py:29`, `ccp/llm/anthropic_client.py:129-146`, `ccp/llm/client.py:272-287` |

## Arrows

| Arrow | Evidence |
|---|---|
| Sources → Connectors, "pull()" | `ccp/connectors/base.py:49-54`, `ccp/connectors/registry.py:28-53` |
| Connectors → Landing, "SourceEvent" | `ccp/ingest/landing.py:92-100` |
| Landing → Raw zone, "write once" | `ccp/ingest/landing.py:50`, `ccp/ingest/raw_zone.py:52-72` |
| Landing → event_log, "append raw.*" | `ccp/ingest/landing.py:51-77` |
| Landing → Normalize, "raw.* topic" | `ccp/normalize/consumer.py:44-66`, `ccp/ingest/consumers.py:61-72` |
| Normalize → Entity tables, "insert, link" | `ccp/normalize/consumer.py:75-77`, `ccp/normalize/repository.py:50-142`, `ccp/identity/resolver.py:74-114` |
| Normalize → Evaluators, "entities topic" | `ccp/normalize/consumer.py:78-85`, `ccp/identity/resolver.py:117-126`, `ccp/evaluators/impact.py:221-249` |
| Entity tables → chunks, "chunk + embed" (index refresh) | `ccp/retrieval/index.py:76-82`, `ccp/retrieval/index.py:139-186`, `ccp/compliance_state/run.py:67` |
| Evaluators → chunks, "retrieve" | `ccp/evaluators/tier2.py:155-185` |
| Evaluators → verdicts, "persist" | `ccp/evaluators/runner.py:93-96`, `ccp/evaluators/verdict.py:102-138` |
| Evaluators → State writer, "verdicts topic" | `ccp/evaluators/runner.py:97-105`, `ccp/compliance_state/writer.py:81-104` |
| State writer → compliance_state, "append row" | `ccp/compliance_state/writer.py:59-78`, `ccp/storage/bitemporal.py:123-155` |
| compliance_state → ccp report, "chain heads" | `ccp/cli.py:441-446`, `ccp/storage/bitemporal.py:203-210` |
| Read-only tools → Entity tables, "get_person" (also `get_access_grants`) | `ccp/agent/tools.py:314-335`, `ccp/agent/tools.py:392-410` |
| Read-only tools → chunks, "search" (`search_evidence`) | `ccp/agent/tools.py:225-236` |
| Read-only tools → verdicts, "get_verdict" | `ccp/agent/tools.py:352-371` |
| Read-only tools → compliance_state, "state rows" (`get_current_state`, `list_findings`) | `ccp/agent/tools.py:338-349`, `ccp/agent/tools.py:374-389` |
| event_log → Investigator, "verdicts topic: fail, needs_review" | `ccp/investigation/runner.py:41`, `ccp/investigation/runner.py:208-239` |
| Agent core → Read-only tools, "tool calls" | `ccp/agent/core.py:336-352` |
| Investigator → Agent core | `ccp/investigation/runner.py:54-65` |
| Policy Agent → Agent core | `ccp/policy/runner.py:78-96` |
| Copilot → Agent core | `ccp/copilot/runner.py:133-157` |
| Agent core → Anthropic API, "LLMClient" | `ccp/agent/core.py:237`, `ccp/agent/core.py:311-319`, `ccp/llm/client.py:283-287` |
| Investigator → Approval stage, "proposals" | `ccp/investigation/runner.py:153-156`, `ccp/investigation/runner.py:190-205`, `ccp/remediation/proposals.py:82-126`; evidence requests at `ccp/investigation/runner.py:175-186` |
| Policy Agent → Approval stage, "draft" | `ccp/policy/runner.py:217-236` |
| Copilot → Approval stage, "fix this" | `ccp/copilot/runner.py:413-466`, `ccp/copilot/cli.py:60-62` |
| Operator → Approval stage, "approve" | `ccp/approvals/cli.py:83-128`, `ccp/approvals/store.py:98-115` |
| Approval stage → Remediation, "approved" (gate 5) | `ccp/approvals/actions.py:55-57`, `ccp/remediation/runner.py:118`, `ccp/remediation/gates.py:77-84` |
| Remediation → GitHub API, "live only" | `ccp/remediation/runner.py:136-141`, `ccp/remediation/runner.py:183-202` |

## Card claims

| Claim | Evidence |
|---|---|
| Topic edges are `event_log` rows; ordered consumers keep a cursor; evaluations claimed with `SKIP LOCKED` | `ccp/ingest/event_log.py:58-101`, `ccp/ingest/consumers.py:45-72`, `ccp/ingest/consumers.py:91-96` |
| The Investigator is the second `verdicts` consumer and state never waits for it | `ccp/investigation/runner.py:1-6`, `ccp/ingest/consumers.py:54-56` (separate cursor per consumer name) |
| Graphs read, runners write | `spec.md:231-235`, `ccp/agent/tools.py:1-8`, `ccp/agent/graphs/investigator.py:12` |
| Five live gates | `ccp/remediation/gates.py:1-9`, `ccp/remediation/gates.py:53-84` |
| Four approval kinds; tier 3 pauses as `tier3_interrupt` | `ccp/approvals/store.py:16-18`, `ccp/agent/tier3.py:192-209` |
| `reevaluate()` exists but only the e2e test calls it | `ccp/remediation/reevaluate.py:35-54`, `tests/e2e/test_remediation_loop.py:86`, `ccp/remediation/cli.py:1-2` |

## Where the code differs from spec.md and README.md

The diagram draws the code. These differences were found while tracing it:

1. `ccp run` never runs the impact step. It enqueues every (test, subject) pair through `enqueue_all`
   (`ccp/compliance_state/run.py:62-69`, `ccp/evaluators/runner.py:190-216`). The impact step runs from
   `ccp evaluate run --impact` (`ccp/cli.py:194-198`), the `impact` worker (`ccp/evaluators/impact.py:252`)
   and `reevaluate()` (`ccp/remediation/reevaluate.py:41-43`). README.md:54-55 describes the impact step as
   the path a verdict takes.
2. spec.md:1519-1523 lists the Policy Agent's gap-analysis trigger as a third consumer of the `verdicts`
   topic. No such consumer is registered (the registrations are `ccp/normalize/consumer.py:91-94`,
   `ccp/evaluators/impact.py:252`, `ccp/evaluators/runner.py:187`, `ccp/compliance_state/writer.py:122`,
   `ccp/investigation/runner.py:248`, `ccp/evaluators/clock.py:83`, `ccp/ingest/worker.py:61-64`). The
   Policy Agent runs only from `ccp policy gap|draft`, which matches spec.md:1964-1970.
3. The parallel `investigations` topic is declared (`ccp/ingest/topics.py:25`, spec.md:632) but nothing
   produces or consumes it; the Investigator is an ordered consumer of `verdicts` (spec assumption A-74).
4. README.md:45 lists `ccp remediate simulate` and `reevaluate`; neither command exists
   (`ccp/remediation/cli.py:1-2`), and `reevaluate()` is reachable only from tests.
5. README.md:25 runs `ccp report --tenant acme`, but `--framework` is required (`ccp/cli.py:432`).
6. spec.md:1510 places the rollup in `ccp/compliance_state/rollup.py`; it is `ccp/knowledge/rollup.py`.

## Left out of this diagram

- The clock producer (`ccp/evaluators/clock.py`), the identity review queue, the evals harness, replay,
  and the step log and Langfuse tracing (`ccp/observability/steps.py`, `ccp/observability/tracing.py`).
- The embedding providers (Voyage, OpenAI, or the local hashing client, `ccp/retrieval/embeddings.py:175-185`)
  and contextual chunk enrichment through the model (`ccp/retrieval/index.py:58-63`, `ccp/retrieval/index.py:100-137`).
- Agent output tables (`investigations`, `gap_reports`, `policy_drafts`, `copilot_answers`), the
  `data/drafts/` folder, and the LangGraph `checkpoints` schema as a separate box (it is a tag on Agent core).
- Tier 3's own edges to the agent core and the approval stage (stated in a card instead).
