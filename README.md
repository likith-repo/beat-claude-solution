# Lead Triage Agent

Submission for the **Beat Claude Challenge**: *AI Automation Intern 012, The Lead Triage Agent* (brief version 2026-07).

The agent reads a CSV of inbound leads and classifies every row as `QUALIFY`, `NURTURE`, `REJECT` or `ESCALATE`, with a reason, a confidence and a route to a human where one is needed. The output is a first pass for a person to review, not a final call.

## How it works

Deterministic guardrails wrap one constrained LLM call. The model never has the final say on anything risky.

```
leads CSV
  -> ingest      schema check, SHA-256 of the input (written to the run log)
  -> rules       normalize text and budget, dedupe by contact, raise flags
                 (injection, phishing link, privacy request, disposable email, ...)
  -> verify      optional SerpApi web check, run by the pipeline, never by the model
  -> classify    OpenRouter model, JSON-only answer; lead text is untrusted data
  -> merge       hard rules override the model; the model decides only what is left
  -> decisions.csv, decisions.json, run_log.jsonl
```

Design choices:

- **The model has no tools.** It cannot fetch URLs, send mail or write anywhere. The run log records the request parameters to show this.
- **Lead text is data, not instructions.** Rule-based reasons are templates and never echo lead text. Exported cells are defused against spreadsheet formulas.
- **Nothing is dropped.** A row the model fails on becomes `ESCALATE`, and the run aborts if the decision count differs from the row count.
- **Privacy and security requests are routed, not acted on.** Erasure requests go to Privacy/DPO and phishing links to InfoSec, with no automated reply and no link ever opened.
- **Confidence is capped at 0.95.** Qualify blockers (disposable email, unparseable budget, domain mismatch, and so on) hold a row at `NURTURE`.

The qualification thresholds (for example USD 5,000 per month to qualify) are assumptions, not approved company policy. See `skills/lead-triage/SKILL.md` and `triage/merge.py`.

## Repo layout

| Path | Purpose |
|---|---|
| `triage/` | Core pipeline: `ingest`, `rules`, `search`, `classifier`, `merge`, `prompt`, `pipeline` |
| `prompts/`, `skills/lead-triage/` | System prompt and triage policy given to the model |
| `scripts/run.py` | CLI runner (online or `--offline`) |
| `scripts/analyze.py` | Rules-only analysis, prints flags per lead |
| `scripts/build_lambda.py` | Builds the deterministic `build/lambda.zip` |
| `backend/handler.py` | AWS Lambda API and worker handlers |
| `app/streamlit_app.py` | Streamlit front end |
| `infra/` | Terraform for the AWS deployment (`infra/bootstrap` holds the CI roles) |
| `tests/` | pytest suite |
| `plan.md`, `system-design.md` | Early planning notes. They describe a Vercel and TypeScript design that was **not** built; the code here is Python on AWS |

## Run it locally

Requires Python 3.12. The pipeline itself uses only the standard library.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pytest

# Rules only: no API key, no network
python3 scripts/run.py --input path/to/leads.csv --offline --out out/offline

# With the model (and web verification if SERPAPI_API_KEY is set)
cp .env.example .env   # fill in OPENROUTER_API_KEY, optionally SERPAPI_API_KEY
python3 scripts/run.py --input path/to/leads.csv --out out/run1
```

The input CSV needs these columns: `lead_id, submitted_at, name, email, company, website, monthly_budget_usd, message, source`. The challenge fixture and run outputs are not committed (`*.csv` and `out/` are git-ignored).

Each run writes three files to `--out`:

- `decisions.csv` and `decisions.json`: one decision per row, with `decided_by` showing whether a rule or the model made the call
- `run_log.jsonl`: header (input SHA-256, mode, model, prompt hash, git SHA) plus one record per row with the prompt, raw model output, flags and final decision

Run the tests:

```bash
python -m pytest -q
```

## Web app and API

The deployed version runs on AWS in `ap-southeast-1`: API Gateway (HTTP API), two Lambda functions (API and worker), S3 for run state, and SSM Parameter Store for secrets. The API starts a run and the page polls it.

| Endpoint | Purpose |
|---|---|
| `POST /runs` | Body `{"csv": "<file contents>"}`. Returns `202` with a `run_id` |
| `GET /runs/{id}` | Progress, decisions so far, counts, and 15-minute download links when done |

Every request needs an `x-api-key` header. Limits: 100,000 bytes of CSV and 50 rows per run. Run files expire from S3 after 7 days.

Start the Streamlit front end with the API URL and key from the Terraform outputs and the `/lead-triage/api_key` SSM parameter:

```bash
pip install -r app/requirements.txt
export LEAD_TRIAGE_API_URL=<api_url output>
export LEAD_TRIAGE_API_KEY=<value of /lead-triage/api_key>
streamlit run app/streamlit_app.py
```

Alternatively put both values in `.streamlit/secrets.toml` (git-ignored).

## Deploy

Infrastructure is deployed by the `deploy` GitHub Actions workflow using OIDC, with no long-lived AWS keys.

- **Pull request:** run tests, build the Lambda zip, then `terraform fmt`, `validate` and `plan`.
- **Manual run on `main`:** apply, gated by the `production` environment.

The three secrets (`api_key`, `openrouter_api_key`, `serpapi_api_key`) live in SSM and are created outside Terraform, so their values never reach state:

```bash
aws ssm put-parameter --overwrite --type SecureString --name /lead-triage/api_key --value <value>
```

## Known limits

- The agent is stateless across runs, so duplicates are only detected within one file.
- Company verification is a weak signal from web search. There is no enrichment or CRM lookup.
- Model output is not strictly deterministic even at temperature 0, so rerunning can change borderline calls. The run log records the model and prompt hash for comparison.
- Thresholds and the email-domain lists are assumptions, not verified against any company policy.
