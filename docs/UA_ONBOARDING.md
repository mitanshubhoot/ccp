# Onboarding guide: ccp

Generated on 11 Nov 2026 from the Understand Anything knowledge graph (`.ua/knowledge-graph.json`, commit `66c3e7f`, 532 files analysed, 2,610 nodes and 8,886 edges). Open it visually with `/understand-dashboard`; ask it questions with `/understand-chat`. Read `spec.md` before changing anything: it is the design, and section 22 (assumptions A-1 onward) explains why surprising choices were made.

## 1. Project overview

**ccp** is a teaching-scale but faithful build of an AI-native compliance automation platform on production technology. Connectors land immutable raw evidence; normalization and identity resolution turn it into canonical entities; deterministic and model-backed evaluators produce verdicts with verified citations; and a bitemporal state table answers both "what is true" and "what did we believe then". Agents (Investigator, Policy Agent, Copilot, task items) read through one tool surface and can only propose, behind one human approval stage. When a test fails, CCP suggests a fix and confirms it from a later read. It never opens a pull request or changes a customer's systems.

Newer capabilities sit around that core: feedback on uploads within seconds (evidence segments, admissibility rules and an adequacy verifier), security questionnaires, code-scored framework mappings and model-drafted checks, AWS coverage with service playbooks, as-of audit packages that an auditor verifies offline, a read-only auditor workspace with an attestation record, method cards and quality snapshots, measured autonomy levels for skipping review, and deployment cells with EU residency.

| | |
|---|---|
| Languages | Python (317 files), SQL (63 migrations), Markdown, YAML, JSON, HTML (Jinja2), Terraform, Dockerfile, TOML |
| Frameworks | FastAPI, Pydantic, Uvicorn, Typer, LangGraph, Anthropic SDK (first-party API or Amazon Bedrock, per cell), Langfuse, psycopg on PostgreSQL 17 with pgvector, boto3, pytest, Hypothesis |
| Runtime | One cell per region on AWS. ECS Fargate runs the API (`ccp serve`) and the worker (`ccp worker run`) from one container image, plus a PgBouncer service in transaction mode. RDS PostgreSQL 17 (Multi-AZ) with a read replica; raw evidence in an S3 bucket with Object Lock and per-tenant KMS keys; signed audit checkpoints in a separate audit account |
| Entry points | `python -m ccp` / the `ccp` console script (`ccp/cli.py`), the HTTP API under `/v1` and the UI under `/ui` (`ccp/api/app.py`, `ccp/api/ui.py`), and `ccp/audit/package_verify.py`, which an auditor runs on its own |

Developer quickstart: `cp .env.example .env`, `docker compose up -d`, `uv sync --group dev`, `uv run ccp db migrate`, `uv run ccp knowledge load`, `uv run ccp run --tenant acme`, `uv run ccp report --tenant acme`; this runs offline on the fixture tenants (`acme`, `globex`), and model keys switch on tiers 2 and 3, the agents and the judge.

## 2. Architecture layers

Ten layers, derived from the import-linter contract in `pyproject.toml`. Higher layers may import lower ones, never the reverse; CI runs import-linter and fails the build if they do.

| Layer | Files | What lives there |
|---|---|---|
| CLI & Web API | 16 | The `ccp` Typer CLI, the FastAPI `/v1` API (including fix suggestions, autonomy levels, method cards and the auditor workspace), reason codes and the server-rendered reviewer UI |
| Evaluation Harness | 41 | Golden and adversarial suites, the LLM judge and its calibration, shadow and regression gates, context probes, production sampling and relabels, drafting of declarative checks, framework-mapping review, method cards and quality snapshots, and `ccp replay` |
| Agent Workflows | 87 | The agent harness (LangGraph core, read-only tools, allowlists, authz, run logs, stop rules) and the workflows on it: Copilot, investigation, policy drafting, task items, lookups, coverage and playbooks, fix suggestions with confirmation, and questionnaires. Also approval actions, tenant onboarding, and the compliance-state run with audit packages, attestations, the auditor workspace and autonomy levels |
| Evaluators | 47 | Tier 1 deterministic and declarative rules, tier 2 extract-then-rule with verified citations and a second reader, upload feedback and the adequacy verifier, confidence and completeness, budget gates, rule versions and changes, and the verdict record |
| Knowledge, Retrieval & LLM | 83 | The compliance catalogue (frameworks, controls, tests, checks, impact table, mappings, admissibility rules, the fix catalogue, lookups, cloud service classes), the retrieval stack (segmentation, admission, chunking, contextual embeddings, rerank, calibration, generations) and the single `LLMClient` with pinned models, budgets and versioned prompts |
| Ingestion & Identity | 59 | Connectors for GitHub, AWS, HRIS, Okta and manual uploads with conformance checks; the event log, consumers, worker, raw zone and landing; mappers into entities; identity resolution with human review; and the audit chain's checkpoints and verifiers |
| Platform Foundation | 91 | Tenant-scoped Postgres access with forced row-level security, connection pools, bitemporal helpers, tenant partitions and the numbered SQL migrations, auth and members, the approval store, security guards, observability, deployment cells, credentials, tenancy, config and shared types |
| Tests & Fixtures | 69 | Security suites (tenant isolation, RLS, prompt injection, tool allowlists, agent role, secret redaction, approvals fail closed), workflow, chaos and property tests, and the two fixture tenants. Unit and component tests exist but are not in this graph |
| Infrastructure & Configuration | 33 | The Dockerfile, GitHub Actions (CI, deploy, nightly evals), Terraform for a cell (ECS Fargate, RDS and the replica, S3, PgBouncer), the customer's CloudFormation read role, `pyproject.toml`, and the `config/*.yaml` runtime settings |
| Documentation | 6 | `spec.md`, `README.md`, `CLAUDE.md`, `TODOS.md`, the engineering review and the credentials verification checklist |

## 3. Key concepts

These are the rules the code is built around. Most are enforced by a test, a database constraint or the import contract, not by convention.

- **Models extract, code decides.** No model call returns a pass or fail. Tier 2 asks a model to fill typed fields with pointer citations; code checks each citation, then a pure, versioned rule sets the status. A refusal or a failed check becomes `needs_review`. Where code can answer, no model is called at all.
- **Graphs read, runners write.** No LangGraph graph has a tool with a side effect. A graph returns a typed proposal; a plain Python runner persists it or opens an approval. Agent tools read through a read-only, RLS-scoped database role, and agents may not import `ccp/credentials.py` (an import-linter contract).
- **One query surface per tenant.** `ccp/storage/scoped.py` (`ScopedDB`) is the only way to read tenant data; every transaction sets `app.tenant_id`, and forced row-level security filters every row. `ccp/config.py` is the only module that reads the environment; `ccp/credentials.py` is the only one that reads secrets.
- **Evidence is immutable.** Connectors emit frozen `SourceEvent`s keyed by provider ids. Landing writes the untouched payload once, with a digest, to the raw zone (an S3 bucket with Object Lock) before anything maps it. Lookups, inventory snapshots and playbook reads land their responses the same way. Absence counts only from a complete sync.
- **Uploads are checked on arrival.** Code splits each document version into citable segments (headings, sentences, table cells), so a model cites a segment id and code renders the quote. Within seconds, code applies per-type admissibility rules and a provenance badge; a blocking reason keeps the document out of evaluation. An adequacy verifier then asks whether an auditor would accept it, in shadow until it agrees with auditors' labels.
- **Postgres is the queue.** `ccp/ingest/event_log.py` is a Kafka-style log with per-tenant sequence numbers, NOTIFY wake-ups, leases and dead letters. Ordered consumers process a tenant's topic in order; parallel consumers handle independent work. A fair dispatcher serves each consumer across tenants, and per-tenant schedules fire each job exactly once.
- **Bitemporal state.** `compliance_state` records when something was true (`valid_from`/`valid_to`) and when CCP learned it (`recorded_seq`). Rows are superseded with a reason, never edited, so as-of queries can show what CCP believed on any date.
- **Idempotent, reproducible verdicts.** Each verdict has a hashed evaluation key, so retries are no-ops. It also stores its rule version, pinned inputs and exact model request and response, so `ccp replay` can rebuild it without sampling a model again.
- **Prompt input is guarded.** `ccp/security/prompt_guard.py` is the only path by which evidence and tool output enter a prompt: it redacts secrets, strips invisible characters and fences content with a nonce.
- **One approval stage, failing closed.** Every pause and every action goes through `ccp/approvals/`: append-only items and events, decisions only from signed-in members with the right role, each bound to the hash of the payload the reviewer saw.
- **Remediation is suggest-only (A-158 to A-160).** CCP never opens a pull request or changes a customer's systems. The sequence is:
  1. A FAIL triggers lookups that code chooses from a per-test table: AWS Config history, CloudTrail events and a search of the organization's Terraform. The responses land in the raw zone first.
  2. When the rule explains the failure, code writes the investigation. Otherwise the Investigator picks a fix by id.
  3. Code renders the fix from the versioned catalogue (`ccp/knowledge/fixes.yaml`) and checks it offline: parameters, CLI safety, Terraform syntax and change scope.
  4. The customer applies it. A person may record accept or dismiss, but that never counts as proof.
  5. A later read confirms it, and a FAIL within 30 days reopens it.

  The older live GitHub collaborator removal (`ccp/remediation/runner.py`) still exists as an opt-in path, off by default.
- **Coverage comes from what the account holds.** A daily AWS Config inventory is crossed with service classes and their intents into a coverage map (pass, fail, pending, gap, not_assessed). A new resource type waits for a person to choose its class. Service playbooks read gaps and record only fail or needs_review, never pass.
- **Catalogue changes go through code and a person.** Code scores framework mappings; a model may only pick among code's candidates or abstain. A model may draft a declarative check offline, but four code gates and a named reviewer decide whether it enters the catalogue.
- **Measured, not trusted.** Golden and adversarial cases run several times, a calibrated LLM judge grades them, and regression gates block a release. Method cards explain from recorded facts how a verdict or fix suggestion was decided. Quarterly quality snapshots per AI workflow withhold any bound that rests on too few FAIL cases.
- **Skipping review is earned.** A draft skips review only for reversible, low-impact decision classes, at a level promoted on a published quality snapshot for the current release bundle, within a daily exposure budget and with a seeded review sample. A bundle change or a quality alarm demotes the level.
- **Tamper-evident records.** Migration `0043_audit_chain.sql` hash-chains record tables per tenant. `ccp/audit/` signs hourly checkpoints of each chain head, exports them write-once to an Object Lock bucket in a separate audit account, and verifies the chain.
- **Every outside assertion is attested.** An as-of audit package holds results with their method, raw evidence pointers and chain proofs under a signed checkpoint, sealed write-once, and an auditor can verify it offline. Auditors get time-boxed, read-only access to named packages. Each grant and each send is recorded as an attestation under the versioned publication policy.
- **Cells fix where a tenant lives.** A tenant's cell fixes its region, data residency and model route; the EU cell uses Bedrock EU inference profiles. Behind PgBouncer in transaction mode the tenant id must be set per transaction, as `ScopedDB` does. Lag-tolerant reads go to the replica only within a lag bound, and the largest tenants get their own partition with row-level security kept.

## 4. Guided tour

Fifteen steps that follow one finding from raw evidence to a confirmed fix. Each step names the files to open.

1. **Project overview.** `README.md`, `spec.md`, `docs/eng-review-2026-09-11.md`.
2. **Entry points and pipeline.** `ccp/__main__.py`, `ccp/cli.py`, `ccp/compliance_state/run.py`, `ccp/api/app.py`.
3. **Tenant-scoped foundation.** `ccp/storage/scoped.py`, `ccp/tenancy.py`, `ccp/config.py`, `ccp/storage/db.py`, `0002_rls.sql`.
4. **Connectors land raw evidence.** `ccp/connectors/base.py`, the AWS connector, acme's `employees.json` (where the leaver story starts), `ccp/ingest/landing.py`, `raw_zone.py`.
5. **Normalization and identity.** `ccp/ingest/event_log.py`, the normalize consumer, the entities model, `ccp/identity/scoring.py`, `resolver.py`.
6. **Verdicts and bitemporal state.** `ccp/evaluators/verdict.py`, `0005_state.sql`, `storage/bitemporal.py`, the state writer, the state reader.
7. **Knowledge and tier 1 rules.** `ccp/knowledge/graph.py`, `tests.yaml`, the GH-1 rule, the AWS-1 declarative check, `ccp/evaluators/deterministic/declarative.py`.
8. **Tier 2: extract, then rule.** `ccp/evaluators/runner.py`, `tier2.py`, `ccp/retrieval/hybrid.py`, `citations.py`, the HR-3 rule.
9. **One read-only tool surface.** `ccp/agent/tools.py`, `allowlists.py`, `authz.py`, `prompt_guard.py`, `ccp/agent/rest.py`.
10. **Agents that only propose.** `ccp/agent/core.py`, the investigator, policy and copilot graphs, `ccp/llm/budget.py`.
11. **The human approval stage.** `ccp/approvals/store.py`, `actions.py`, `ccp/agent/decisions.py`, `ccp/questionnaires/pipeline.py`, `ccp/api/ui.py`.
12. **Suggest-only remediation.** `ccp/lookups/runner.py`, `ccp/investigation/written.py`, `ccp/knowledge/fixes.yaml`, `ccp/remediation/fixes.py`, `confirm.py`.
13. **Evals, method cards and autonomy.** `ccp/evals/runner.py`, `judge.py`, `regress.py`, `publish.py`, `ccp/compliance_state/autonomy.py`.
14. **Audit chain, packages and observability.** `0043_audit_chain.sql`, `ccp/audit/checkpoints.py`, `ccp/compliance_state/packages.py`, `auditors.py`, `ccp/observability/steps.py`.
15. **Deployment, cells and scale.** `Dockerfile`, `deploy.yml`, `ccp/cells.py`, `infra/terraform/scale.tf`, `ccp/storage/partitions.py`.

## 5. File map

The most connected files in each layer, plus the files behind newer capabilities. The dashboard has the rest.

**CLI & Web API**
- `ccp/cli.py`: the `ccp` CLI; every command group mounts here.
- `ccp/api/app.py`: the `/v1` REST API (onboarding, approvals, findings and reports, fix suggestions, autonomy levels, method cards, auditor grants and workspace, exceptions, members, settings, the agent tool surface).
- `ccp/api/ui.py`: the reviewer UI with OIDC/PKCE sign-in, the inbox with hash-bound decisions, findings, reports, verdicts and live run views with a Stop button.
- `ccp/api/reasons.py`: maps reason codes to plain explanations and the one action that clears each.

**Evaluation Harness**
- `ccp/evals/runner.py`, `targets.py`: run each target (k times when a model is involved) against the registry of everything a golden case can invoke.
- `ccp/evals/judge.py`, `calibration.py`: the LLM judge, with fenced candidates and bias checks, and its calibration against human labels.
- `ccp/evals/regress.py`, `production.py`, `relabel.py`: regression gates, the daily canary and nightly judged sample, and expert relabels.
- `ccp/evals/publish.py`: method cards for verdicts and fix suggestions, and quarterly quality snapshots per AI workflow.
- `ccp/evals/drafting.py`, `mapping.py`: model-drafted declarative checks behind four code gates, and the framework-mapping propose, review and eval commands.
- `ccp/replay/rebuild.py`: rebuilds stored model requests for verdicts, investigations, gap reports and Copilot answers.

**Agent Workflows**
- `ccp/agent/core.py`: GraphSpec and NodeSpec, with mandatory retry and timeout policies; every graph is built here.
- `ccp/agent/tools.py`: the typed read-only tool registry and gateway.
- `ccp/investigation/runner.py`, `written.py`: the Investigator, and investigations written in code when the rule explains the failure.
- `ccp/lookups/runner.py`: reads Config history, CloudTrail and Terraform about each failing resource and lands the responses raw.
- `ccp/remediation/fixes.py`, `suggestions.py`, `confirm.py`: render and check fixes offline, keep suggestions append-only, and confirm or reopen them from later reads.
- `ccp/lookups/inventory.py`, `coverage.py`, `playbooks.py`: AWS inventory snapshots, the coverage map and service playbooks on gaps.
- `ccp/questionnaires/pipeline.py`, `store.py`, `send.py`: the fixed answering pipeline, the answer library and staleness, and person-initiated sending.
- `ccp/compliance_state/run.py`, `writer.py`, `reader.py`: the per-tenant pipeline run and the state table's writer and reader.
- `ccp/compliance_state/packages.py`, `auditors.py`, `attest.py`: as-of audit packages, the auditor workspace and the attestation record.
- `ccp/compliance_state/autonomy.py`: autonomy levels, the review sample and the exposure budget.
- `ccp/tasks/store.py`, `runner.py`, `launcher.py`: long compliance tasks, their passes and their fan-out into items.

**Evaluators**
- `ccp/evaluators/runner.py`: routes each (test, subject) by tier and skips work already done.
- `ccp/evaluators/verdict.py`: the Verdict and Citation schema and the hashed evaluation key.
- `ccp/evaluators/tier2.py`, `citations.py`: one retrieval, one schema-bound extraction, then citation verification.
- `ccp/evaluators/deterministic/declarative.py`: the total interpreter for declarative YAML checks.
- `ccp/evaluators/feedback.py`, `adequacy.py`: code feedback on each upload, and the adequacy verifier with its calibration against auditors.
- `ccp/evaluators/impact.py`: maps entity changes to the evaluations they affect.
- `ccp/evaluators/rule_changes.py`, `rederive.py`: rule amendments and corrections with history kept, and the nightly tier 1 sentinel.

**Knowledge, Retrieval & LLM**
- `ccp/llm/client.py`: the single `LLMClient` every model call goes through, with a factory that picks the Anthropic or Bedrock client.
- `ccp/knowledge/graph.py`: the Framework > Requirement > Control > TestSpec graph and the tier router.
- `ccp/knowledge/fixes.yaml`, `fixes.py`, `lookups.yaml`: the versioned fix catalogue and the per-test lookup table.
- `ccp/knowledge/mapping.py`: code-scored framework-to-control mapping.
- `ccp/knowledge/service_classes.yaml`, `admissibility.yaml`: AWS service classes and their intents, and per-type admissibility rules.
- `ccp/retrieval/segments.py`, `admission.py`: deterministic segments and whether an upload counts for retrieval.
- `ccp/retrieval/index.py`, `generations.py`, `embeddings.py`, `hybrid.py`: indexing, insert-only index generations, the embedding seam and hybrid search.
- `ccp/llm/release.py`, `budget.py`: release bundle ids and hard run budgets.

**Ingestion & Identity**
- `ccp/connectors/base.py`: the connector contract and `SourceEvent`.
- `ccp/connectors/aws/connector.py`, `onboarding.py`: reads through the customer's read-only role, onboarded with an external id.
- `ccp/ingest/landing.py`, `raw_zone.py`, `event_log.py`, `consumers.py`, `worker.py`: landing, the Object Lock raw zone, the Postgres queue, consumer base classes and the fair worker loop.
- `ccp/ingest/connections.py`: connections as versioned data, probed daily.
- `ccp/normalize/repository.py`: append-only, idempotent entity observations.
- `ccp/identity/resolver.py`: scores, auto-links or opens a review.
- `ccp/audit/checkpoints.py`, `verify.py`, `package_verify.py`: signed checkpoints, the chain verifier and the standalone offline package verifier.

**Platform Foundation**
- `ccp/config.py`, `ccp/tenancy.py`, `ccp/types.py`: settings, tenant identity and shared types.
- `ccp/storage/scoped.py`, `db.py`: the tenant query surface; the app, read-only agent and replica pools, lag-bounded read routing and the migration runner.
- `ccp/cells.py`, `ccp/storage/partitions.py`: deployment cells, and moving a large tenant into its own partition.
- `ccp/approvals/store.py`: the append-only approval record.
- `ccp/security/prompt_guard.py`: the only way evidence reaches a prompt.
- `ccp/observability/steps.py`: one JSON record per step, with OpenTelemetry GenAI names, mirrored to Langfuse.

**Infrastructure & Configuration**
- `infra/terraform/services.tf`, `storage.tf`, `database.tf`, `scale.tf`, `monitoring.tf`: ECS Fargate, S3 with Object Lock and per-tenant KMS keys plus the audit-account checkpoint bucket, RDS PostgreSQL 17, the read replica and PgBouncer, and alarms.
- `infra/cloudformation/ccp-read-role.yaml`: the read-only role a customer deploys for the AWS connector.
- `.github/workflows/ci.yml`, `deploy.yml`, `nightly-evals.yml`: checks, gated deploys and keyed nightly evals.
- `config/*.yaml`: runtime knobs (tool policy, identity bands, retrieval, eval gates, SLOs, task kinds, cells, autonomy, the evaluation method, the publication policy and more).

## 6. Complexity hotspots

Ninety-two files are rated complex (196 moderate, 244 simple). These are the most connected ones; read the tests that cover them before changing them. `ccp/storage/scoped.py` is rated simple but has the most dependants in the repo, so treat it with the same care.

| File | Why to be careful |
|---|---|
| `ccp/cli.py` | Mounts every command group; most connected file in the repo |
| `ccp/storage/db.py` | Every pool, PgBouncer-safe session settings, lag-bounded replica reads, role hardening and the migration runner |
| `ccp/config.py` | Every setting; the only reader of the environment |
| `ccp/evaluators/verdict.py` | Changing the evaluation key changes what counts as the same verdict |
| `ccp/evaluators/runner.py` | Tier routing, verdict reuse, budget and completeness guards for every evaluation |
| `ccp/evaluators/tier2.py` | Retrieval, extraction, citation checks and the rule in one path |
| `ccp/investigation/runner.py` | Investigator jobs, target revalidation, fix rendering and evidence requests meet here |
| `ccp/agent/core.py` | Every graph is built on it; retry, timeout and checkpoint behaviour is shared |
| `ccp/agent/tools.py` | The tool gateway: scopes, allowlists, truncation and tenant binding |
| `ccp/knowledge/graph.py` | The catalogue models and the tier router; almost every layer reads it |
| `ccp/api/app.py` | Every `/v1` endpoint, including auditor access that is decided by grants, not membership |
| `ccp/observability/steps.py` | Every step record, its masking and its model cost |
| `ccp/agent/tier3.py` | Budgets, checkpoints and the human interrupt |
| `ccp/evals/targets.py` | Every golden case depends on its target signatures |
| `ccp/tasks/launcher.py`, `store.py` | Long-running work, spend tracking and fan-out |
| `ccp/approvals/store.py` | The approval record; fails closed by design |
| `ccp/ingest/event_log.py`, `consumers.py` | The queue: ordering, leases, retries and dead letters |
| `ccp/llm/client.py` | Every model call, its provider and its retry behaviour |
| `ccp/lookups/runner.py`, `ccp/remediation/fixes.py` | What is read about a failing resource, and which rendered fixes pass the offline checks |
| `ccp/compliance_state/autonomy.py` | Decides which drafts skip a person's review |
| `ccp/compliance_state/packages.py` | What an auditor receives; it must stay verifiable offline |

## 7. Business capabilities

`/understand-domain` built a separate map of what the product does: 18 domains, 81 flows and 550 steps, in `.ua/domain-graph.json`. The dashboard opens on this view. Each domain below lists its flows with their entry points, then its main files.

### Evidence Ingestion
Collects evidence from GitHub, HRIS, Okta, AWS and manual uploads. Every pull is a recorded sync run with per-scope outcomes, and every event or provider response lands once in the raw zone before the event log announces it.
- Ingest Connector Evidence: `ccp ingest run`, also the scheduled sync job.
- Check Connection Health: `ccp connections check`, `ccp connections list`, also the daily connection-check job.
- Onboard an AWS Account: `ccp aws onboard`, `ccp aws verify`.
- Transcribe Scanned Uploads: `ccp ingest transcribe`, also the scheduled transcription job.
- Run Connector Conformance: `ccp connectors conformance`.

Main files: `ccp/ingest/connections.py`, `ccp/retrieval/transcribe.py`, `ccp/connectors/conformance.py`, `ccp/ingest/landing.py`, `ccp/connectors/aws/onboarding.py`.

### Event Processing & Scheduling
The Postgres event log used as a queue, with ordered and parallel topics, leased claims, retries, parking and dead letters. A fair dispatcher serves each consumer across tenants, and per-tenant schedules fire jobs exactly once.
- Dispatch Consumer Work Across Tenants: `ccp worker run`.
- Fire Scheduled Jobs: the scheduler's pass over per-tenant wall-clock schedules.
- Replay Dead Letters: `ccp worker dead-letters`, `ccp worker replay`, `ccp worker unpark`.

Main files: `ccp/ingest/event_log.py`, `schedules.py`, `ops.py`, `dispatch.py`, `consumers.py`.

### Entity Normalization & Identity Resolution
Pure mappers turn raw events into append-only observations of persons, access grants, assets, evidence and vendors. Identity resolution decides which accounts belong to the same person and sends uncertain matches to a reviewer.
- Normalize Raw Events: `ccp normalize run` (ordered consumers on the `raw.<connector>` topics).
- Resolve Identities: `ccp identity resolve`, also run before each evaluation.
- Review an Identity Match: `ccp identity review` (identity-merge approval items).

Main files: `ccp/identity/resolver.py`, `ccp/identity/scoring.py`, `ccp/normalize/consumer.py`, `ccp/ingest/raw_zone.py`, `ccp/normalize/github_mapper.py`.

### Evidence Retrieval & Indexing
Chunks current, admissible evidence, optionally adds context, embeds it into versioned index generations and serves hybrid full-text and pgvector search with reranking. Calibration and per-type gates decide when a new embedder may serve.
- Refresh the Retrieval Index: `ccp index refresh`, `ccp index rebuild`, also part of `ccp run`.
- Hybrid Evidence Search: `ccp index search` and the `search_evidence` agent tool.
- Re-embed into a New Generation: `ccp index reembed`.
- Calibrate Retrieval and Check Gates: `ccp index calibrate`, `ccp index gate`.

Main files: `ccp/retrieval/generations.py`, `hybrid.py`, `index.py`, `config.py`, `reembed.py`.

### Evidence Admissibility & Adequacy
Feedback seconds after an upload lands, then an adequacy verifier held in shadow until it agrees with auditors.
- Give Feedback on an Upload: the `evidence-feedback` consumer on the entities topic; `ccp evidence feedback`.
- Verify an Upload's Adequacy: the parallel consumer on the adequacy topic; `ccp evidence verify`.
- Calibrate the Adequacy Verifier against Auditors: `ccp evidence label`, `ccp evidence calibrate`.

Main files: `ccp/evaluators/adequacy.py`, `ccp/evaluators/feedback.py`, `ccp/retrieval/segments.py`, `ccp/evaluators/evidence_cli.py`, `ccp/retrieval/admission.py`.

### Compliance Knowledge & Readiness Reporting
The typed catalogue of frameworks, controls and 14 tests (GH-1..5, HR-1..5, AWS-1..3, IDP-1), with checks, the impact table, the lookup table and the fix catalogue. It rolls state up into readiness reports, governs rule changes, proposes framework mappings and takes model-drafted checks only through code gates and a reviewer.
- Load the Knowledge Catalogue: `ccp knowledge load`, `ccp knowledge show`.
- Framework Readiness Report: `ccp report`, `GET /v1/tenants/{tenant}/reports/{framework}`, the `/ui` report page.
- Preview and Correct a Rule Change: `ccp rules preview`, `ccp rules correct`, `ccp rules notices`.
- Propose and Review a Framework Mapping: `ccp knowledge map propose`, `ccp knowledge map review`, `ccp knowledge map eval`.
- Draft a Declarative Check with a Model: `ccp knowledge checks draft`, `ccp knowledge checks review`.

Main files: `ccp/evals/drafting.py`, `ccp/knowledge/graph.py`, `ccp/evaluators/rule_changes.py`, `ccp/evals/mapping.py`, `ccp/knowledge/mapping.py`.

### Compliance Evaluation
Produces a verdict for every (test, subject) pair: tier 1 rules, tier 2 extract-then-rule, and tier 3 workflows that stop at a human gate when confidence is low. Entity changes, staleness clocks and full runs trigger re-evaluation.
- Run a Full Evaluation: `ccp evaluate run`, also the scheduled full-run job.
- Tier 2 Extract-then-Rule Evaluation: the evaluations topic consumer (tier 2 tests).
- Tier 3 Agentic Evaluation with Human Gate: the evaluations topic consumer (tier 3 tests).
- Impact-Driven Re-evaluation: the ordered consumer on the entities topic.
- Clock-Driven Staleness Re-evaluation: the clock job; `ccp evaluate clock`.

Main files: `ccp/evaluators/impact.py`, `ccp/evaluators/runner.py`, `ccp/agent/tier3.py`, `ccp/evaluators/tier2.py`, `ccp/agent/graphs/tier3.py`.

### Compliance State & Exceptions
The append-only bitemporal state table, the end-to-end per-tenant pipeline, findings views and time-boxed control exceptions decided through the approval stage.
- Run the Full Tenant Pipeline: `ccp run`.
- Write Bitemporal Compliance State: the ordered consumer on the verdicts topic.
- Query Compliance State As Of: `ccp state asof`, `ccp state current`, `ccp state history`.
- Request a Control Exception: `POST /v1/tenants/{tenant}/exceptions`, `ccp approvals exceptions request`.
- Review Findings and Verdict Explanations: `GET /v1/tenants/{tenant}/findings`, the `/ui` findings and verdict pages.

Main files: `ccp/compliance_state/writer.py`, `ccp/storage/bitemporal.py`, `ccp/compliance_state/run.py`, `ccp/compliance_state/reader.py`, `ccp/storage/exceptions.py`.

### AWS Coverage & Service Playbooks
Coverage measured from what the customer's AWS account holds, with playbooks on the gaps (see section 3).
- Take an AWS Inventory Snapshot: the daily inventory job; `ccp coverage inventory`.
- Build the Coverage Map: `ccp coverage map`.
- Classify a New AWS Resource Type: the Monday registry-diff job; `ccp coverage registry`, `ccp coverage classify`.
- Run Service Playbooks on Coverage Gaps: the daily playbooks job; `ccp coverage playbooks`.

Main files: `ccp/lookups/inventory.py`, `ccp/lookups/coverage_cli.py`, `ccp/lookups/playbooks.py`, `ccp/knowledge/service_classes.py`, `ccp/connectors/aws/connector.py`.

### AI Compliance Agents
LangGraph agents on one core and one read-only tool surface. The Investigator explains failures the rule cannot and picks catalogue fixes by id; the Policy Agent reports gaps and drafts policy; the Copilot answers questions with verified citations. People can watch and stop any run.
- Investigate a Failing Verdict: the verdicts topic consumer or the lookups job; `ccp investigate run`.
- Policy Gap Analysis and Draft: `ccp policy gap`, `ccp policy draft`.
- Ask the Compliance Copilot: `ccp copilot` (a REPL, or `--ask`).
- Call the Read-Only Tool Surface over REST: `GET /v1/tenants/{tenant}/tools`, `POST /v1/tenants/{tenant}/tools/{name}`.
- Watch and Stop an Agent Run: the `/ui` run page and its Stop button.

Main files: `ccp/investigation/runner.py`, `ccp/policy/runner.py`, `ccp/api/ui.py`, `ccp/agent/graphs/policy.py`, `ccp/policy/schema.py`.

### Long Compliance Tasks
Audit preparation and questionnaire tasks as typed rows. Code plans the items and settles what verdicts already answer; agent runs read the rest under spend and deadline caps.
- Plan a Compliance Task: `ccp tasks plan`.
- Advance a Task Pass: `ccp tasks advance`, also the scheduled advance-open job.
- Fan Out Task Items: the `task_items` topic consumer (fan-out mode).
- Answer a Waiting Task Item: `ccp tasks answer`.

Main files: `ccp/tasks/launcher.py`, `ccp/tasks/runner.py`, `ccp/tasks/planners.py`, `ccp/agent/graphs/task_item.py`, `ccp/tasks/cli.py`.

### Security Questionnaires
A fixed pipeline, not an agent: map each question to a tested control, reuse a still-true approved answer, take yes or no from current verdicts, draft with two tool-less model steps over fenced evidence, and check the draft in code. A person approves and sends. Answers go stale when what they rest on changes, and every send is attested.
- Draft Questionnaire Answers: `ccp questionnaires create`, `ccp questionnaires answer`.
- Approve and Send a Questionnaire: `ccp questionnaires approve` or `reject`, then `ccp questionnaires send`.
- Sweep Stale Answers: the daily answer-staleness job; `ccp questionnaires stale`, `ccp questionnaires metrics`.

Main files: `ccp/questionnaires/pipeline.py`, `ccp/questionnaires/store.py`, `ccp/questionnaires/send.py`, `ccp/questionnaires/cli.py`, `ccp/compliance_state/attest.py`.

### Human Approval Stage & Review Autonomy
One append-only approval stage gates every pause and action (tier 3 decisions, exceptions, identity merges, scope changes, evidence requests), each decision bound to the exact payload shown. Autonomy levels decide which AI drafts may skip review.
- Decide an Approval Item: the `/ui` inbox, `POST /v1/tenants/{tenant}/approvals/{id}/approve`, `ccp approvals approve` or `reject`.
- Apply a Tier 3 Reviewer Decision: the `applies` topic consumer.
- Break-Glass Approval: `ccp approvals break-glass`, `ccp approvals audit`.
- Set a Decision Class's Autonomy Level: `POST /v1/tenants/{tenant}/autonomy`.
- Decide Whether a Draft Skips Review: a questionnaire library answer, and the hourly `autonomy_watch` job.

Main files: `ccp/compliance_state/autonomy.py`, `ccp/approvals/store.py`, `ccp/agent/decisions.py`, `ccp/api/ui.py`, `ccp/approvals/actions.py`.

### Fix Suggestions & Confirmation
CCP suggests fixes and applies none. The five-step sequence is in section 3.
- Look Up a Failing Resource: the `lookups-queue` and `lookups` consumers on the verdicts topic; `ccp lookups run`.
- Write the Investigation in Code: the investigations job, for verdicts the rule explains.
- Render and Check a Fix Offline: called for each fix choice by the code-written and Investigator paths.
- Accept or Dismiss a Fix Suggestion: `ccp fixes list`, `show`, `accept`, `dismiss`; `POST /v1/tenants/{tenant}/fixes/{id}/accept` or `/dismiss`.
- Confirm or Reopen a Fix from a Later Read: the `fix-confirmations` consumer on the verdicts topic; `ccp fixes confirm`.
- Opt-in Live GitHub Access Removal (Off by Default): `ccp remediate`.

Main files: `ccp/remediation/confirm.py`, `ccp/lookups/runner.py`, `ccp/remediation/fixes.py`, `ccp/investigation/written.py`, `ccp/knowledge/fixes.py`.

### Tenants, Cells, Membership & Sign-in
Tenants are data: onboarded without a deploy, with versioned settings and role-based memberships. Members sign in through OIDC with PKCE. Each tenant lives in one cell, behind PgBouncer, a lag-bounded read replica and, for the largest tenants, its own partitions.
- Onboard a Tenant: `POST /v1/tenants`, `ccp tenants create`.
- Change Tenant Settings: `PATCH /v1/tenants/{tenant}/settings`, `ccp tenants set`.
- Sign In a Member: `GET /ui/login`, then the OIDC callback.
- Manage Tenant Members: `PUT` or `DELETE /v1/tenants/{tenant}/members/{principal}`, `ccp members grant` or `revoke`.
- Serve a Tenant in Its Cell at Scale: process start with `CCP_CELL`, and admin partition maintenance.

Main files: `ccp/auth/members.py`, `ccp/api/app.py`, `ccp/api/ui.py`, `ccp/tenants/scope.py`, `ccp/tenants/onboarding.py`.

### Model Quality & Eval Harness
Measures every model-backed step, from golden and adversarial suites and a calibrated judge to production sampling and quarterly quality snapshots.
- Run the Eval Suite: `ccp evals run`.
- Calibrate the LLM Judge: `ccp evals calibrate`.
- Regression Gate on a Release: `ccp evals regress`, in the nightly-evals workflow.
- Production Quality Sampling: the scheduled canary, judged-sample and monthly relabel jobs; `ccp evals relabel`.
- Agent Harness Gate: `ccp agent harness-gate`, `ccp agent harness-report`.
- Publish a Quality Snapshot: `ccp evals publish`.

Main files: `ccp/evals/regress.py`, `ccp/evals/publish.py`, `ccp/evals/runner.py`, `ccp/evals/calibration.py`, `ccp/evals/relabel.py`.

### Audit Integrity & Replay
Lets an auditor trust the records: a per-tenant hash chain with signed, write-once checkpoints, re-hashing of raw-zone objects, replay of any stored model request, and a nightly sentinel that re-derives tier 1 verdicts from what they read.
- Sign an Audit Checkpoint: the hourly audit-checkpoint job; `ccp audit checkpoint`.
- Verify the Audit Chain: `ccp audit verify`, also a scheduled job.
- Audit the Raw Zone: `ccp audit raw`.
- Replay a Stored Model Call: `ccp replay <record-id>`.
- Nightly Tier-1 Sentinel: the nightly sentinel job; `ccp evaluate sentinel`.

Main files: `ccp/audit/verify.py`, `ccp/replay/rebuild.py`, `ccp/cli.py`, `ccp/audit/checkpoints.py`, `ccp/evaluators/rederive.py`.

### Audit Packages & Auditor Assurance
What CCP asserts outside itself: as-of audit packages an auditor verifies offline, a time-boxed read-only auditor workspace, the attestation record, and method cards.
- Build an As-Of Audit Package: `ccp audit package build --framework F --as-of T --by NAME`, with `--corrects ID` for a correction.
- Verify an Audit Package Offline: `python package_verify.py <folder> --public-key <pem>`, or `ccp audit package verify`.
- Grant an Auditor Time-Boxed Access: `POST /v1/tenants/{tenant}/auditor-grants`, revoked at `.../auditor-grants/{id}/revoke`.
- Read Packages in the Auditor Workspace: `GET /v1/tenants/{tenant}/auditor/packages` and `.../{id}/files/{name}`.
- Explain a Decision with a Method Card: `GET /v1/tenants/{tenant}/verdicts/{id}/card`, `GET /v1/tenants/{tenant}/fixes/{id}/card`, `ccp evals card`.

Main files: `ccp/compliance_state/packages.py`, `ccp/audit/package_verify.py`, `ccp/compliance_state/auditors.py`, `ccp/api/app.py`, `ccp/compliance_state/attest.py`.
