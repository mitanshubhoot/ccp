# Engineering Review: Target Architecture and Execution Plan

Review of `spec.md` v1.2, 2026-09-11. Produced by `/plan-eng-review` with an independent
outside-voice pass (fresh-context Opus subagent). Every recommendation below was put to
the owner as a decision; all 22 decisions (D5 to D26) and 4 deferred items took the
recommended option. `spec.md` v1.3 folds the accepted decisions into the layers; this
document is the rationale, the target architecture, and the prioritized plan.

The standard applied: if an expert team were designing this system today to be one of
the strongest production AI systems in its category, what would they change?

---

## 1. Verdict in one paragraph

The v1.2 spec has the right bones: production storage under row-level security, an
append-only bitemporal state table, a knowledge graph that routes tests to tiers, one
tool module with per-graph allowlists, "graphs read, runners write", and a security
section where every guardrail names its test. Four things were wrong enough to be P0:
the identity weights cannot link the flagship pair, so the end-to-end case is empty; a
retried evaluation writes a second open compliance-state row; the bitemporal writer has
no defined `valid_from` for the flagship test and no serialization under concurrency;
and an unscoped database read returns a clean-looking empty report. The rest of the
review moves probabilistic decisions into deterministic code (extract-then-rule,
workflow-first tier 3, a computed rollup), makes retrieval measurable and correct under
filters, adds the reliability layer the spec had none of (retries, timeouts, budgets,
dead-letter, staleness), and turns the evaluation harness from a report card at the end
into the design instrument from Phase 2.

---

## 2. Target architecture

```
                        CUSTOMER
     connectors (GitHub, HRIS)   uploads (md/txt/pdf + manifest)
               │                           │
               ▼                           ▼
   ┌───────────────────────────────────────────────────┐
   │ INGESTION  raw zone (fs, write-once)              │
   │            event_log (Postgres)                   │
   │              ordered topics: raw.*, entities      │  one ordered consumer, offset cursor
   │              parallel topics: evaluations         │  status/attempts/claimed_at, SKIP LOCKED
   │            dead_letter after N attempts           │
   └───────────────────────┬───────────────────────────┘
                           ▼
   ┌───────────────────────────────────────────────────┐
   │ NORMALIZE  3 pure mappers → Person, AccessGrant,   │
   │            Asset, Evidence, Vendor, Finding        │
   │ RESOLVE    block → kind gate → score (fitted,      │
   │            clamped) → merge / distinct / review    │
   └───────────────────────┬───────────────────────────┘
                           ▼
   ┌───────────────────────────────────────────────────┐
   │ TRIGGER    impact table (entity delta → tests)     │
   │            clock producer (evidence_max_age,       │
   │            revalidate_every)                       │
   │            debounce per (test, subject), 60 s      │
   │            tenant daily budget gate → parked       │
   └───────────────────────┬───────────────────────────┘
                           ▼
   ┌───────────────────────────────────────────────────┐
   │ EVALUATE   idempotency key (tenant,test,subject,   │
   │            trigger,prompt_v,model) UNIQUE          │
   │   tier 1   pure rules over entities                │
   │   tier 2   retrieve → EXTRACT typed fields         │
   │            (+citation each) → RULE (pure) → verdict│
   │   tier 3   fixed workflow graph: fetch → extract   │
   │            → fetch → extract → compare (pure) →    │
   │            gate; bounded gather fallback on miss   │
   │   retrieval: FTS ∪ pgvector (iterative scan,       │
   │            ef_search set) → RRF top-20 →           │
   │            rerank-2.5 → top-6; contextual chunks   │
   │   confidence: from field coverage, verified        │
   │            citations, retrieval percentile; never  │
   │            self-reported                           │
   │   RunBudget in LLMClient (tokens, USD, wall-clock) │
   │   RetryPolicy + TimeoutPolicy on every node        │
   └───────────────────────┬───────────────────────────┘
                           ▼
   ┌───────────────────────────────────────────────────┐
   │ COMPLIANCE STATE (bitemporal, append-only)         │
   │   valid_from by declared strategy per test         │
   │   per-subject advisory lock, per-tenant sequence   │
   │   rollup: worst-status precedence, names blocker   │
   │   unscoped read RAISES                             │
   └───────┬──────────────────────┬────────────────────┘
           ▼                      ▼
   INVESTIGATOR (agent loop)   POLICY AGENT (gap / draft)
   targets validated against   full requirement intent
   tenant entities + verdict   text; drafts never
   evidence refs               auto-ingested
           └──────────┬───────────┘
                      ▼
   ┌───────────────────────────────────────────────────┐
   │ APPROVALS (one table pair, append-only events)     │
   │   tier3_interrupt · remediation · policy_draft ·   │
   │   evidence_request; payload hash pinned            │
   └───────────────────────┬───────────────────────────┘
                           ▼
   REMEDIATION (dry-run default, 5 gates, rollback)
                           │
                           ▼  re-pull → impact → evaluations topic
                      compliance state

   COPILOT (agent loop, every read tool, no write path)
     intent → gather → answer(+verified citations)
     "fix" → ≤2 validated requested_actions → REPL confirm → approvals
     streams tokens; never says "ready"/"compliant"

   CROSS-CUTTING
     RLS on every tenant table incl. LangGraph checkpoints (thread_id prefix)
     nonce-fenced evidence in every prompt that reads untrusted text
     step log (gen_ai.* names) → JSONL (replay source) + Langfuse Cloud
     evals: component → composition → end-to-end, from Phase 2
```

Two rules hold across all of it. **Graphs read, runners write**: no LangGraph graph has a
side-effecting tool; a graph's output is a typed proposal and a plain runner persists it
or opens an approval. **Models extract, code decides**: every interpretive test ends in
a pure function over typed fields, and every rollup is a function over statuses.

---

## 3. Decision register

| Id | Decision | Section |
|---|---|---|
| D4 | Full scope; Langfuse Cloud replaces the six-container self-host stack | Scope |
| D5 | Tier 3 is a fixed workflow graph with one bounded gather fallback | Arch |
| D6 | Tier 2 is extract-then-rule; confidence from deterministic signals | Arch |
| D7 | Retrieval: iterative scan fix, contextual enrichment, rerank-2.5, retrieval golden set, all feature-flagged | Arch |
| D8 | RLS on LangGraph checkpoint tables keyed on thread_id prefix | Arch |
| D9 | Idempotent evaluations, RetryPolicy/TimeoutPolicy, attempts + dead-letter, infra failure never a verdict | Arch |
| D10 | RunBudget in LLMClient, tenant daily budget, concurrency cap, impact debounce | Arch |
| D11 | evidence_max_age and revalidate_every on TestSpec; clock producer | Arch |
| D12 | valid_from strategy per test; advisory lock and per-tenant sequence for supersession | Arch |
| D13 | Identity weights refit, clamped, verified-domain email in fixture, Phase 4 gate test | Quality |
| D14 | Per-topic claiming modes; model calls outside transactions; PDF subprocess with caps | Quality |
| D15 | Unscoped database access raises; rollup labels "no evaluations yet" | Quality |
| D16 | Deterministic rollup with precedence and blocking test; full requirement intent text | Quality |
| D17 | Shared agent core (node factories, GraphSpec) under four thin graph modules | Quality |
| D18 | Target validation against tenant entities and verdict evidence; two actions per Copilot turn; REPL confirm | Quality |
| D19 | Per-call random nonce fence for evidence; chunk rejection on collision; nonce logged | Quality |
| D20 | Thin eval runner and ten cases in Phase 2; every later phase adds cases to its done-when | Tests |
| D21 | Component eval suite: retrieval with distractors, extraction accuracy, support-precision, injection suite with ASR, reliability curve and ECE, judge bias, trajectories, trace capture, chunker/schema versions | Tests |
| D22 | Calibration set stays at 20 with intervals, rubric-paraphrase robustness, growth path documented | Tests |
| D23 | Fourteen infrastructure tests plus push and nightly CI workflows | Tests |
| D24 | Prompt caching layout, Batches API for offline, async fan-out for online | Perf |
| D25 | Per-query ef_search, psycopg pools, percentile thresholds from distractors, content-hash embedding cache | Perf |
| D26 | Provisional SLOs, p50/p95/p99 in evals run, regress fails on breach, streaming Copilot | Perf |
| T1-T4 | Deferred to TODOS.md: citation entailment stage, calibration growth to 100, OTLP exporter, Copilot memory via Store | TODO |

---

## 4. Prioritized recommendations

Effort is given at both scales: a human team, and Claude Code with this plan in hand.

### P0

#### R1. Identity scoring that reaches its own fixtures (D13)

- **Current state.** Layer 4 weights: email exact 0.55, local part equals handle 0.25, name 0.15, employee id 0.30; auto-merge at 0.85. Fixture table promises `auto_merge` for HR Jane Smith and GitHub `jsmith`, which has no email in the fixture.
- **Problem.** The pair scores at most 0.40 and routes to auto-distinct. HR-1 and GH-1 never see one person. The flagship end-to-end case produces nothing. Weights sum past 1.0 with no clamp; Dana Okafor's tabled 0.55 is unreachable.
- **Proposed.** GitHub fixture and live client carry `verified_domain_emails` (GraphQL `organizationVerifiedDomainEmails`). Feature set: email exact 0.55, local part equals handle 0.30, handle contains local part 0.20, name Jaro-Winkler at or above 0.90 0.20, employee id 0.40, conflicting verified emails on one provider minus 0.50, clamped to [0, 1]. Auto-merge at 0.85, auto-distinct at or below 0.40. Jane: 0.55 + 0.30 + 0.20 = 1.05, clamped 1.0, merge. Jane without the email: 0.50, review, never merge on handle plus name alone. Dana (`dokafor-dev` vs `dokafor@`): 0.20 + 0.20 = 0.40 plus a partial name signal lands in review. Chris Runner vs `ci-runner` is gated before scoring.
- **Why.** A scoring table the fixtures contradict is a bug scheduled for Phase 4. Alternatives: lower the threshold (turns handle-plus-name into an automatic merge, the exact confident-wrong-merge the kind gate exists to prevent); learn weights from labels (no labels exist at this scale).
- **Implementation.** `config/identity.yaml` weights and thresholds; `ccp/identity/scoring.py` clamp; `fixtures/*/github/members.json` gains `verified_domain_emails`; `ccp/connectors/github/client.py` maps the GraphQL field; `tests/component/test_identity_bands.py` computes every tabled pair and asserts its band.
- **Evaluation.** The band test is a Phase 4 done-when. Resolution precision and recall on the component golden cases (18 exact cases) reported by `evals run`.
- **Risks.** Verified-domain emails are absent for orgs without domain verification; the spec states that handle plus name alone lands in review, which is the safe outcome.
- **Effort.** human ~half a day / CC ~15 min.

#### R2. Evaluation idempotency, failure semantics, and a queue that can hold its own guarantees (D9, D14)

- **Current state.** `verdict_id` is a fresh ULID per attempt. No retry, timeout, attempts, or dead-letter language anywhere. Layer 2 asks one cursor to be ordered, parallel, and source-time ordered, and puts the model call inside the offset transaction.
- **Problem.** A 529 mid-batch makes the worker re-claim and re-evaluate, producing a second open bitemporal row for the same subject and valid time; `current_state` has no rule to pick one. A poison record blocks its consumer forever. A thirty-second Sonnet call holds a Postgres transaction.
- **Proposed.**
  - Deterministic evaluation key `(tenant_id, test_id, subject_key, trigger_observed_at, prompt_version, model)` with a UNIQUE index on `verdicts`; the writer is an insert-or-ignore keyed on it; the compliance-state writer is idempotent on `verdict_id`.
  - Two topic kinds declared in a Layer 2 table. Ordered topics (`raw.*`, `entities`): one consumer per topic, offset cursor, partition order by `source_ts`. Parallel topics (`evaluations`, `investigations`): per-row `status` (pending, claimed, done, dead, parked), `attempts`, `claimed_at`, `last_error`, `FOR UPDATE SKIP LOCKED` claim, visibility timeout longer than the run wall-clock budget, `dead_letter` table after N attempts with one alertable log line.
  - Every model call sits between two short transactions: claim, work with no transaction open, write results and mark done.
  - Error taxonomy in `ccp/llm/errors.py`: retryable (429, 529, connection, timeout) with exponential backoff and jitter; terminal (400, schema refusal after one re-ask, budget exceeded) to dead-letter. LangGraph `RetryPolicy` and `TimeoutPolicy` declared on every node by the shared core; a unit test fails any graph node lacking them.
  - An infrastructure failure never produces a verdict or a state row. `needs_review` means evidence was insufficient, never that the network was.
  - PDF extraction in a subprocess with size, page, and time caps; a failing file dead-letters like any other poison record.
- **Why.** The bitemporal table is the one thing the brief said not to simplify; it is only trustworthy if writes are idempotent and serialized. Alternatives: idempotency key only (leaves poison and hangs); single ordered consumer everywhere (one slow tier 3 run stalls every tenant); a durable-execution engine such as Temporal (an innovation token this build does not need; LangGraph's durability covers the agent parts and the queue covers the rest).
- **Implementation.** `ccp/storage/schema.sql` (event_log columns, dead_letter, verdicts unique index), `ccp/ingest/consumers.py` (OrderedConsumer, ParallelConsumer base classes), `ccp/llm/errors.py`, `ccp/llm/anthropic_client.py` (backoff), `ccp/agent/core.py` (policies), `ccp/connectors/manual/pdf_text.py` (subprocess).
- **Evaluation.** Tests: same record processed twice yields one verdict and one state row; two workers with one killed mid-evaluation yield exactly one verdict; poison record reaches dead-letter after N; fault injection for 429, 529, timeout, and a structured-output refusal; partition order under load for an ordered topic.
- **Risks.** Visibility timeout must exceed the wall-clock budget or a live run is re-claimed. Two consumer base classes to maintain.
- **Effort.** human ~2.5 days / CC ~1 hour.

#### R3. A bitemporal writer with two well-defined clocks (D12)

- **Current state.** `valid_from` is "the `observed_at` of the newest evidence the verdict cites". `recorded_at` is `now()`. `ux_cs_supersedes` is unique.
- **Problem.** HR-1's violation becomes true at termination plus SLA, not at any observation; a fail caused by missing evidence cites nothing, so `valid_from` is undefined for the flagship cases. Two concurrent writers both read the current row, both insert, one dies on the unique index with no retry; commit order can diverge from `recorded_at` order, so `believed_state(known_at)` can answer differently on two runs.
- **Proposed.** `TestSpec.valid_from_strategy` in `{evidence_observed, event_time, event_time_plus_sla, trigger_time}`; HR-1 and GH-1 use `event_time_plus_sla`, HR-3 stale uses `evidence_observed`, missing-evidence fails use `trigger_time`. The writer runs `pg_advisory_xact_lock(hash(tenant_id, subject_key))`, reads the current row, stamps `recorded_at` from a per-tenant sequence mapped to a timestamp (or a `recorded_seq` column with `recorded_at` for display), inserts, commits; lock, insert, commit in one short function.
- **Why.** Both time axes must be derivable by a rule you can read in `tests.yaml` and stable under concurrency, or the auditability claim is false. Alternative: fix `valid_from` only (leaves one of two writers crashing).
- **Implementation.** `ccp/knowledge/tests.yaml`, `ccp/storage/bitemporal.py`, the worked example in `spec.md` gains an SLA-derived row.
- **Evaluation.** Two-writer test asserting one supersession chain and strictly increasing `recorded_seq`; a hypothesis property test that no insert sequence yields two open rows for one subject; the five-query example plus the new SLA row.
- **Risks.** Advisory locks held too long; the writer function is small on purpose and a test proves release on error.
- **Effort.** human ~1 day / CC ~20 min.

#### R4. Loud failure at the isolation guard (D15)

- **Current state.** A-39: `current_setting('app.tenant_id', true)` returns NULL when unset, so an unscoped connection sees empty tables.
- **Problem.** An empty compliance report is indistinguishable from a clean one.
- **Proposed.** Drop `missing_ok`; unscoped queries raise. `reader.py` returns an explicit `no_evaluations_yet` state in the rollup schema. S1(b) asserts an error rather than zero rows.
- **Why.** In this domain silence is the dangerous failure. Alternative: keep silent-empty (rejected).
- **Implementation.** `ccp/storage/rls.sql`, `ccp/compliance_state/reader.py`, `tests/security/test_tenant_isolation.py`.
- **Evaluation.** The S1(b) test and a reader test on an empty tenant.
- **Risks.** Maintenance scripts must use ScopedDB or the admin URL.
- **Effort.** human ~1 hour / CC ~5 min.

### P1

#### R5. Extract-then-rule for tier 2, confidence from measured signals (D6)

- **Current state.** One structured call returns status, reasoning, citations, and a self-reported confidence; `confidence = min(model_confidence, cap)`.
- **Problem.** The rule lives in prose inside a prompt; a prompt edit flips verdicts with no code change; self-reported confidence is near-constant and decorative.
- **Proposed.** Per test: an extraction schema (GH-4: `policy_exists`, `timeline_stated`, `timeline_text`; HR-3: `subject_name_in_document`, `completion_status`, `completion_date`, `provider`), each field with a citation; a pure rule function `decide(fields, params, clock) -> status, reasons`. Confidence = f(field coverage, verified-citation rate, retrieval score percentile), computed in `confidence.py`; `model_confidence` is logged, never used.
- **Why.** Moves the decision from probabilistic to deterministic wherever the decision is a rule, which is every tier 2 test. Alternatives: keep verdict-by-model; add self-consistency sampling (deferred; needs temperature above zero and weakens replay).
- **Implementation.** `ccp/evaluators/extraction/{gh4,hr3}.py` schemas, `ccp/evaluators/rules/{gh4,hr3}.py`, `ccp/evaluators/tier2.py`, `ccp/evaluators/confidence.py`, prompt files become extraction prompts.
- **Evaluation.** Rule unit tests over every field combination; extraction field accuracy on golden cases; support-precision (field value appears in its cited quote); reliability curve and ECE of the computed confidence against golden labels, with the interrupt threshold read off the curve.
- **Risks.** Two artifacts per test.
- **Effort.** human ~1.5 days / CC ~30 min.

#### R6. Tier 3 as a fixed workflow with a bounded fallback (D5)

- **Current state.** Model-steered tool loop, up to eight calls, for HR-4.
- **Problem.** Nondeterministic path for a two-lookup task; trajectory untestable.
- **Proposed.** Nodes: `fetch_critical_list` → `extract_systems` → `fetch_latest_review` → `extract_scope` → `compare` (pure) → `gate`. `gather_fallback` (the tool loop, allowlisted, budgeted) runs only when a direct fetch returns nothing, and its use is a guardrail event. Interrupt at `gate` unchanged.
- **Why.** Predictable steps get workflows; open-ended questions (Investigator, Copilot) get agents. Alternatives: keep the loop; pure workflow with no fallback.
- **Implementation.** `ccp/agent/graphs/tier3.py` rebuilt on `ccp/agent/core.py` factories.
- **Evaluation.** Trajectory test asserts the node sequence; fallback test asserts it fires only on a miss; HR-4 tabled outcomes for both tenants.
- **Risks.** A new multi-evidence test needs its workflow nodes written.
- **Effort.** human ~1 day / CC ~25 min.

#### R7. Retrieval that is correct under filters and measurable (D7, D25)

- **Current state.** One SQL statement: FTS CTE, cosine CTE, RRF, top 6, filtered by RLS and `evidence_type`. Absolute 0.30 cosine threshold in the cap table. No retrieval metric. No pooling. `ef_search` default.
- **Problem.** pgvector's HNSW gathers `ef_search` candidates before filtering; under RLS plus a type filter the dense arm silently returns fewer rows than requested. The threshold never fires on this embedding model. Nothing measures retrieval.
- **Proposed.** `SET LOCAL hnsw.iterative_scan = relaxed_order` and `hnsw.ef_search` per query; RRF over top-20 candidates; Voyage `rerank-2.5` to top-6 (flag `RETRIEVAL_RERANK`); contextual chunk enrichment at index time with Haiku and the document cached (flag `RETRIEVAL_CONTEXTUAL`); a retrieval golden set (query → relevant chunk ids) scored by Recall@5, MRR, nDCG; 300 to 500 generated distractor chunks per tenant; cap thresholds as percentiles of the distractor score distribution, stored in config with a measurement date; content-hash cache for embeddings and enrichment; psycopg pools for ScopedDB and the saver. Reranker or embedding outage degrades to plain hybrid with a logged event.
- **Why.** Contextual retrieval and reranking are the two additions with published effect on retrieval failure, and a metric is what makes any of it a fact. Alternatives: fix and metric only (metric reads near-perfect at fixture scale, stages never taught); keep as written.
- **Implementation.** `ccp/retrieval/{hybrid,rerank,contextual,embeddings,cache}.py`, `scripts/make_distractors.py`, `evals/golden/retrieval.yaml`, `config/retrieval.yaml`.
- **Evaluation.** Short-result test (six asked, six returned); Recall@5 and MRR with the ablation flags off and on; percentile calibration printed by `ccp index rebuild`.
- **Risks.** Two more paid calls that must degrade, not fail; thresholds re-measured on model change.
- **Effort.** human ~3 days / CC ~1 hour.

#### R8. RLS on the checkpoint tables (D8)

- **Proposed.** `ALTER TABLE checkpoints.* ENABLE/FORCE ROW LEVEL SECURITY; CREATE POLICY ... USING (thread_id LIKE current_setting('app.tenant_id') || ':%')` applied by the migration runner after the saver's `setup()`; the saver's pool connects with `options='-c app.tenant_id=<tenant>'` per tenant-scoped graph build; a unit test asserts every minted thread id starts with the tenant.
- **Evaluation.** S1 extended: raw read of another tenant's thread returns nothing under the setting and raises without it.
- **Effort.** human ~half a day / CC ~15 min.

#### R9. Budgets at every level (D10)

- **Proposed.** `RunBudget(max_input_tokens, max_output_tokens, max_usd, max_wall_clock, max_tool_calls)` enforced inside `LLMClient` and passed through graph config; `tenants.yaml` daily USD budget enforced by the evaluations consumer, which parks records as `budget_exhausted`; per-tenant concurrency cap (semaphore in the async client); 60-second debounce per `(test, subject)` in the impact step with last-trigger-wins; the report shows parked subjects as "evaluation pending".
- **Evaluation.** Stub loop past the cap stops with `budget_exceeded`; ten changes to one subject enqueue one evaluation; a tenant over budget parks and resumes after reset.
- **Effort.** human ~1 day / CC ~25 min.

#### R10. Time-based re-evaluation (D11)

- **Proposed.** `TestSpec.evidence_max_age` and `revalidate_every`; `ccp/evaluators/clock.py` producer run by `ccp worker run --consumer clock` (and by the nightly CI job) enqueues subjects whose newest cited evidence is older than the max age or whose last evaluation exceeds the interval; the resulting verdict supersedes with `reason = stale_evidence`. All evaluators take an injectable clock.
- **Evaluation.** Golden case advancing the clock past twelve months for an HR-3 pass and asserting a superseding fail.
- **Effort.** human ~1 day / CC ~20 min.

#### R11. An unguessable evidence boundary and an adversarial suite (D19, D21)

- **Proposed.** `frame_evidence()` generates a per-call random fence, rejects any chunk containing it, names the fence in the system turn, and records it in the step log; the same function feeds the judge. Injection suite of 15 to 20 cases: direct override, role-play, boundary-break with every plausible closing tag, obfuscated (unicode, base64, spaced), fake-citation injection, table-cell injection, cross-tenant reference, and judge-targeted injection; scored as attack-success-rate and printed beside golden scores.
- **Evaluation.** ASR per class, regress fails on any increase.
- **Effort.** human ~1.5 days / CC ~40 min.

#### R12. Validated, bounded actions (D18)

- **Proposed.** `ccp/remediation/targets.py` resolves every proposal target against tenant entities and the originating verdict's `evidence_refs`; shared by Investigator and Copilot runners; at most two `requested_actions` per Copilot turn; explicit REPL confirmation before approval rows are written.
- **Evaluation.** A proposal naming globex's repo to acme's Investigator is rejected with a guardrail event; three requested actions yield two approvals and a note; no approval row without confirmation.
- **Effort.** human ~half a day / CC ~15 min.

#### R13. A computed requirement state and real requirement text (D16)

- **Proposed.** `rollup(statuses) -> RequirementState` with precedence fail > needs_review > pending > partial > pass and the blocking `(test_id, subject_key)`; `frameworks/*.yaml` carries full requirement intent paraphrases with sub-clauses in our own words; the Policy Agent's gap analysis and its golden cases (one negative per requirement) use them.
- **Evaluation.** Rollup unit test over every status combination; Policy Agent negative cases.
- **Effort.** human ~1 day / CC ~30 min.

#### R14. Evaluation as the design instrument (D20, D21, D22)

- **Proposed.** Phase 2 delivers `ccp evals run` with the golden schema and ten exact cases; every later phase's done-when includes its cases green. The component suite adds: retrieval metrics with distractors and ablation; extraction field accuracy; support-precision; injection ASR; reliability curve and ECE; judge bias checks (swapped candidate order, length); trajectory evals for tier 3, Investigator, and Copilot; `ccp evals capture <trace_id>` writing a golden case from a trace; `chunker_version` and `schema_version` on verdicts. Calibration stays at 20 with confidence intervals, rubric-paraphrase robustness replacing temperature-zero self-consistency, and a documented growth path.
- **Evaluation.** The harness measures itself: every metric has a baseline file and `regress` diffs it.
- **Effort.** human ~5 days / CC ~2 hours across phases.

#### R15. Infrastructure tests and CI (D23)

- **Proposed.** Tests listed in the Section 3 coverage diagram; `.github/workflows/ci.yml` (push: ruff, mypy, pytest no-key against a Postgres service) and `.github/workflows/nightly-evals.yml` (keyed, cost-capped, uploads `evals/runs/latest.json`).
- **Effort.** human ~3 days / CC ~1 hour.

#### R16. Prompt caching (D24, part)

- **Proposed.** Every prompt built stable-first: system, tool schemas, rubric, framework text, then a cache breakpoint, then the nonce-fenced evidence. A test asserts `cache_read_input_tokens` is non-zero on the second identical call.
- **Effort.** human ~half a day / CC ~15 min.

### P2

#### R17. Shared agent core (D17)

`ccp/agent/core.py`: `make_tool_loop(allowlist, budget)`, `make_structured_node(schema)`, `verify_citations_node`, `budget_gate`, `with_policies(node)`, and `GraphSpec`. Four graph modules become declarations. The allowlist and read-only tests run against the core. human ~1 day / CC ~30 min.

#### R18. Batches for offline work, async for online (D24, rest)

Offline (first run, enrichment, eval suite) goes through the Message Batches API keyed by custom id; online uses the async client under the concurrency cap. human ~1 day / CC ~30 min.

#### R19. Latency SLOs and streaming (D26)

`config/slos.yaml` provisional targets; p50/p95/p99 per workflow in `evals run`; regress fails on p95 breach; Copilot REPL streams with first-token and total latency recorded. human ~half a day / CC ~15 min.

#### R20. Tracing: Langfuse Cloud, `gen_ai.*` names (D4, T3)

`docker-compose.yml` runs Postgres only; Langfuse Cloud via two keys; JSONL fields renamed to the OpenTelemetry GenAI convention; OTLP exporter deferred (T3). human ~half a day / CC ~15 min.

### P3 (TODOS.md)

T1 citation entailment stage; T2 calibration growth to 100 with a second annotator; T3 OTLP exporter; T4 Copilot memory via LangGraph Store.

---

## 5. Evaluation architecture, mirrored to the product

| Workflow | Level 1 components | Level 2 compositions | Level 3 end-to-end |
|---|---|---|---|
| Ingest and normalize | mapper exactness; dedup; ordering | ingest → normalize → resolve | full tenant run, both tenants |
| Identity | band test per pair; kind gate | resolve → HR-1 | termination case |
| Retrieval | Recall@5, MRR, nDCG with distractors; ablation per stage; short-result check | retrieve → rerank → extract | tier 2 golden cases |
| Tier 2 | extraction field accuracy; rule unit tests; support-precision; injection ASR | retrieve → extract → rule → verdict | GH-4, HR-3 per tenant |
| Tier 3 | trajectory; compare() unit; fallback trigger | fetch → extract → compare → gate → resume | HR-4 per tenant |
| Investigator | category and action exactness; target validation | verdict → investigation → approval | simulated remediation loop |
| Policy | gap list exactness with negatives; scrub | gather → analyze → compose | draft judged by rubric |
| Copilot | intent accuracy; never-ready string test; cross-tenant refusal | classify → gather → answer → confirm | four question types judged |
| Cross-cutting | reliability curve and ECE; judge bias; calibration with intervals; latency percentiles; cost per completed workflow | | regress diff against latest.json |

Every judged metric records the judge model id, prompt version, and rubric hash. Deterministic metrics are preferred everywhere a value can be compared exactly.

---

## 6. Security additions

| Id | Guardrail | Test |
|---|---|---|
| S14 | Checkpoint tables under RLS | raw read of another tenant's thread: empty under setting, raises without |
| S15 | Unscoped access raises | S1(b) rewritten |
| S16 | Nonce fence | boundary-break suite passes by construction |
| S17 | Injection suite with ASR | 15-20 variants, per-class ASR, regress on increase |
| S18 | Budgets | run budget, tenant budget, concurrency, debounce tests |
| S19 | Target validation and per-turn cap | Investigator and Copilot rejection tests |
| S20 | PDF resource caps | oversized, over-page, and hanging PDFs dead-letter |
| S21 | PII in traces | Langfuse Cloud project gets the same data-processing note as Anthropic and Voyage; step log redacts secrets patterns; retention documented |

---

## 7. Observability: the loop from failure to regression test

`production failure` → Langfuse trace or JSONL step → `ccp evals capture <trace_id>` writes `evals/golden/captured/<id>.yaml` with inputs, retrieved chunk ids, fence nonce, prompt and schema versions, and the observed output → a human sets `expected` → the case runs in `regress` forever. Replay reads only JSONL and Postgres.

---

## 8. NOT in scope

- **MinIO or S3 for the raw zone.** Filesystem is a faithful write-once store; an S3 API adds a container without teaching anything new.
- **Self-hosted Langfuse.** Replaced by Langfuse Cloud (D4).
- **Temporal or another durable-execution engine.** LangGraph durability plus the Postgres queue cover this build's needs.
- **Citation entailment via a second model.** T1, decided after support-precision exists.
- **Calibration set beyond 20.** T2, human labelling scheduled separately.
- **OTLP exporter.** T3; the attribute names are adopted now.
- **Copilot cross-session memory.** T4; needs an authenticated user identity.
- **A UI, RBAC, real OAuth, screenshots and OCR, connectors beyond GitHub and HRIS.** Unchanged non-goals.
- **Live remediation test organization.** Live path stays behind the `live_github` marker; a disposable org is the operator's to provide.

## 9. What already exists

No code exists. Built-ins the plan now uses instead of rolling by hand: LangGraph `RetryPolicy`, `TimeoutPolicy`, `interrupt`, `Command(resume)`, and `PostgresSaver`; pgvector iterative scans; Postgres advisory locks, sequences, `SKIP LOCKED`, and row-level security; Anthropic structured outputs, prompt caching, and the Message Batches API; Voyage `rerank-2.5`; Langfuse's LangChain callback handler and Cloud ingestion; pypdf; hypothesis for property tests.

## 10. Diagrams to embed in code

Inline ASCII diagrams in: `ccp/storage/bitemporal.py` (supersession timeline and the two as-of queries), `ccp/ingest/consumers.py` (ordered versus parallel claiming state machine), `ccp/identity/resolver.py` (block → gate → score → route decision tree), `ccp/agent/core.py` (node pipeline with policies and budget), `ccp/evaluators/tier2.py` (retrieve → extract → rule), `ccp/retrieval/hybrid.py` (FTS ∪ dense → RRF → rerank), `ccp/approvals/store.py` (approval event state machine), `ccp/remediation/runner.py` (five gates), and `tests/e2e/test_remediation_loop.py` (what the loop test sets up and why).

## 11. Failure modes for new code paths

| Code path | Realistic failure | Test | Handling | User sees |
|---|---|---|---|---|
| Parallel evaluations consumer | worker dies mid-run | two-worker kill test | visibility timeout, re-claim, idempotent write | one verdict, a retry step in the log |
| Ordered consumer | out-of-order source_ts | late-arrival test | partition sort before process | correct valid_from |
| LLM client | 529 storm | fault injection | backoff, then dead-letter, never a verdict | record in dead_letter, alert line |
| Extraction | schema refusal | fault injection | one re-ask, then terminal | dead-letter, not needs_review |
| Retrieval | reranker down | degrade test | plain hybrid, guardrail event, cap lowered | verdict with lowered confidence |
| Retrieval | filtered HNSW short result | short-result test | iterative scan | six chunks |
| Bitemporal writer | two writers, one subject | two-writer test | advisory lock, sequence | one chain |
| Clock producer | clock skew | injectable clock test | DB `now()` only | stale supersession at the right instant |
| Budgets | tenant over budget | parking test | parked, resumes on reset | "evaluation pending" |
| Nonce fence | chunk contains fence | collision test | chunk rejected, event | verdict cites remaining chunks or needs_review |
| Copilot actions | "fix everything" | cap test | two actions, confirm | note about the cap |
| PDF extraction | hostile PDF | caps test | subprocess timeout, dead-letter | upload listed as rejected |
| Checkpoint RLS | wrong-tenant thread | S14 | empty or raise | refusal |
| Langfuse Cloud | unreachable | degrade test | JSONL only, one warning | trace missing in UI, replay works |

Critical gaps (no test, no handling, silent): none after the decisions above. Two watch items with handling but no test in this pass: raw-zone disk exhaustion (write fails loudly), and free-text reviewer identity on approvals (a known non-goal, logged).

## 12. Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| Core (Phases 0-3) | config, tenancy, storage, ingest, connectors, normalize | — |
| Identity (R1) | identity, fixtures | Core |
| Bitemporal + RLS (R3, R4, R8) | storage, compliance_state | Core |
| Retrieval (R7, R25) | retrieval, config | Core |
| Agent core + tier 3 + graphs (R6, R17) | agent | Core, Retrieval |
| Tier 2 extract-then-rule (R5) | evaluators | Retrieval |
| Reliability + budgets + clock (R2, R9, R10) | ingest, llm, evaluators | Core |
| Post-verdict agents + approvals + actions (R12, R13) | investigation, policy, copilot, approvals, remediation, knowledge | Agent core, Bitemporal |
| Evals + CI (R14, R15) | evals, tests, .github | Core (thin runner), then everything |
| Observability (R16, R19, R20) | observability, llm | Core |

Lanes: **A** Identity → Bitemporal+RLS (shared storage/). **B** Retrieval → Tier 2 (shared retrieval/). **C** Reliability + budgets + clock (ingest/, llm/). **D** Observability (observability/). Launch A, B, C, D in parallel after Core. Then **E** Agent core + tier 3 (needs B). Then **F** Post-verdict agents + approvals (needs A and E). Evals lane runs alongside from Phase 2 and gates each merge. Conflict flags: C and D both touch `llm/` (budget and caching live in the same client file); sequence C before D or coordinate on `llm/anthropic_client.py`.

## 13. Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~half a day / CC: ~15min)** — identity — Refit weights with clamp, add verified_domain_emails to fixture and live client, add band gate test
  - Surfaced by: Code quality — finding 7 (D13)
  - Files: config/identity.yaml, ccp/identity/scoring.py, fixtures/*/github/members.json, ccp/connectors/github/client.py, tests/component/test_identity_bands.py
  - Verify: pytest tests/component/test_identity_bands.py
- [ ] **T2 (P1, human: ~1 day / CC: ~30min)** — storage/ingest — Deterministic evaluation key with UNIQUE; event_log status/attempts/claimed_at/last_error; dead_letter table; per-topic consumer base classes
  - Surfaced by: Architecture — finding 5 (D9); Code quality — finding 8 (D14)
  - Files: ccp/storage/schema.sql, ccp/ingest/consumers.py, ccp/ingest/worker.py
  - Verify: pytest tests/component/test_queue.py tests/workflow/test_two_workers.py
- [ ] **T3 (P1, human: ~1 day / CC: ~30min)** — llm — Error taxonomy, backoff, RunBudget, prompt-caching layout, Batches path, async client with concurrency cap
  - Surfaced by: Architecture — findings 5, 6 (D9, D10); Performance — findings 14, 15 (D24)
  - Files: ccp/llm/errors.py, ccp/llm/client.py, ccp/llm/anthropic_client.py, ccp/llm/budget.py
  - Verify: pytest tests/unit/test_llm_client.py (fault injection, budget, cache_read non-zero)
- [ ] **T4 (P1, human: ~1 day / CC: ~20min)** — storage — valid_from_strategy on TestSpec; advisory lock and per-tenant sequence in the bitemporal writer; SLA row in the worked example
  - Surfaced by: Architecture — outside voice point 9 (D12)
  - Files: ccp/knowledge/tests.yaml, ccp/storage/bitemporal.py, tests/component/test_bitemporal.py, tests/property/test_bitemporal_props.py
  - Verify: pytest tests/component/test_bitemporal.py tests/property/
- [ ] **T5 (P1, human: ~1 hour / CC: ~5min)** — storage — Unscoped access raises; rollup no_evaluations_yet state
  - Surfaced by: Code quality — finding 9 (D15)
  - Files: ccp/storage/rls.sql, ccp/compliance_state/reader.py, tests/security/test_tenant_isolation.py
  - Verify: pytest tests/security/test_tenant_isolation.py
- [ ] **T6 (P1, human: ~half a day / CC: ~15min)** — storage/agent — RLS policy on checkpoint tables; saver pool with tenant option; thread-id prefix unit test
  - Surfaced by: Architecture — finding 4 (D8)
  - Files: ccp/storage/rls.sql, ccp/storage/db.py, ccp/agent/checkpoints.py, tests/security/test_checkpoint_rls.py
  - Verify: pytest tests/security/test_checkpoint_rls.py
- [ ] **T7 (P1, human: ~1.5 days / CC: ~30min)** — evaluators — Extraction schemas and pure rules for GH-4 and HR-3; deterministic confidence; model_confidence logged only
  - Surfaced by: Architecture — finding 2 (D6)
  - Files: ccp/evaluators/extraction/, ccp/evaluators/rules/, ccp/evaluators/tier2.py, ccp/evaluators/confidence.py, ccp/llm/prompts/
  - Verify: pytest tests/unit/test_rules.py; ccp evals run --target tier2
- [ ] **T8 (P1, human: ~1 day / CC: ~25min)** — agent — Tier 3 fixed workflow nodes with bounded gather fallback; trajectory test
  - Surfaced by: Architecture — finding 1 (D5)
  - Files: ccp/agent/graphs/tier3.py, tests/component/test_tier3_trajectory.py
  - Verify: pytest tests/component/test_tier3_trajectory.py
- [ ] **T9 (P1, human: ~3 days / CC: ~1h)** — retrieval — Iterative scan and ef_search per query; RRF top-20; rerank-2.5; contextual enrichment; pools; embedding cache; percentile thresholds; distractor generator; retrieval golden set
  - Surfaced by: Architecture — finding 3 (D7); Performance — finding 16 (D25)
  - Files: ccp/retrieval/*.py, scripts/make_distractors.py, evals/golden/retrieval.yaml, config/retrieval.yaml
  - Verify: pytest tests/component/test_retrieval_short_result.py; ccp evals run --target retrieval
- [ ] **T10 (P1, human: ~1 day / CC: ~25min)** — evaluators — Tenant daily budget parking, concurrency cap, 60 s impact debounce
  - Surfaced by: Architecture — finding 6 (D10)
  - Files: ccp/evaluators/impact.py, ccp/ingest/consumers.py, config/tenants.yaml
  - Verify: pytest tests/component/test_budgets.py
- [ ] **T11 (P1, human: ~1 day / CC: ~20min)** — knowledge/evaluators — evidence_max_age and revalidate_every; clock producer; injectable clock
  - Surfaced by: Architecture — outside voice point 2 (D11)
  - Files: ccp/knowledge/tests.yaml, ccp/evaluators/clock.py, tests/workflow/test_staleness.py
  - Verify: pytest tests/workflow/test_staleness.py
- [ ] **T12 (P1, human: ~2 hours / CC: ~10min)** — security — Nonce fence in frame_evidence(); collision rejection; nonce in step log; judge uses the same function
  - Surfaced by: Code quality — finding 13 (D19)
  - Files: ccp/security/prompt_guard.py, ccp/observability/steps.py, tests/security/test_prompt_injection.py
  - Verify: pytest tests/security/test_prompt_injection.py
- [ ] **T13 (P1, human: ~half a day / CC: ~15min)** — remediation — Target validator; two-action cap; REPL confirm
  - Surfaced by: Code quality — finding 12 (D18)
  - Files: ccp/remediation/targets.py, ccp/investigation/runner.py, ccp/copilot/runner.py, ccp/copilot/repl.py
  - Verify: pytest tests/security/test_action_targets.py tests/security/test_copilot_cannot_act.py
- [ ] **T14 (P1, human: ~1 day / CC: ~30min)** — knowledge/compliance_state — Deterministic rollup with precedence and blocker; full requirement intent text
  - Surfaced by: Code quality — finding 10 (D16)
  - Files: ccp/compliance_state/rollup.py, ccp/knowledge/frameworks/*.yaml, tests/unit/test_rollup.py
  - Verify: pytest tests/unit/test_rollup.py
- [ ] **T15 (P1, human: ~1 day / CC: ~20min)** — evals — Thin runner, golden schema, ten exact cases in Phase 2; done-when lines per phase
  - Surfaced by: Test review — D20
  - Files: ccp/evals/runner.py, evals/golden/*.yaml, spec.md phase table
  - Verify: ccp evals run
- [ ] **T16 (P1, human: ~4 days / CC: ~1.5h)** — evals — Component suite: extraction accuracy, support-precision, injection suite with ASR, reliability curve and ECE, judge bias, trajectory evals, trace capture, chunker/schema versions
  - Surfaced by: Test review — D21; outside voice points 5, 6, 7, 8
  - Files: ccp/evals/scorers/*.py, evals/golden/*.yaml, evals/adversarial/*.yaml, ccp/evals/capture.py
  - Verify: ccp evals run; ccp evals regress
- [ ] **T17 (P2, human: ~half a day / CC: ~15min)** — evals — Calibration with intervals, rubric-paraphrase robustness, growth path doc
  - Surfaced by: Test review — D22
  - Files: ccp/evals/calibrate.py, evals/labeled/README.md
  - Verify: ccp evals calibrate
- [ ] **T18 (P1, human: ~3 days / CC: ~1h)** — tests/ci — Fourteen infrastructure tests; push and nightly workflows
  - Surfaced by: Test review — D23
  - Files: tests/**, .github/workflows/ci.yml, .github/workflows/nightly-evals.yml
  - Verify: gh workflow run ci.yml
- [ ] **T19 (P2, human: ~1 day / CC: ~30min)** — agent — Shared core: node factories, GraphSpec, policy wrappers; thin graph modules
  - Surfaced by: Code quality — finding 11 (D17)
  - Files: ccp/agent/core.py, ccp/agent/graphs/*.py
  - Verify: pytest tests/unit/test_agent_core.py tests/security/test_tool_allowlist.py
- [ ] **T20 (P2, human: ~half a day / CC: ~15min)** — observability — SLO table, percentiles in evals run, regress on p95 breach, streaming Copilot
  - Surfaced by: Performance — D26
  - Files: config/slos.yaml, ccp/evals/latency.py, ccp/copilot/repl.py
  - Verify: ccp evals run prints percentiles
- [ ] **T21 (P2, human: ~half a day / CC: ~15min)** — observability — Langfuse Cloud config, Postgres-only compose, gen_ai.* field names in JSONL
  - Surfaced by: Scope — D4; TODO 3
  - Files: docker-compose.yml, ccp/observability/steps.py, .env.example
  - Verify: pytest tests/unit/test_steps_schema.py
- [ ] **T22 (P1, human: ~half a day / CC: ~15min)** — connectors — PDF extraction subprocess with size, page, and time caps
  - Surfaced by: Code quality — finding 8 (D14)
  - Files: ccp/connectors/manual/pdf_text.py, tests/component/test_pdf_limits.py
  - Verify: pytest tests/component/test_pdf_limits.py

_No new tasks from the Performance review beyond T3, T9, T20._

## 14. Completion summary

- Step 0: Scope Challenge — scope accepted at full scope; one reduction taken (Langfuse Cloud replaces self-host)
- Architecture Review: 8 issues found, 8 decided
- Code Quality Review: 7 issues found, 7 decided
- Test Review: diagram produced, 39 gaps identified, covered by 4 decisions
- Performance Review: 3 issues found, 3 decided
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 4 items proposed to user, 4 accepted
- Failure modes: 0 critical gaps flagged, 2 watch items
- Outside voice: ran (Claude subagent; Codex not installed); 15 findings; 10 absorbed into decisions, 2 already agreed, 3 noted as tension and resolved by prior decisions
- Parallelization: 6 lanes, 4 parallel after core / 2 sequential
- Lake Score: 22/22 recommendations chose the complete option

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found (Claude subagent) | 15 findings, 12 absorbed |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 22 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** the outside voice independently reached extract-then-rule, workflow-first tier 3, idempotent evaluation writes, and impact coalescing; it went further on collapsing all graphs into one and dropping the retrieval stack until distractors exist, both of which the owner decided against with the option on the table.
- **VERDICT:** ENG CLEARED — ready to implement from spec.md v1.3.

NO UNRESOLVED DECISIONS
