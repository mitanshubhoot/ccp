# Evidence: ccp Investigator Run, Tools to Approval

Diagram: `ccp-investigator-run.html` (source `candidate.json`).
Code revision: `3386899764f95dfc77030e010c829838198318d1` on `main`. The repository has no git remote, so the
diagram carries no source links; this file holds the evidence instead. Paths are relative to the repository
root and line numbers are inclusive.

Current receipts: `review-2/` (the files at the folder root are from the first run, before the re-evaluate fix).

The Investigator is drawn because it is the one agent whose run uses both the tool loop and the
remediation approval end to end. The Policy Agent and the Copilot reach the same approval stage
(see the last card and the architecture diagram).

## Nodes

| Node | What the code shows | Evidence |
|---|---|---|
| Run starts, tag "verdicts topic" | Ordered consumer of `verdicts`; skips anything but fail and needs_review, and verdicts already investigated; budget check; thread id starts with the tenant | `ccp/investigation/runner.py:41`, `ccp/investigation/runner.py:68-101`, `ccp/investigation/runner.py:208-239`, `ccp/storage/checkpoints.py:83-85` |
| load_context, "pure tool calls" | Calls tools directly, no model | `ccp/agent/graphs/investigator.py:82-146` |
| Direct reads | `get_verdict`, `get_current_state`, `get_control` per control, `get_access_grants` for person subjects | `ccp/agent/graphs/investigator.py:84-145`, `ccp/agent/tools.py:338-371`, `ccp/agent/tools.py:392-434` |
| gather, tag "8 calls max" | The shared bounded tool loop on the investigator allowlist | `ccp/agent/graphs/investigator.py:160-168`, `ccp/agent/core.py:279-384` (`max_calls=8` at line 288) |
| Allowlisted call, "11 tools via ScopedDB", tag "secrets redacted" | Tools bound from the investigator allowlist; arguments validated, extra fields forbidden; chunk text redacted | `ccp/agent/allowlists.py:9-25`, `ccp/investigation/runner.py:58-61`, `ccp/agent/tools.py:179-196`, `ccp/agent/tools.py:501-533` |
| Off-list tool | Answered with an error result and a `tool_not_permitted` guardrail event, never dispatched | `ccp/agent/core.py:339-341` |
| diagnose, "one structured call" | Fenced evidence in, `InvestigationDraft` fields out | `ccp/agent/graphs/investigator.py:177-185`, `ccp/agent/core.py:200-237` |
| Anthropic API | Reached only through `LLMClient` | `ccp/llm/client.py:283-287`, `ccp/llm/anthropic_client.py:129-146` |
| verify + bound_actions | Quotes checked against gathered chunks; actions outside the catalogue or missing target fields dropped; finalize computes confidence | `ccp/agent/graphs/investigator.py:187-250`, `ccp/agent/graphs/investigator.py:252-282` |
| Persist + propose, "targets checked again" | Runner re-validates targets, persists the Investigation, writes proposal rows | `ccp/investigation/runner.py:129-188`, `ccp/investigation/runner.py:190-205` |
| Approval item, "remediation, evidence_request", tag "payload hash" | Each proposal opens a `remediation` item with a payload hash; missing evidence opens an `evidence_request` item | `ccp/remediation/proposals.py:82-126`, `ccp/approvals/store.py:22-24`, `ccp/approvals/store.py:69-95`, `ccp/investigation/runner.py:175-186` |
| Operator decides, tag "typed reviewer name" | `ccp approvals approve` or `reject`; a reviewer name is required | `ccp/approvals/cli.py:83-128`, `ccp/approvals/store.py:98-115` |
| Proposal rejected | Rejecting a remediation item marks the proposal `rejected` | `ccp/approvals/actions.py:62-86` |
| remediate execute, tag "live: GitHub call" | Dry run records the planned call; live needs all five gates and captures the pre-state first | `ccp/remediation/cli.py:86-110`, `ccp/remediation/runner.py:101-202`, `ccp/remediation/gates.py:53-84` |
| Re-evaluate, tag "no CLI command" | `reevaluate()` re-pulls, normalizes, runs impact, evaluation and state; only the e2e test calls it | `ccp/remediation/reevaluate.py:35-54`, `tests/e2e/test_remediation_loop.py:86`, `ccp/remediation/cli.py:1-2` |

## Edges

| Edge | Evidence |
|---|---|
| Run starts → load_context, "verdict id" | `ccp/investigation/runner.py:85-112`, `ccp/agent/graphs/investigator.py:303` |
| load_context → gather | `ccp/agent/graphs/investigator.py:304` |
| gather → diagnose, "chunks" | `ccp/agent/graphs/investigator.py:59-68`, `ccp/agent/graphs/investigator.py:182`, `ccp/agent/graphs/investigator.py:305` |
| diagnose → verify + bound_actions, "draft" | `ccp/agent/graphs/investigator.py:306-308` |
| verify + bound_actions → Persist + propose, "typed proposal" | `ccp/investigation/runner.py:110-115` |
| Persist + propose → Approval item, "open item" | `ccp/investigation/runner.py:153-156`, `ccp/investigation/runner.py:196-205`, `ccp/remediation/proposals.py:113-123` |
| Approval item → Operator decides | `ccp/approvals/cli.py:83-128` |
| Operator decides → remediate execute, "approved: gate 5" | `ccp/approvals/actions.py:55-57`, `ccp/remediation/runner.py:118`, `ccp/remediation/gates.py:77-84` |
| remediate execute → Re-evaluate, "operator re-runs" | No code links them: `execute` never calls `reevaluate()` (`ccp/remediation/runner.py:101-202`) and there is no CLI command for it (`ccp/remediation/cli.py:1-2`). From the CLI the operator re-runs `ccp run` (`ccp/cli.py:80-97`); with `--source fixture` the fixtures do not change, so only a live pull shows the effect. |
| load_context → Direct reads, "4 reads" | `ccp/agent/graphs/investigator.py:84-145` |
| gather → Allowlisted call, "tool_use" | `ccp/agent/core.py:311-352` |
| diagnose → Anthropic API, "extract" | `ccp/agent/core.py:237` |
| gather → Off-list tool, "off-list name" | `ccp/agent/core.py:339-341` |
| Operator decides → Proposal rejected, "reject" | `ccp/approvals/cli.py:112-128`, `ccp/approvals/actions.py:79-85` |

## Groups and cards

| Claim | Evidence |
|---|---|
| The graph runs as a checkpointed LangGraph thread | `ccp/investigation/runner.py:64-65`, `ccp/investigation/runner.py:83-86`, `ccp/storage/checkpoints.py:1-11` |
| The approval stage is shared by every agent | `ccp/approvals/store.py:16-18`, `ccp/policy/runner.py:225-236`, `ccp/copilot/runner.py:442-456` |
| The graph has no write tool; the runner persists and opens items | `ccp/agent/graphs/investigator.py:12`, `ccp/investigation/runner.py:1-6` |
| Tool surface: direct calls, 11 investigator tools, off-list handling, tenant from the closure | `ccp/agent/graphs/investigator.py:84-145`, `ccp/agent/allowlists.py:23-25`, `ccp/agent/core.py:339-341`, `ccp/agent/tools.py:1-8` |
| Live action gates; dry run records the planned call | `ccp/remediation/gates.py:1-9`, `ccp/remediation/runner.py:136-141` |
| Policy Agent opens `policy_draft`; Copilot opens `remediation` after an explicit yes, investigating first when needed | `ccp/policy/runner.py:217-236`, `ccp/copilot/runner.py:413-466`, `ccp/copilot/cli.py:60-62` |

## Where the code differs from spec.md and README.md

- README.md:45 lists `ccp remediate simulate` and `reevaluate`. Neither exists (`ccp/remediation/cli.py:1-2`);
  `simulate()` and `reevaluate()` are reachable only from tests, so the CLI has no way to close the loop in
  fixture mode.
- spec.md:632 declares a parallel `investigations` topic; nothing produces or consumes it
  (`ccp/ingest/topics.py:25`). The Investigator reads `verdicts` as an ordered consumer (spec assumption A-74).

## Left out of this diagram

- `ccp remediate rollback` (re-invites with the recorded permission, `ccp/remediation/runner.py:243-289`).
- The rule-fallback proposals from `ccp remediate propose` (`ccp/remediation/runner.py:35-71`), which open
  the same kind of approval item without the Investigator.
- Budget, retry and timeout policies on each graph node (`ccp/agent/core.py:47-143`).
