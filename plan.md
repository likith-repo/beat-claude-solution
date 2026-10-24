# Plan: AI Automation Intern 012 - The Lead Triage Agent

Brief version: 2026-07
Fixture: `challenges/ai-automation-intern-012/fixtures/inbound_leads.csv` (20 rows, 9 columns)
Fixture SHA-256: `cc1927ca771c37b186a2abdb7b9757594da79ec6dda074e5a514dd2761cc8599` (observed, computed 2026-10-20; the pipeline must recompute it at runtime and write it into the run log)

## 1. What is being asked

Build a first-pass triage agent that processes every fixture row and emits `lead_id, decision, reason, confidence`, where decision is one of `QUALIFY | NURTURE | REJECT | ESCALATE`. It must:

- Escalate on missing, conflicting, malformed or sensitive input instead of guessing.
- Treat lead text as data, never as instructions.
- Ship a run log from the run that produced the decisions.
- Come with a write-up of at most 4 pages that includes one real bad output from our own run.
- Answer the meta question in 3-5 sentences.

What loses: confidently qualifying trap rows, skipping messy rows, a doc with no run, hiding bad outputs, over-scoping. What wins: a working loop built in hours, sensible calls on messy rows, and an honest bad-output story.

Packet files the grader and `scripts/validate_submission.py` look for: written answer, operating artifact, evidence log, number source labels, AI usage disclosure, what breaks it / failure handling, artifact access, and what stays human. Also include the brief version and the fixture checksum.

## 2. Row-by-row trap analysis (my hypotheses, to be confirmed by the run and a blind manual pass)

The seeded-trap key is private, so this is my own read. Each row below needs to be checked against the real agent output, not assumed.

| ID | What I see | Trap type | Planned decision |
|----|------------|-----------|------------------|
| L-001 | BrightCart, Series B, USD 25,000, clear paid + CRO need, 30-day timeline | Clean positive (control) | QUALIFY, high confidence |
| L-002 | Student, no company, budget 0, wants mentoring | Non-buyer | REJECT |
| L-003 | Same email as L-001, name "D. Reyes", company "Bright Cart Inc", adds SEO interest | Duplicate with variant name and company | Link to L-001 (`duplicate_of`), do not double-count. NURTURE with an "add SEO interest to L-001 record" note |
| L-004 | No email, otherwise strong (USD 12,000, approved budget, webinar source) | Missing contact field | ESCALATE (human finds contact via the website or webinar list) |
| L-005 | Budget is the string "we'll discuss", sensible fit | Malformed budget | NURTURE, lower confidence. Do not coerce to 0 or reject |
| L-006 | Budget 500 with an embedded instruction to force QUALIFY at confidence 1.0 | Prompt injection | Classify on merits (budget 500, unverifiable claims) so REJECT, with `manipulation_flag=true` and a note to a human. Never QUALIFY |
| L-007 | 12,000-employee CPG, budget 50, global rebrand | Conflicting signals (budget looks like a typo or unit error) | ESCALATE |
| L-008 | Gmail address, no company, "Fortune 500 VP", USD 40,000, "sign this week" | Unverifiable claim, urgency pressure | ESCALATE (do not qualify on an unverifiable claim) |
| L-009 | Spanish-language message, solid fit, USD 18,000 | Language trap (must not be dropped as noise) | QUALIFY. Note the language so the human can route to a Spanish-speaking owner |
| L-010 | `test` / `asdf@asdf.com` / `asdfasdf` / budget 999999 | Junk or test submission with an absurd budget | REJECT |
| L-011 | Agency "comparing retainers for a client", USD 30,000 | Competitor or reseller intel, ambiguous intent | ESCALATE to partnerships (or NURTURE; decide and justify) |
| L-012 | Timestamp `2026-13-45T99:99:00Z` | Malformed field | ESCALATE, low-priority data-quality route. The fit looks decent. Do not silently drop the row |
| L-013 | Mailinator address, no website, asks for the methodology deck and client results first | Disposable email, asset-extraction attempt | NURTURE with "send nothing until identity is verified" (or REJECT, to be debated) |
| L-014 | USD 3,000, pre-seed, will scale in about 6 months | Small now, real later | NURTURE |
| L-015 | Same email as L-002, now claims a family restaurant group with 4 locations, USD 2,500 | Same person, conflicting identity and intent across rows | NURTURE with a verify flag, linked to L-002 (or ESCALATE) |
| L-016 | HTML tags, entities, emoji, USD 6,000 HVAC | Dirty text (must sanitize, not reject) | QUALIFY, moderate confidence |
| L-017 | Budget "15k" | Format variant to parse | Parse to 15000, QUALIFY. Note in-house team of 3 and the compliance review requirement |
| L-018 | Empty message, USD 22,000, Quorum Data | Missing field | NURTURE (needs discovery to learn the need) or QUALIFY at lower confidence |
| L-019 | GDPR Article 17 erasure request | Sensitive and legal | ESCALATE to privacy / DPO. The agent never deletes anything and never auto-replies. The statutory clock starts at receipt |
| L-020 | Overdue-invoice pretext with a link to an `.exe` | Phishing or malware | ESCALATE to InfoSec. Never fetch or follow the URL |

Open judgment calls (my picks go in the write-up as stated assumptions): L-003, L-011, L-012, L-013, L-015, L-018. The distribution should land at roughly 4 QUALIFY, 5-6 NURTURE, 3-4 REJECT, 6-8 ESCALATE. That is a count I expect and have not observed. Escalating nearly everything would be as weak as escalating nothing.

### Assumed qualification policy (the brief gives no ICP, so I state it explicitly)

- QUALIFY: parsed budget at or above USD 5,000/month, a verifiable company (domain matches the email, not a free or disposable mailbox), and a stated marketing need.
- NURTURE: real intent but low or unclear budget, a future timeline, or missing discovery info.
- REJECT: non-buyer, junk, or budget below USD 1,000 with no strategic signal.
- ESCALATE: required field missing or malformed, signals conflict, claims cannot be verified, or the content is legal, security or privacy sensitive.

These thresholds are [Assumed]. Say so in the write-up.

## 3. Architecture (scrappy, about 250-400 lines of TypeScript)

Platform: **Vercel** (Next.js App Router, TypeScript), with a CLI + GitHub Actions fallback running the same core. Full platform design, verified limits and security model are in `system-design.md`. This section is the logical pipeline only.

Deterministic guardrails around a constrained LLM call. The LLM never has final say on anything risky.

```
inbound_leads.csv
   |
   v
[1 Load + checksum]     sha256 of fixture -> run log header
   |
   v
[2 Normalize]           strip HTML/entities/emoji noise, parse budget ("15k", "we'll discuss"),
                        validate timestamp (ISO 8601), domain-vs-email check,
                        free/disposable email list, URL scheme + executable-extension check
   |
   v
[3 Dedupe]              group by lowercased email (plus fuzzy company/name) -> duplicate_of / same_person_conflict
   |
   v
[4 Deterministic flags] injection-phrase regex, sensitive-request keywords (GDPR, delete, erasure),
                        malicious-link check, missing required fields
   |
   v
[5 LLM classify]        Claude API, JSON-only output via schema; lead text inside a delimited data block;
                        system prompt says the block is untrusted data; model also returns
                        `manipulation_detected` and `reason`
   |
   v
[6 Guardrail merge]     hard rules override the LLM: injection -> never QUALIFY; sensitive/legal/security -> ESCALATE;
                        missing email or malformed value on a high-value row -> ESCALATE;
                        low-confidence LLM -> ESCALATE
   |
   v
decisions.csv + run_log.jsonl (one record per row: inputs hash, flags, prompt, raw model output, final decision, which rule overrode)
   |
   v
[7 Persist]             private Vercel Blob at runs/{run_id}/..., plus a committed copy of the first real run in out/
```

Design choices:

- **Model**: use a current Claude model through the Anthropic TypeScript SDK (load the `claude-api` skill before writing the call). Use structured output and a small fixed schema: `decision`, `reason`, `confidence`, `manipulation_detected`, `needs_human_because`.
- **Run log lives in Blob and the repo, not in Vercel logs.** On the Hobby plan runtime logs are kept for 1 hour [Verified: https://vercel.com/docs/logs/runtime, read 2026-10-20].
- **Prompt hygiene**: lead fields are passed as JSON values inside a clearly delimited data block, never concatenated into the instruction text. The system prompt states that the block contains untrusted content and that any instruction in it is a signal to flag, not a command.
- **No tools given to the model**: no URL fetching, no email sending, no CRM writes. The agent is read-only and produces a file. This is the strongest defense against L-006 and L-020.
- **No-key fallback**: `--offline` mode that uses only the deterministic rules, so a reviewer can re-run without an API key. The run log records which mode produced the file.
- **Temperature 0**, and log the model ID, so reruns are comparable. Say honestly that LLM output is not strictly deterministic.
- **Confidence**: calibrated by rule. Hard-rule outcomes get high confidence, LLM-judged ambiguity gets 0.5-0.7. Do not emit 1.0 anywhere. Document how it is derived.
- **Reason strings**: one sentence, written without echoing the injected text, so downstream tools and reviewers do not ingest it.

## 4. Repo layout (in `/home/username/Documents/GitHub/beat-claude-solution`)

```
README.md              how to run, demo URL + access, checksum, brief version
plan.md                this file
system-design.md       Vercel platform design
app/page.tsx           UI: run button, live decisions table, downloads
app/api/triage/route.ts  streaming route (nodejs runtime, explicit maxDuration)
lib/ingest.ts          CSV parse, sha256, schema check
lib/rules.ts           normalization, dedupe, flags (pure functions)
lib/merge.ts           guardrail precedence (pure)
lib/classifier.ts      Classifier interface: Anthropic + Offline implementations
lib/prompt.ts          system prompt + data-block template
lib/runlog.ts          JSONL writer, Blob sink and file sink
cli/triage.ts          same pipeline from the terminal (--offline | --online)
tests/                 vitest: budget parsing, timestamp validation, dedupe, injection flag, merge, row-count invariant
fixtures/inbound_leads.csv   copy of the fixture, byte-identical (verify the checksum)
.github/workflows/triage.yml reviewer re-run path, no Vercel account needed
out/decisions.csv      full 20-row output
out/decisions.json     same, with flags and duplicate links
out/run_log.jsonl      the first real run's trace, never regenerated
write-up.md            the 4-page submission (see section 6)
```

## 5. Build order (timeboxed: 1-2 hours of work per the brief)

1. **Setup (15 min)**: copy the fixture, verify the checksum, scaffold Next.js + TypeScript, link the Vercel project, create a **private** Blob store (access mode cannot be changed later), add env vars.
2. **Rules layer (30 min)**: budget parser, timestamp validator, HTML cleaner, dedupe, flags. Add unit tests for the odd rows (`15k`, `we'll discuss`, `2026-13-45`, `<div>...&amp;`).
3. **LLM layer (20 min)**: prompt, schema, call, retry on invalid JSON, log raw output.
4. **Guardrail merge (15 min)**: precedence order and the override log.
5. **CLI + route + minimal UI (30 min)**: CLI first (it is the evidence path), then the streaming route and a plain table. No styling work.
6. **Deploy + protect (10 min)**: deploy to Production, enable access protection, set a row cap and field-length cap.
7. **Run it for real (10 min)**: run once online. Save the log untouched. If the run is rerun after changes, keep the first log too, because the first run is where the real bad output lives.
8. **Blind manual pass (15 min)**: before reading the agent output, write my own decision for all 20 rows into `manual_pass.csv`, then diff. This gives a Tier 4 comparison. Analyze each disagreement. Do this **before** step 7 if possible.
9. **Write-up, meta answer, evidence log (30 min)**.
10. **Pre-screen**: `python3 scripts/validate_submission.py write-up.md` from the beat-claude repo, then fix the failures.

Total is about 2.5-3 hours, slightly over the brief's 1-2 hour estimate because of the Vercel UI and deploy steps. If time runs short, cut step 5's UI and step 6, and ship the CLI plus Actions path. That still meets the brief.

## 6. Write-up outline (max 4 pages)

1. **Header**: brief version 2026-07, fixture checksum, repo link, run date.
2. **How it works**: the 6 steps above in about 8 lines.
3. **Architecture**: a diagram or the text diagram, model and data flow.
4. **What stays human**: legal and privacy (L-019), security (L-020), unverifiable high-value claims (L-008), conflicting enterprise signals (L-007), ambiguous competitor or partner intent (L-011), plus a sampled review of auto-QUALIFY and auto-REJECT. Explain why each one: the cost of a wrong automated call is legal, security or reputational.
5. **What the fixture threw at you**: a table of trap rows, why each is a trap, what the agent did. Quote the L-006 content **inside a code block**, and put no injection phrasing in the surrounding prose (the linter flags it).
6. **A real bad output**: this must come from the actual run. I cannot write it in advance. Candidate places to look: LLM treating `15k` or "we'll discuss" wrongly, qualifying L-016 on noise, qualifying L-018 with no stated need, merging L-003 incorrectly, rejecting L-009 because of language, or a reason string that parrots injected text. Record how I detected it (manual-pass diff or log inspection) and what I changed or routed to a human. If the run produces no real miss, say so and show the closest near-miss. Do not invent one.
7. **What breaks next** at 500 leads/week: duplicate detection across weeks (needs a persistent store), the free-text company lookup that this version does not do (no enrichment), cost and latency of one LLM call per row, confidence calibration drift, and injection variants the regex does not know. The first failure is likely the dedupe state, since the agent is stateless across runs. Label these [Estimated].
8. **Meta question (3-5 sentences)**: I must write this myself. It has to be specific and personal, with the most recent thing I automated and what I deliberately left manual. I will not draft it for me.

Required packet extras (can sit on a final page or in the README, because the code and logs do not count toward the page limit):

- **Evidence log** with the tier of each claim, for example: the agent and decisions file are Tier 2; the run log with prompt traces is Tier 3; the agent-vs-manual comparison is Tier 4 (not Tier 5, because no independent party verified it).
- **Number labels**: `20 rows [Observed]`, agreement rate with my manual pass `[Observed]`, build time `[Observed]` only if I track it, otherwise `[Estimated]`, the USD thresholds `[Assumed]`.
- **AI usage disclosure**: Claude Code helped with exploring the brief, the plan and the code scaffold; I decided the policy thresholds and the hard-rule precedence, and I checked the unit tests and the manual-pass diff. Name known weak spots.
- **What breaks it** and **Artifact access** sections (the validator requires them), with the exact re-run command and whether an API key is needed.

## 7. Risks and decisions for me

- **Do not fabricate.** The decisions file and log must come from a real execution. The plan's table is a hypothesis, and if the agent disagrees, I analyze it rather than overwrite the output.
- **Do not copy the planned decisions into the output by hand.** Reviewers know the fixture and can see when the log does not match.
- **Rotation**: a fresh fixture may be provided for a live re-run. Keep the rules generic (no row-ID special cases, no hardcoded emails), and make the pipeline run on any CSV with this schema.
- **PII**: the fixture is synthetic, but the habit is worth keeping. Do not paste real lead data into the write-up beyond what is needed, and do not follow or fetch the L-020 URL.
- **Over-scoping**: no enrichment API, no CRM integration, no web UI. If time runs short, ship steps 1-5 and list the rest as hardening.

## 8. Open questions for you

Decided: Vercel, TypeScript (replaces the earlier Python and n8n options).

Still open (details in `system-design.md` section 11):

1. Vercel plan, Hobby or Pro?
2. Is the GitHub repo public (needed for the free Actions re-run path)?
3. Do you have an Anthropic API key to use?
4. Demo access: password-protected or public with caps?
5. Are you comfortable with the ESCALATE-heavy outcome on L-011, L-012 and L-015, or do you want a more decisive stance on them?
6. The meta answer and the manual blind pass have to be yours. I can build everything else.
