# Agentic SDLC — Testmu AI

> Requirements go in. **KaneAI authors the tests.** HyperExecute runs them. AI explains failures. Objectives auto-improve every run.

---

## What this is

A fully automated testing pipeline where **KaneAI** — Testmu AI's browser agent — authors every test case from a plain-English objective. No selectors. No scripts. No page objects.

You give KaneAI an objective:

> *"Login to saucedemo as standard_user with password secret_sauce, click the Add to cart button for Sauce Labs Backpack, and verify the cart badge shows 1."*

KaneAI opens a real browser, figures out the UI, executes the steps, and saves the verified test directly into Testmu AI Test Manager. HyperExecute runs it in parallel. If it fails, Claude explains why and rewrites the objective — automatically, before the next run.

---

## The Pipeline

```
Push to main  ──────────────────────────────────────────────────────────────►
                                                                              │
        [optional: requirements URL provided via manual dispatch]             │
                    ↓                                                         │
           Claude extracts Acceptance Criteria                                │
                    ↓                                                         │
           Claude writes crisp objectives (max 5)                            │
                    ↓                                                         │
              committed to ci/objectives.json                                 │
                                                                              │
◄─────────────────────────────────────────────────────────────────────────────┘
                    ↓
         Cross-run self-heal (start of run)
         └── Claude pre-heals objectives that failed last run
             using LT AI RCA as context for HE failures
                    ↓
         kane-cli runs each objective on a real browser (KaneAI)
         ├── 3 SCs in parallel, staggered 3s apart
         ├── Tier 1 — Infra retry: CDP/browser crash → retry same objective
         ├── Tier 2 — Inline self-heal: logic failure → Claude rewrites → retry (up to 2 heal attempts)
         │           Each heal uses the fresh failure detail from the previous attempt as context
         └── saves verified test to Testmu AI Test Manager
                    ↓
         Test Run created in TM → instances linked with environment
                    ↓
         HyperExecute runs all tests in parallel (5 VMs, 1 retry per test)
         └── pipeline polls HE job status API until job reaches final state
                    ↓
         Wait 120s for LT insights engine to index HE sessions
                    ↓
         AI Root Cause Analysis
         ├── LT AI RCA: POST /rca/generate → wait 90s → GET /rca?job_ids=...
         │   └── retries trigger up to 3× (60s apart) if not yet indexed
         └── Claude RCA fallback (Phase 1 authoring failures only)
                    ↓
         Traceability Matrix → GitHub Step Summary
         ├── Authoring column (Phase 1) + Execution column (Phase 3)
         └── HE job link + TM Test Run Report link
                    ↓
         Auto-improve: Claude rewrites objectives for all failed SCs
         ├── Phase 1 failure → uses kane-cli run summary as context
         ├── HE execution failure → uses LT AI RCA as context
         └── commits improved objectives.json back to main [skip ci]
```

On the **next push**, the pipeline starts from the improved objectives automatically.

---

## Triggers

The pipeline runs **only on manual dispatch** — there is no push trigger.

| How | What happens |
|-----|-------------|
| **Manual dispatch — no URL** | Uses committed `objectives.json` — full pipeline from Phase 1 |
| **Manual dispatch + requirements URL** | Downloads doc → Claude extracts ACs → Claude generates objectives → full pipeline |

Go to **Actions → Agentic SDLC → Run workflow** and fill in the required fields.

---

## Design Decisions

### Why 3 parallel workers for kane-cli?

KaneAI authoring spins up a real browser session per SC on Testmu AI infrastructure. Running all 5 SCs simultaneously risks hitting the concurrent session limit and causes the later sessions to queue rather than start immediately. **3 is the sweet spot** — fast enough to keep total authoring time under 10 minutes, conservative enough to avoid session contention. The 3s stagger between starts further smooths the load spike.

### Why inline heal AND cross-run heal?

Two different problems, two different fixes:

| | Inline heal | Cross-run heal |
|--|-------------|----------------|
| **When** | Same run, immediately after failure | Start of the next run |
| **Why** | Saves the current run — a rewritten objective gets a second chance right now | Starts the next run from a better baseline before any SC even runs |
| **Context** | kane-cli's live failure detail (what it just tried) | Previous run's failure detail + LT AI RCA (richer, post-execution context) |
| **Who rewrites** | Claude Sonnet (fast, in-run) | Claude Sonnet (start of run, no time pressure) |

Without inline heal, a logic failure wastes the entire run. Without cross-run heal, you'd need a human to fix the objective between runs.

### Why two tiers within inline heal?

- **Tier 1 (infra retry, same objective):** CDP disconnects, browser crashes, and screenshot failures are transient. Healing the objective won't fix them — just retry. Using Claude here wastes tokens and risks introducing unnecessary changes.
- **Tier 2 (Claude heal + retry, up to 2 attempts):** If the failure persists after an infra retry, it's a logic problem — the objective is ambiguous, too specific about UI coordinates, or chains too many actions. That's when Claude rewrites it. A single heal attempt may produce a better-but-still-failing objective — the second attempt feeds that fresh failure detail back to Claude so each rewrite builds on the previous one rather than starting blind.

| Attempt | What runs |
|---------|-----------|
| 1 | Original objective |
| Tier 1 | Same objective (only if infra crash detected) |
| 2 | Claude heal #1 → retry |
| 3 | Claude heal #2 using failure from attempt 2 → retry |

### Why 5 VMs for HyperExecute?

5 VMs runs all 5 SCs simultaneously in under 2 minutes. More VMs would not reduce wall-clock time further since each SC maps to one session. Fewer VMs would serialise tests unnecessarily.

### Why 1 retry on HyperExecute?

Browser tests on real infrastructure can be flaky for reasons unrelated to the test logic (page load timing, transient network blip). A single retry catches these without masking real failures — two consecutive failures of the same test almost always indicate a genuine bug or a broken objective.

### Why manual dispatch only — no push trigger?

The pipeline consumes real LT quota (KaneAI sessions, HE VMs, Claude API tokens) on every run. A push trigger would fire on every commit — including auto-improve commits, README edits, and dependency bumps — burning quota unnecessarily. Manual dispatch gives explicit control over when the full pipeline runs, and the required `tm_project_id` input ensures runs are always attributed to the right project.

### Why is the objective format strictly enforced?

KaneAI works best with **intent-based** objectives — describe what to verify, not how to click. Chained actions ("add to cart and then navigate to cart page") require KaneAI to hold intermediate state and are prone to timing issues. State transitions ("verify the button changes to Remove") are inherently flaky because they depend on the exact moment the assertion fires. One action + one immediately visible assertion is the pattern that produces the most reliable, reusable test cases.

### Why does the reuse check exist?

If an objective is unchanged from the last run and a valid TM test case already exists, re-running kane-cli would produce an identical result. Skipping kane-cli for those SCs can save 5–8 minutes of authoring time per run. Only changed objectives or first-time SCs go through the full authoring cycle.

### Why does LT AI RCA need a 120s pre-wait?

The LT automation sessions API (used to fetch session results) indexes sessions almost immediately after they complete. The LT insights engine (used for AI RCA) is a separate system that ingests from the sessions API asynchronously — it typically lags by 2–3 minutes. Calling the RCA trigger too early returns `triggered=0` because the engine hasn't seen the sessions yet. The 120s wait is a safety buffer so the trigger finds the sessions on the first or second attempt.

### Why use the HE sessions API instead of the automation sessions API?

The automation sessions API (`/api/v1/sessions`) is account-wide — it returns the last 100 sessions across all runs. When the same TM test cases are reused across runs (because the reuse check skipped kane-cli), the same TC IDs (e.g. `TC-42299`) appear in every run's history. Fetching sessions by TC ID returns 10–15 entries for 4 TCs, with stale results from old runs overriding the current run's result during deduplication.

The HE sessions API (`GET /v2.0/job/{jobID}/sessions`) is job-scoped — it returns exactly the sessions created by this HE job. No time filtering, no `exclude_ids` snapshot, no timezone mismatch. Each session entry includes `status: passed/failed`, the TC ID embedded in the `name` field (`"Web || gagandeepb || TC-42303"`), and a `sessionID` for building the automation dashboard link. The result is always exactly 1 session per SC, from the current run only.

---

## Stage-by-stage

### Stage 1 — Requirements Analysis *(only when a requirements URL is provided)*

Claude reads the document and extracts Acceptance Criteria.

**Supported URL formats:**

| Source | Example |
|--------|---------|
| Google Docs | `docs.google.com/document/d/<ID>/edit` |
| Google Drive | `drive.google.com/file/d/<ID>/view` |
| GitHub file | `github.com/user/repo/blob/main/reqs.md` |
| GitHub Gist | `gist.github.com/user/<ID>` |
| Any raw URL | `https://example.com/requirements.txt` |

All formats are auto-detected. Make the doc publicly accessible before running.

**Output:** `requirements/analyzed_requirements.json`

---

### Stage 2 — Objective Generation *(only when a requirements URL is provided)*

Claude generates up to 5 crisp, intent-based objectives from the extracted ACs.

**Format enforced (strictly):**
```
Login to <url> as <user> with password <pass>, <one physical action>, and verify <one immediately visible result>.
```

**Good:**
```
Login to https://www.saucedemo.com/ as standard_user with password secret_sauce,
click the Add to cart button for Sauce Labs Backpack, and verify the cart badge shows 1.
```

**Bad (never do this):**
```
# Multiple actions chained
add Sauce Labs Backpack to the cart and navigate to the cart page, and verify...

# State transition (timing-sensitive)
...and verify the button changes to 'Remove'.

# Step-by-step micro-instructions
Navigate to the URL, type 'standard_user' into the username field, click the Login button...
```

**Output:** `ci/objectives.json` — committed to the repo for reuse across runs.

---

### Stage 3 — KaneAI Authoring *(Phase 1)*

`kane-cli run` executes each objective on a real browser. KaneAI uses AI vision — no pre-written selectors. Each verified test is saved into Testmu AI Test Manager.

- **3 SCs run in parallel**, staggered 3s apart — avoids concurrent session contention on LT infrastructure
- **600s timeout per SC** — KaneAI explores the UI autonomously; complex objectives can take time
- **Reuse check:** if objective is unchanged and a valid TM test case exists, kane-cli is skipped — saves 5–8 min per run
- **Tier 1 — Infra retry:** transient failures (CDP disconnect, browser crash) → retry same objective immediately, no Claude call
- **Tier 2 — Inline self-heal:** logic failure after infra retry → Claude (`claude-sonnet-4-6`) rewrites objective on-the-fly → retry. Up to **2 heal attempts per SC** — the second heal uses the failure detail from the first healed attempt as context, giving Claude a better signal each iteration
- **Cross-run self-heal:** at the start of every run, Claude pre-heals objectives that failed the previous run (uses LT AI RCA as context for HE failures, kane-cli run summary for authoring failures)

The healed objective (before/after comparison) is printed to logs so you can see exactly what changed.

---

### Stage 4 — Test Run + HyperExecute *(Phases 2 & 3)*

Creates a Testmu AI Test Manager test run, links all authored test cases with a test environment, and triggers HyperExecute via the TM API — no `hyperexecute.yaml` needed.

| Setting | Value | Why |
|---------|-------|-----|
| Concurrency | 5 parallel VMs | One VM per SC — maximum parallelism for 5 SCs |
| Retry on failure | 1 retry | Catches transient flakiness without masking real bugs |
| Environment | Configurable via `TM_ENVIRONMENT_ID` | Decouple browser/OS config from pipeline code |

The pipeline polls `GET /v2.0/job/{jobID}` (HyperExecute job status API) every 30s until `status` reaches a terminal state (`completed`, `failed`, `cancelled`, `aborted`, `error`). Once the job is done, `GET /v2.0/job/{jobID}/sessions` fetches the per-TC session results (status + sessionID) scoped to exactly this job — no cross-run contamination.

---

### Stage 5 — Root Cause Analysis

Two-layer RCA for every failed scenario:

**Layer 1 — LT AI RCA** (for HE execution failures):
- Waits 120s after HE completion for the LT insights engine to index sessions
- Triggers `POST /insights/api/v3/public/rca/generate` for the HE job
- Retries trigger up to 3× with 60s gaps if `triggered=0` (indexing lag)
- After trigger, waits 90s for AI generation then fetches via `GET /rca?job_ids=...`
- Retries fetch up to 3× with 30s gaps if no entries returned yet
- Each RCA entry contains: `failure_summary`, `analysis[]`, `steps_to_fix[]`

**Layer 2 — Claude RCA fallback** (for Phase 1 authoring failures only):
- Fires when LT AI RCA is unavailable OR the SC failed at authoring (never made it to HE)
- Reads kane-cli `failure_detail` from `run_history.json`
- Generates structured analysis: objective given, what kane-cli did, what needs fixing
- Marked as `source: claude-fallback` in `rca_results.json`

**Output:** `ci/rca_results.json` — consumed by both the traceability matrix and the auto-improve step.

---

### Stage 6 — Traceability Matrix

Full matrix linking every result back to its origin — published as GitHub Step Summary after every run.

```
AC → Objective → SC → TM Test Case → Authoring result → HE Execution result → AI RCA
```

Columns:
- **Authoring** — did kane-cli successfully author the test? (Phase 1)
- **Execution** — did HyperExecute pass the test? (Phase 3)
- **RCA** — LT AI RCA text inline; blank for claude-fallback entries (those go to auto-improve only)

---

### Stage 7 — Auto-Improve *(end of every run)*

After HE results and RCA are in, Claude rewrites objectives for **all failed SCs** and commits the result:

| Failure type | Context used for healing |
|-------------|--------------------------|
| Phase 1 authoring failure | kane-cli run summary (what it actually tried) |
| HE execution failure | LT AI RCA (`failure_summary` + `analysis` + `steps_to_fix`) |

The improved `objectives.json` is committed back to `main`. The workflow's `paths-ignore` excludes `ci/objectives.json` and `requirements/analyzed_requirements.json`, so this commit never re-triggers the pipeline. The next push automatically starts from better objectives — no human intervention needed.

> Custom URL runs (`REQUIREMENTS_URL` set) never overwrite the committed saucedemo objectives — only default runs auto-improve.

---

## Forking this repo

### 1. GitHub Secrets *(required)*

Go to **Settings → Secrets and variables → Actions → Secrets**

| Secret | Description | Where to get it |
|--------|-------------|-----------------|
| `LT_USERNAME` | Testmu AI username | [accounts.lambdatest.com/security](https://accounts.lambdatest.com/security) |
| `LT_ACCESS_KEY` | Testmu AI access key | [accounts.lambdatest.com/security](https://accounts.lambdatest.com/security) |
| `ANTHROPIC_API_KEY` | Claude API key | [console.anthropic.com](https://console.anthropic.com) |

These three are the only required configuration. Without them the pipeline cannot authenticate.

### 2. Required workflow inputs *(must provide on every run)*

These are entered at **Actions → Agentic SDLC → Run workflow** — the pipeline refuses to start without them.

| Input | Required | Description | How to find it |
|-------|----------|-------------|----------------|
| `tm_project_id` | ✅ Yes | Test Manager project ID (ULID) | KaneAI → your project → copy from URL |
| `tm_environment_id` | ⚠️ Optional | Test environment ID (integer). Leave blank on first run — pipeline auto-creates a compatible env and reuses it on subsequent runs. | Test Manager → Environments → your environment ID |
| `kane_folder_id` | ⚠️ Recommended | KaneAI folder ID (ULID) inside your project | KaneAI → your project → folder → copy from URL |

> **Why `kane_folder_id` matters:** kane-cli saves authored test cases to a specific folder. If you set `tm_project_id` but don't set `kane_folder_id`, kane-cli falls back to its auto-selected folder which may belong to a **different project** — test cases end up in the wrong place and Phase 2 finds nothing.
>
> If you have a `KANE_FOLDER_ID` repository secret set, the workflow uses that automatically and you don't need to enter it every run.

### 3. Configure kane-cli for your project

No manual configuration needed — the workflow handles it automatically using your workflow inputs.

### 4. Update objectives

Replace `ci/objectives.json` with objectives for your app:

```json
[
  {
    "id": "SC-001",
    "ac_id": "AC-001",
    "name": "SC-001: short description",
    "objective": "Login to https://yourapp.com as user with password pass, click <one thing>, and verify <one immediately visible result>."
  }
]
```

Or trigger a manual dispatch with a `requirements_url` to generate objectives automatically from your requirements doc.

### 5. Push

```bash
git push origin main
```

Watch the run at **Actions → Agentic SDLC — KaneAI Pipeline**.

---

## Running locally

```bash
pip install -r requirements.txt
npm install -g @testmuai/kane-cli@latest

kane-cli login --username $LT_USERNAME --access-key $LT_ACCESS_KEY
kane-cli config project YOUR_TM_PROJECT_ID

# Full pipeline
LT_USERNAME=<u> LT_ACCESS_KEY=<k> ANTHROPIC_API_KEY=<k> \
  TM_PROJECT_ID=<id> TM_ENVIRONMENT_ID=<id> \
  python3 ci/flow2_pipeline.py

# Single SC (for quick testing)
python3 ci/flow2_pipeline.py --sc SC-001

# Skip Phase 1 (reuse last kane-cli sessions)
python3 ci/flow2_pipeline.py --skip-phase1
```

---

## Key files

| File | Purpose |
|------|---------|
| `ci/objectives.json` | Current test objectives (auto-updated by pipeline) |
| `ci/flow2_pipeline.py` | Main pipeline: Phase 1 (kane-cli), Phase 2 (TM), Phase 3 (HE) |
| `ci/self_heal.py` | Cross-run and inline objective healing via Claude |
| `ci/rca.py` | LT AI RCA + Claude fallback RCA |
| `ci/traceability.py` | Builds the requirements → results matrix |
| `ci/generate_objectives.py` | Claude-powered objective generation from ACs |
| `ci/analyze_requirements.py` | Extracts ACs from a requirements URL |
| `hyperexecute.yaml` | HE configuration (discovers kane/ test.py files) |
| `requirements/analyzed_requirements.json` | Extracted ACs (auto-generated, not hand-edited) |

---

## Edge cases

| Situation | Behaviour |
|-----------|-----------|
| SC fails authoring | Infra retry (Tier 1); then up to 2× Claude heal + retry (Tier 2); cross-run heal on next push |
| All SCs fail Phase 1 | Pipeline aborts before creating HE job |
| Phase 2 returns no TC IDs | HE poll skipped immediately (no 30-min timeout) |
| LT AI RCA `triggered=0` | Retries trigger 3× (60s apart); Claude fallback for Phase 1 authoring failures only |
| RCA session returns 404 | Skipped instantly — no retry loop |
| HE session status `completed` | Treated as in-progress retry (not passed) — waits for final result |
| Auto-improve commit | `paths-ignore` on `ci/objectives.json` + `requirements/analyzed_requirements.json` prevents re-triggering |
| Custom URL run | Objectives generated for that URL; never overwrites default saucedemo objectives |
| First run (no history) | Cross-run heal skips gracefully — nothing to heal |
| No `tm_environment_id` provided | Pipeline checks `ci/.working_env_id` from previous run; creates a new compatible env if none found and saves the id for future runs |
| Bad `tm_environment_id` (KaneAI UI-created, non-HE-compatible) | Pipeline detects incompatible env, creates a working replacement, and persists the new id to `ci/.working_env_id` |
| Private Google Doc URL | Download fails — make the doc public ("Anyone with link can view") |