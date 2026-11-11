# Verifying with real credentials

Everything below is built and tested offline: stub models, recorded API responses, a local
identity provider, and local stand-ins for AWS. None of it counts as verified until it has
run against the real service with the credential named. This list grows with each roadmap
step; work through it when the credentials are available.

## Anthropic API key (`ANTHROPIC_API_KEY`)

- [ ] `ccp evals run --suite all --no-unit --runs 5`: the 33 component cases that skip
      without a key, plus the adversarial suite, with pass^k over five runs.
- [ ] Tier 2 extraction on Haiku 5.5 and tier 3 on Sonnet 5.5: the pinned model ids
      resolve, the structured output parses, a refusal or a truncation becomes
      needs_review.
- [ ] The investigator (`ccp investigate run`), the policy agent (`ccp policy gap`,
      `draft`) and the copilot (`ccp copilot`) end to end.
- [ ] Prompt caching: cache reads appear on repeated stable prefixes.
- [ ] `ccp replay <id> --execute` on a model-backed verdict.

## OpenAI API key (`OPENAI_API_KEY`)

- [ ] The cross-family judge grades judged cases; `ccp evals calibrate` against the
      labelled set.

## Voyage API key (`VOYAGE_API_KEY`)

- [ ] Embeddings and reranking: `ccp index refresh`, then the retrieval golden set.

## GitHub token (`GITHUB_TOKEN`)

- [ ] `ccp ingest run --source live --connector github` against a real organization: the
      sync run's scopes, the completion marker, presence and revocation (A-118, A-119).
- [ ] `ccp connections check` with a live GitHub connection (A-121).
- [ ] The error table against real responses: a private repository on a free plan, an
      empty repository, a hidden listing (A-120).

## Langfuse keys

- [ ] Traces arrive with metadata only, and cost per run.

## Cognito or another OIDC provider (MT4)

- [ ] `CCP_AUTH_MODE=oidc`, `CCP_OIDC_ISSUER` (the user pool's issuer URL) and
      `CCP_OIDC_AUDIENCE` (the app client id): a token from a real sign-in verifies; an
      expired token, another client's token and a token from another issuer are refused
      (A-129).
- [ ] Federation from a customer's SAML or OIDC provider into the user pool.

## A git remote (NT1)

- [ ] `.github/workflows/ci.yml` on every push and `nightly-evals.yml` at 03:17 UTC with
      the keys above held as repository secrets.
