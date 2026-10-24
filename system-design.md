# System Design: Lead Triage Agent on Vercel (AI Automation Intern 012)

Status: design only, nothing built. Companion to `plan.md`.
Brief version: 2026-07. Fixture SHA-256: `cc1927ca771c37b186a2abdb7b9757594da79ec6dda074e5a514dd2761cc8599` (observed, 2026-10-20).
Platform decision: **Vercel** (chosen by you). Earlier AWS / n8n comparison dropped.

Source labels: platform limits below were read from Vercel docs on 2026-10-20 and are marked **[Verified: URL]**. Anything marked **[Unverified]** is from general knowledge and must be checked before relying on it. Plan tier (Hobby vs Pro) is not known yet; limits are given for both.

## 1. Recommendation

One Next.js (App Router, TypeScript) project on Vercel containing:

1. **A pure triage core** (`lib/`): normalize, dedupe, flags, guardrail merge, classifier interface. No framework imports, so it also runs as a CLI.
2. **A thin web layer**: a page to upload or select the fixture and watch rows resolve, plus one API route that runs the triage.
3. **A CLI + GitHub Actions job** running the same core with no API key (`--offline` rules-only mode), so a reviewer can re-run without your Vercel account. This is the fallback path if the Vercel deployment is down, gated or private.

Why TypeScript end to end: one language for core, UI, tests and CLI; the Anthropic TypeScript SDK and `@vercel/blob` are first-class. (Vercel also runs Python functions [Verified: https://vercel.com/docs/functions/limitations], but mixing a Python core with a Next.js UI means two toolchains for a 20-row task.) This replaces the Python assumption in the earlier plan.

## 2. Platform facts that shape the design

| Fact | Value | Design consequence | Source |
|---|---|---|---|
| Max function duration (Fluid compute) | Hobby 300s default and max. Pro 300s default, 800s max | A 20-row run finishes far inside 300s. 500 rows must be chunked or streamed | [Verified: https://vercel.com/docs/functions/limitations] |
| Request/response body | 4.5 MB max | Fixture (about 3 KB) is fine. The decisions payload is fine. Large CSV uploads would need Blob client uploads | same |
| Active CPU billing | Waiting on I/O such as model calls does not count as active CPU | LLM-bound work is cheap on Vercel | same |
| Runtime log retention | Hobby **1 hour**, Pro 1 day, Pro + Observability Plus 30 days. Max 256 lines and 1 MB per request | **Vercel logs cannot be the run log.** Write our own JSONL to Blob and commit one run to the repo. Keep per-request logging under the 1 MB cap | [Verified: https://vercel.com/docs/logs/runtime] |
| Blob access mode | Private or public, **cannot be changed after the store is created** | Create a **private** store from the start (leads are PII-like even when synthetic) | [Verified: https://vercel.com/docs/vercel-blob] |
| Blob auth on Vercel | OIDC by default, no long-lived token. `BLOB_READ_WRITE_TOKEN` only needed outside Vercel (CI, other hosts) | Route uses OIDC. The GitHub Action does not touch Blob | same |
| Blob overwrites | Blocked by default. Use unique pathnames | Name each run's artifacts `runs/{run_id}/...`, treat as immutable | same |
| Blob region | Chosen at store creation, not changeable | Pick one region near the function region. Note it for the privacy story | same |
| Default function region | `iad1`, configurable | Set deliberately. For SGT-based user, consider `sin1` [Unverified: check region code list] | [Verified: functions limitations page] |
| Deployment protection / password gate | Exists on Vercel | Gate the demo URL so strangers cannot spend model credits. **[Unverified]** which protection options are available on your plan, check dashboard | none |

## 3. Architecture

```
 Reviewer / you
      |
      v
 +-----------------------------+            +------------------------------+
 | Next.js page (Vercel)       |  POST      | /api/triage  (Node runtime)  |
 | - "Run fixture" button      |----------->| 1. load fixture, sha256      |
 | - live table of decisions   |  NDJSON    | 2. rules: normalize,dedupe,  |
 | - flags, escalation reasons |<-----------|    flags                     |
 | - download decisions + log  |  stream    | 3. classify (bounded         |
 +-----------------------------+            |    concurrency ~5)           |
                                            | 4. guardrail merge           |
                                            | 5. stream row result         |
                                            | 6. write artifacts           |
                                            +---------+---------+----------+
                                                      |         |
                                       Anthropic API  |         | @vercel/blob (private, OIDC)
                                       (no tools)     v         v
                                                  model       runs/{run_id}/decisions.csv
                                                              runs/{run_id}/decisions.json
                                                              runs/{run_id}/run_log.jsonl

 Parallel path, same core, no Vercel account needed:
 GitHub Actions: checkout -> verify checksum -> `npm run triage -- --offline|--online` -> upload artifacts
```

Notes:

- **Streaming results (NDJSON)** keeps the page live and keeps the function responding row by row, which also protects against the duration cap as volume grows. Streamed responses count toward the max duration [Verified: functions limitations page].
- **The fixture lives in the repo** (`fixtures/inbound_leads.csv`, byte-identical copy) and is bundled into the deployment. A fresh fixture can be supplied by upload (under 4.5 MB). The route validates the schema and refuses anything else.
- **The model has no tools, no network and no secrets.** The key stays in the route handler. The classifier function receives one lead as a JSON value and returns a constrained object. That is the structural defense against L-006 and L-020. The prompt is the second layer, not the first.
- **Run artifacts are written once and never overwritten**: `runs/{run_id}/...` with `run_id` = timestamp plus short hash of the fixture checksum.

### Module layout

```
app/
  page.tsx                 UI
  api/triage/route.ts      streaming route (runtime: nodejs, maxDuration set explicitly)
lib/
  ingest.ts                CSV parse, sha256, schema check
  rules.ts                 normalize, budget parse, timestamp validate, flags, dedupe  (pure)
  classifier.ts            interface + AnthropicClassifier + OfflineRuleClassifier
  prompt.ts                system prompt, data-block template
  merge.ts                 guardrail precedence (pure)
  runlog.ts                JSONL writer (Blob sink and file sink behind one interface)
  types.ts
cli/triage.ts              same pipeline, file sinks, --offline|--online
fixtures/inbound_leads.csv
tests/                     vitest: rules, merge, budget/timestamp parsing, injection flag, row-count invariant
.github/workflows/triage.yml
out/                       committed copy of one real run (decisions + run_log)
```

Only two seams are abstracted: `Classifier` and `Sink`. Everything else is plain functions.

## 4. Environments and secrets

| Item | Where | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Vercel project env var (mark as sensitive, scope Production and Preview) and GitHub Actions secret for the manual `online` job | Never in the repo, the run log or client bundle. No `NEXT_PUBLIC_` prefix. Rotate if it is ever exposed |
| Blob store | Private, connected to the project, OIDC | No static token in Vercel. Local dev: `vercel env pull` |
| `TRIAGE_MODE` | env var | `online` or `offline`, recorded in the run log |
| Model ID | env var | Recorded in the run log. Load the `claude-api` skill when writing the call to pick the current ID |
| Fork PRs | Must not receive secrets | Default GitHub behavior. Keep it |

Preview deployments get their own URLs and share env vars only if scoped to Preview. Keep the demo on the Production URL behind protection, and do not leave open preview URLs with the key attached.

## 5. Security and privacy

| Threat | Control |
|---|---|
| Embedded instructions in lead text (L-006) | Lead fields passed as JSON data in a delimited block. Structured output schema. No tools. Merge layer: flagged rows can never be QUALIFY. Human flag raised |
| Malicious link (L-020) | URL is a string only. Nothing fetches it. Defanged (`hxxp://`) in logs, UI and any export. UI never renders lead text as HTML (React escapes by default, do not use `dangerouslySetInnerHTML`) |
| XSS via lead text (L-016 has HTML) | Render as text. Strip tags in normalization, keep the original in the log inside a JSON string |
| Public endpoint abuse (credit burn) | Deployment protection or shared-secret header, max 200 rows per request, max 4,000 chars per field, simple rate limit. **[Unverified]** whether a platform firewall rule is available on your plan |
| Log leakage | JSONL contains lead_id, flags, prompt and model output. Emails are hashed in the log, raw lead text stays only in the input and the in-memory request. Synthetic data here, but the habit matters for the privacy narrative |
| Privacy request (L-019) | Route only. No deletion and no auto-reply by the agent |
| Retention | Run artifacts in the Blob store are manual-delete. Add a documented retention note (for example 30 days [Assumed]) and a delete-run script. Blob deletes are free [Verified: Blob docs] |

## 6. Run log and evidence

Per row, one JSONL line:

```
{ run_id, fixture_sha256, mode, model_id, commit_sha, lead_id,
  normalized, rule_flags[], duplicate_of, prompt_hash, prompt_text,
  raw_model_output, parsed_output, guardrail_overrides[],
  final_decision, confidence, latency_ms, ts }
```

Evidence plan:

- Tier 2: the working deployment URL plus repo.
- Tier 3: committed `out/run_log.jsonl` from the **first** real online run (never regenerated), plus the GitHub Actions run URL.
- Tier 4: `manual_pass.csv` (written blind, before reading agent output) diffed against agent decisions.
- Do **not** cite Vercel runtime logs as evidence: on Hobby they are gone after an hour [Verified: runtime logs page].

## 7. Failure handling

- Model call fails, times out or returns invalid JSON: retry once, then that row becomes `ESCALATE` with reason "classifier unavailable". Never abort the batch, never drop a row.
- End-of-run invariant: `decisions.length === input_rows` else non-zero exit (CLI) or an error event in the stream.
- Function timeout (504) mid-run: because artifacts are written per row to a JSONL as the stream proceeds, a partial run is detectable. The UI shows "incomplete: N of M rows" and does not offer a decisions download as complete.
- Rate limits from the model API: bounded concurrency plus exponential backoff.

## 8. Cost

- Vercel compute: model waits do not count as Active CPU [Verified], so compute cost for this workload should be negligible. [Estimated]
- Blob: a few KB per run. [Estimated] negligible.
- Model: dominant cost. Estimate [Assumed]: about 1.5k input and 0.2k output tokens per row, 20 rows for the challenge, 500 rows per week later. Compute with current published prices (check via the `claude-api` skill) before quoting a number. Do not publish a USD figure unlabeled.

## 9. Path to 500 leads a week (design now, build later)

1. **First thing to fail: cross-run dedupe.** A function has no memory between runs. Add a persistent store keyed on normalized email. On Vercel that means a Marketplace database or Blob-backed index [Unverified which integrations suit]. Not built now.
2. **Duration**: 500 rows at roughly 2-4 seconds each with concurrency 5 should take a few minutes [Estimated], close to the 300s Hobby cap. Chunk into batches of 100, or move to Vercel Workflows, which pause and resume without a duration limit [Verified: functions limitations page mentions Workflows]. Not built now.
3. **Triggering**: lead arrival as a webhook or Vercel Cron instead of a manual button [Unverified details]. Out of scope.
4. **No enrichment**: L-008 and L-013 stay human until a company lookup exists.
5. **Calibration drift**: log human overrides and measure disagreement weekly.

## 10. Out of scope (deliberately)

No database, queue, auth system, CRM write, email sending, agent framework, or multi-region setup. A 20-row triage task does not need them, and the brief penalizes over-scoping.

## 11. Open questions for you

1. **Vercel plan: Hobby or Pro?** Hobby: 300s cap and 1-hour logs. This design works on Hobby because the run log lives in Blob and the repo.
2. **Is the GitHub repo public?** If private, the reviewer cannot use the Actions fallback, so you would add a Loom plus the committed log.
3. **Anthropic API key available?** If not, the offline mode still runs, but the "LLM resists injection" evidence would be missing. Say so now rather than after the build.
4. **Demo access**: password-protect the URL and share the password in the submission, or leave it public with a row cap and rate limit?
5. **Vercel region**: default `iad1`, or something closer to Singapore? It only affects latency, not correctness.

## 12. Next step

Answer section 11. I'll then finalize `plan.md` and, only when you say so, start the build. Proposed build order is in `plan.md` section 5.
