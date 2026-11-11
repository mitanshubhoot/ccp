# Verifying with real credentials

Everything below is built and tested offline: stub models, recorded API responses, a local
identity provider, and local stand-ins for AWS. None of it counts as verified until it has
run against the real service with the credential named. This list grows with each roadmap
step; work through it when the credentials are available.

## Anthropic API key (`ANTHROPIC_API_KEY`)

- [ ] Upload a real console screenshot and a scanned access review with the worker
      running: `GET /v1/tenants/{t}/evidence/{id}/feedback` answers within seconds of
      landing (`seconds_after_landing`), Claude's transcription of a screenshot dated a
      year back blocks it with `date_mismatch`, and it is absent from the index (A-161).
- [ ] The adequacy verifier on Haiku 5.5 in shadow over real uploads, then 30 or more
      auditor labels (`ccp evidence label --entity ... --accept|--flag --by NAME`):
      `ccp evidence calibrate` reports agreement of at least 85% and a false-accept upper
      bound under 10% before `CCP_ADEQUACY_MODE=live` shows anything (A-162). The labels
      are auditors' work.
- [ ] A real questionnaire (20 or more questions) through `ccp questionnaires create`,
      `answer`, review and `send` on Haiku 5.5: drafts whose controls decide them follow
      the verdicts, no draft passes its checks with a number missing from its facts, and
      `metrics` gives the edit rate per kind of question (A-163). Reviewing is people's
      work.
- [ ] `ccp knowledge map propose --framework <a real framework's YAML>` with the
      Sonnet 5.5 chooser: a compliance reviewer accepts or rejects each proposal with
      `ccp knowledge map review`, and the days from the file to a reviewed mapping are
      recorded (A-164). The review is a named person's work.
- [ ] The Investigator on a failing GH-1 or HR-1 verdict chooses
      `github_remove_collaborator` with the right repository and login from the
      context's fix list (prompt `investigator_diagnose_v2`); a choice outside the list
      or with a malformed login is refused in the graph (A-160).
- [ ] `ccp evals run --suite all --no-unit --runs 5`: the 33 component cases that skip
      without a key, plus the adversarial suite, with pass^k over five runs.
- [ ] Tier 2 extraction on Haiku 5.5 and tier 3 on Sonnet 5.5: the pinned model ids
      resolve, the structured output parses, a refusal or a truncation becomes
      needs_review.
- [ ] The investigator (`ccp investigate run`), the policy agent (`ccp policy gap`,
      `draft`) and the copilot (`ccp copilot`) end to end.
- [ ] The agents' reading tools with a real model: an investigation of a policy finding
      calls `find_exact`, `outline` or `read_span` as well as `search_evidence`, and the
      run log shows each result's version and offsets; count searches per verified
      citation before and after (A-146).
- [ ] Transcription (`ccp ingest transcribe`, then `ccp normalize run`) of a real scanned
      PDF, a mixed PDF and a screenshot on Sonnet 5.5: the text is word for word, an
      unreadable page comes back `legible: false`, the transcriptions rows carry the cost,
      and the evidence's page_methods name the model and prompt (A-147).
- [ ] A long task on a real tenant: `ccp tasks plan --kind audit_prep --framework soc2`,
      then the hourly `tasks` job; check items settle from the verdicts with no model
      call, the gap list (`ccp tasks gaps`) matches the rollup, and after 20 read runs
      the forecast uses their measured p95 cost instead of the $0.20 setting (A-150).
- [ ] The single-run baseline and the fan-out on Sonnet 5.5: `ccp tasks measure --kind
      questionnaire --fixture evals/golden/tasks/questionnaire.yaml --mode single`, then
      `--mode fanout`; both land in evals/runs/, the fan-out's accuracy is not below the
      single run's, each item's run in `ccp agent` holds that item alone, and a real
      reading's pointers (evidence, chunk, offsets) open the quoted passage (A-151).
- [ ] The correction round on a real model: over a week of tier 3, investigator and
      task runs, count `correction_round` events and how many end
      `correction_succeeded`; a success rate that climbs while first-pass checks fall
      means a prompt is leaning on the round and needs fixing (A-152).
- [ ] Context ceilings against real token counts: for a sample of tier 2, tier 3,
      investigator, Copilot and task calls, compare the four-characters-a-token estimate
      with `messages.count_tokens` and the usage the API reports; if the estimate runs
      low by more than 10%, lower the ceilings in config/context_ceilings.yaml or move
      the view to the count endpoint (A-153).
- [ ] A waiting item answered by a person (`ccp tasks answer`) on Sonnet 5.5: the new
      run searches for the evidence the answer points to and cites it, rather than citing
      the answer; an answer that carries an instruction changes nothing it reads (A-154).
- [ ] Harness metrics on a real model: after a week of runs, `ccp agent harness` per
      graph and bundle (stop rates, tool error and repeat rates, first-pass rate,
      correction success, tokens and dollars per accepted result); before the next
      bundle ships, `ccp agent harness-gate --baseline <prod> --candidate <new>` on the
      eval organization's runs exits 0 (A-155).
- [ ] The four context probes on Sonnet 5.5: `ccp evals run --tag probe --runs 5`; each
      passes in all five runs, and `reread_by_model` says whether the model chose to read
      a swapped-out result back (A-156).
- [ ] The stratified nightly judged sample on a real judge: a week of `judged_sample`
      checks shows every stratum drawn, near-threshold PASS results most often; on the
      1st, `ccp evals relabel --month YYYY-MM` lists 20 items, an expert labels them
      (`--item ID --pass|--fail --by NAME`), and `--check` compares the agreement with the
      newest `ccp evals calibrate` interval (A-157). Labelling is people's work.
- [ ] Prompt caching: cache reads appear on repeated stable prefixes.
- [ ] `ccp replay <id> --execute` on a model-backed verdict.

## OpenAI API key (`OPENAI_API_KEY`)

- [ ] The cross-family judge grades judged cases; `ccp evals calibrate` against the
      labelled set.

## Voyage API key (`VOYAGE_API_KEY`)

- [ ] Embeddings and reranking: `ccp index refresh`, then the retrieval golden set.
- [ ] `ccp index gate` with the hosted pair: every evidence type clears the default
      floors in config/retrieval_gates.yaml (the keyless overrides do not apply); then
      `ccp index calibrate` writes the noise floor and the reliability curves, and a
      tier 2 verdict's events show `retrieval_reliability_type` or `_pooled` (A-148).
- [ ] `ccp index compare --embed voyage-4-large --rerank rerank-3`: the candidate
      generation is written beside the serving one, the paired report (MRR and Recall@5
      deltas with 95% bootstrap intervals, sign test, per type deltas, latency medians,
      the candidate index's cost) lands in evals/runs/, and nothing switches. Confirm
      the $0.12 per million tokens for voyage-4-large and rerank-3's price on Voyage's
      pricing page before relying on the cost line (A-149).
- [ ] A real re-embedding: `ccp index reembed --model voyage-4-large --no-switch` writes a
      candidate generation beside the serving one and prints both measurements; without
      `--no-switch` it serves only if the gate passes, and `ccp index generations` shows
      the old generation kept. `ccp index search --as-of <before>` still answers with the
      old model's key (A-145).

## GitHub token (`GITHUB_TOKEN`)

- [ ] `ccp ingest run --source live --connector github` against a real organization: the
      sync run's scopes, the completion marker, presence and revocation (A-118, A-119).
- [ ] `ccp connections check` with a live GitHub connection (A-121).
- [ ] The error table against real responses: a private repository on a free plan, an
      empty repository, a hidden listing (A-120).
- [ ] One live removal on a sandbox repository in `allowed_repos`: approve with a signed-in
      approver, `ccp remediate execute --live`; the journal reads started, observed,
      finished; `credential_issues.grant_id` names the grant; a second execute is refused;
      then `ccp remediate rollback --live` restores the recorded permission through its own
      proposal, approval and grant (A-131).

## AWS (MT5)

- [ ] `CCP_RAW_STORE=s3` against the Terraform raw bucket: an object lands locked in
      governance mode with the tenant's KMS key, its version id on the row; a delete of that
      version is refused without the governance bypass; `ccp audit raw` is clean (A-136).
- [ ] `CCP_AUDIT_KMS_KEY_ID` (an ECC_NIST_P256 SIGN_VERIFY key) and `CCP_AUDIT_BUCKET` in the
      separate audit account: an hourly checkpoint signs in KMS and lands locked in
      compliance mode; `ccp audit verify` is clean; a restore drill on an RDS snapshot with
      one altered row gives exactly one finding (A-137).

- [ ] `terraform init -backend-config=...` and `terraform apply` of `infra/terraform` into a
      cell and an audit account; `ccp db migrate` against the RDS instance creates pgvector
      (A-138).
- [ ] `deploy.yml` from main finishes in under 30 minutes; a rollback run with
      `rollback_to` finishes in under 10; a deliberately unhealthy image is rolled back by the
      circuit breaker (A-138).

- [ ] `ccp aws onboard` then the quick-create stack in a sandbox account; `ccp aws verify`
      passes, and fails when the stack is changed to drop the external ID condition or to
      attach ReadOnlyAccess (A-139).
- [ ] A live pull of that account with a Config recorder on and off: AWS-1, AWS-2 and AWS-3
      match what the console shows (A-140).
- [ ] With a failing AWS-1 bucket in a sandbox account, `ccp lookups run`: AWS Config
      history comes back in 7-day windows, CloudTrail events by the bucket's name stay
      under 2 requests a second without a ThrottlingException, every response lands in
      `raw_responses` before its `lookups` row, and the investigation job is queued after
      (A-158). With `GITHUB_TOKEN`, code search finds the bucket's Terraform within the
      10-a-minute search limit.
- [ ] On that sandbox account, the code-written investigation offers
      `s3_block_public_access`; applying its Terraform diff with `terraform plan` shows
      exactly one new `aws_s3_bucket_public_access_block`, and the commands run as
      rendered (A-159). No model key is needed for this path.
- [ ] Apply that fix in the sandbox account and let the next pull run: `ccp fixes
      confirm` marks it confirmed from the verdict that read it; remove the block again
      and the next pull reopens it (A-160). `ccp fixes metrics` shows the hours from
      the failing verdict to the confirming read.

## Okta and the HR aggregator (MT6)

- [ ] `CCP_IDP_TOKEN` and an `idp` live connection naming the Okta domain: a pull pages
      through every user, factors per user; a leaver active in Okta fails HR-1 (A-141).
- [ ] `CCP_HRIS_AGGREGATOR_KEY` and `CCP_HRIS_ACCOUNT_TOKEN` against a Merge sandbox: the
      employees land as HR snapshots and identity resolution links them (A-141).

## Langfuse keys

- [ ] Traces arrive with metadata only, and cost per run.

## Cognito or another OIDC provider (MT4)

- [ ] `CCP_AUTH_MODE=oidc`, `CCP_OIDC_ISSUER` (the user pool's issuer URL) and
      `CCP_OIDC_AUDIENCE` (the app client id): a token from a real sign-in verifies; an
      expired token, another client's token and a token from another issuer are refused
      (A-129).
- [ ] Federation from a customer's SAML or OIDC provider into the user pool.
- [ ] `ccp serve` with `CCP_AUTH_MODE=oidc`: a real ID token reaches `/v1/me`, a token for
      another client or issuer is 401, and a member's decision over HTTP records their
      issuer and subject (A-134).
- [ ] Browser sign-in at `/ui/login` with `CCP_OIDC_AUTHORIZE_URL`, `CCP_OIDC_TOKEN_URL`
      (and `CCP_OIDC_CLIENT_SECRET` for a confidential app client) and `CCP_UI_BASE_URL` set
      to the registered callback's origin: the code flow with PKCE ends signed in (A-135).

## A git remote (NT1)

- [ ] `.github/workflows/ci.yml` on every push and `nightly-evals.yml` at 03:17 UTC with
      the keys above held as repository secrets.
