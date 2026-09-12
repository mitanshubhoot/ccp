# TODOS

## Evaluators

### Citation entailment as a second verification stage behind a flag

**What:** After the deterministic field-in-quote support check, an optional per-citation entailment check (a small NLI model locally, or one cheap structured Haiku call) that can flip `verified` to false when the quote is real but does not support the extracted value.

**Why:** Extract-then-rule makes most support checks deterministic (a timeline string, a date, or a name must appear in the quote), but boolean-ish fields such as "this certificate is about this person" can be backed by a real quote that says something else. The labelled calibration set deliberately contains that case, so it is a known residual, not a hypothetical.

**Context:** Spec v1.3, Layer 7 step 5 and assumption A-17, ship support-precision measured by the deterministic check only. Whether the residual error justifies a model-checks-model stage (which A-17 declined on principle) is only knowable once that number exists. Start from `ccp/evaluators/citations.py` and the support-precision scorer in `ccp/evals/scorers/`; add the stage behind `CITATION_ENTAILMENT=1` so the eval runner can report support-precision with it off and on. Raised by the outside-voice review of 2026-09-11 (point 5).

**Effort:** M
**Priority:** P3
**Depends on:** The support-precision metric from spec v1.3 Layer 11 landing and producing a number on the golden set.

## Evals

### Grow the judge calibration set to 100 with a second annotator

**What:** Expand `evals/labeled/` from 20 to 100 human-labelled cases, with two annotators labelling a 30-case overlap and human-human kappa published as the ceiling the judge is measured against.

**Why:** At n = 20, per-criterion kappa carries a confidence interval of roughly plus or minus 0.4, and without a human agreement ceiling a low kappa cannot be told apart from an ambiguous rubric. Calibration only becomes decision-grade with a larger set and a ceiling.

**Context:** Spec v1.3 keeps 20 cases for this pass with bootstrap intervals, rubric-paraphrase robustness in place of temperature-zero self-consistency, and an optional second annotator on a 10-case overlap. The growth mechanism is `ccp evals capture <trace_id>`, which turns any trace into a golden case; each captured case a human labels grows this set. The labelling is human time that AI does not compress, so it is scheduled separately from the build. Start from `evals/labeled/README.md` and `ccp evals calibrate`. Raised by the outside-voice review of 2026-09-11 (point 15) and decided as D22.

**Effort:** L (human labelling, roughly three days)
**Priority:** P3
**Depends on:** `ccp evals capture` (spec v1.3 Layer 11); a second annotator being available.

## Observability

### OpenTelemetry OTLP exporter alongside the Langfuse SDK

**What:** Emit the step log as OpenTelemetry spans through an OTLP exporter, using the GenAI semantic convention attribute names the JSONL schema already adopts, in addition to the Langfuse SDK path; Langfuse Cloud accepts OTLP directly.

**Why:** One instrumentation, any backend. The conventions are still in development status but are read by Langfuse, Datadog, and the cloud vendors; the attribute names are adopted in this pass, so the exporter is a rename-free follow-up that removes vendor lock-in.

**Context:** Spec v1.3 Layer 12 names the JSONL fields `gen_ai.*` where a convention exists and `ccp.*` otherwise. The exporter is the deferred piece; when it lands, the Langfuse SDK emitter can be retired. Start from `ccp/observability/steps.py`. Watch for attribute renames before the conventions stabilise.

**Effort:** S
**Priority:** P3
**Depends on:** None.

## Copilot

### Long-term memory via LangGraph Store, namespaced by tenant and user

**What:** Give the Copilot cross-session memory using LangGraph's Store with a `(tenant_id, user_id)` namespace, backed by Postgres under the same RLS pattern as the checkpoint tables, so per-user context and preferences persist across REPL sessions.

**Why:** A production copilot remembers. A compliance copilot that remembers must isolate memory per tenant and per user, which is a second isolation surface with its own poisoning risk; both are on the security checklist and neither has a home while memory does not exist.

**Context:** Spec v1.3 keeps the Copilot stateless across sessions (one REPL session is one checkpoint thread) because there is no authenticated user identity in this build; a `user_id` would be a CLI flag anyone can set. When an identity exists, copy the checkpoint-table RLS pattern (spec v1.3 Layer 8, A-40) to the Store's table, add a memory-poisoning case to the adversarial suite, and extend S14 to the Store. Start from `ccp/copilot/runner.py` and `ccp/storage/rls.sql`.

**Effort:** M
**Priority:** P4
**Depends on:** An authenticated user identity, which is a non-goal of this pass.

## Completed
