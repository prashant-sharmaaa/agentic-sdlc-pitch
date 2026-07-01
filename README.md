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
         ├── Tier 2 — Inline self-heal: logic failure → Claude rewrites → immediate retry
         └── saves verified test to Testmu AI Test Manager
                    ↓
         Test Run created in TM → instances linked with environment
                    ↓
         HyperExecute runs all tests in parallel (5 VMs, 1 retry per test)
         └── pipeline polls until all sessions reach final state
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

| How | What happens |
|-----|-------------|
| **Push to `main`** (`ci/`, `requirements/`, `hyperexecute.yaml`) | Pipeline auto-runs using committed `objectives.json` |
| **Manual dispatch — no URL** | Same as push — uses committed `objectives.json` |
| **Manual dispatch + requirements URL** | Downloads doc → Claude extracts ACs → Claude generates objectives → full pipeline |

> Auto-improve commits use `[skip ci]` so they don't re-trigger the pipeline indefinitely.

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
- **Tier 2 (Claude heal + retry):** If the failure persists after an infra retry, it's a logic problem — the objective is ambiguous, too specific about UI coordinates, or chains too many actions. That's when Claude rewrites it.

### Why 5 VMs for HyperExecute?

5 VMs runs all 5 SCs simultaneously in under 2 minutes. More VMs would not reduce wall-clock time further since each SC maps to one session. Fewer VMs would serialise tests unnecessarily.

### Why 1 retry on HyperExecute?

Browser tests on real infrastructure can be flaky for reasons unrelated to the test logic (page load timing, transient network blip). A single retry catches these without masking real failures — two consecutive failures of the same test almost always indicate a genuine bug or a broken objective.

### Why `[skip ci]` on auto-improve commits?

The auto-improve step commits an updated `objectives.json` at the end of every run. Without `[skip ci]`, that commit would trigger a new pipeline run, which would commit another update, which would trigger another run — an infinite loop. The tag tells GitHub Actions to skip this commit.

### Why is the objective format strictly enforced?

KaneAI works best with **intent-based** objectives — describe what to verify, not how to click. Chained actions ("add to cart and then navigate to cart page") require KaneAI to hold intermediate state and are prone to timing issues. State transitions ("verify the button changes to Remove") are inherently flaky because they depend on the exact moment the assertion fires. One action + one immediately visible assertion is the pattern that produces the most reliable, reusable test cases.

### Why does the reuse check exist?

If an objective is unchanged from the last run and a valid TM test case already exists, re-running kane-cli would produce an identical result. Skipping kane-cli for those SCs can save 5–8 minutes of authoring time per run. Only changed objectives or first-time SCs go through the full authoring cycle.

### Why does LT AI RCA need a 120s pre-wait?

The LT automation sessions API (used to fetch session results) indexes sessions almost immediately after they complete. The LT insights engine (used for AI RCA) is a separate system that ingests from the sessions API asynchronously — it typically lags by 2–3 minutes. Calling the RCA trigger too early returns `triggered=0` because the engine hasn't seen the sessions yet. The 120s wait is a safety buffer so the trigger finds the sessions on the first or second attempt.

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
- **Tier 2 — Inline self-heal:** logic failure after infra retry → Claude (`claude-sonnet-4-6`) rewrites objective on-the-fly → immediate retry in the same run
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

The pipeline polls the automation sessions API until all sessions for this job reach a final state (`passed`, `failed`, `cancelled`) before proceeding to RCA.

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

The improved `objectives.json` is committed with `[skip ci]`. The next push automatically starts from better objectives — no human intervention needed.

> Custom URL runs (`REQUIREMENTS_URL` set) never overwrite the committed saucedemo objectives — only default runs auto-improve.

---

## Forking this repo

### 1. GitHub Secrets

Go to **Settings → Secrets and variables → Actions → Secrets**

| Secret | Description | Where to get it |
|--------|-------------|-----------------|
| `LT_USERNAME` | Testmu AI username | [accounts.lambdatest.com/security](https://accounts.lambdatest.com/security) |
| `LT_ACCESS_KEY` | Testmu AI access key | [accounts.lambdatest.com/security](https://accounts.lambdatest.com/security) |
| `ANTHROPIC_API_KEY` | Claude API key | [console.anthropic.com](https://console.anthropic.com) |

### 2. GitHub Variables

Go to **Settings → Secrets and variables → Actions → Variables**

| Variable | Description | How to find it |
|----------|-------------|----------------|
| `TM_PROJECT_ID` | Test Manager project ID (ULID) | KaneAI → your project → URL contains the project ID |
| `TM_ENVIRONMENT_ID` | Test environment ID (integer) | Test Manager → Environments → create or select → ID in API response |

If these variables are not set, the pipeline falls back to the default saucedemo project and environment.

### 3. Configure kane-cli for your project

```bash
kane-cli login --username $LT_USERNAME --access-key $LT_ACCESS_KEY
kane-cli config project YOUR_TM_PROJECT_ID
kane-cli config folder  YOUR_TM_FOLDER_ID   # optional: scope to a folder
```

Update the `kane-cli config project` step in `.github/workflows/flow2.yml` with your project ID.

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
| SC fails authoring | Infra retry first; then inline Claude heal + retry; cross-run heal on next push |
| All SCs fail Phase 1 | Pipeline aborts before creating HE job |
| Phase 2 returns no TC IDs | HE poll skipped immediately (no 30-min timeout) |
| LT AI RCA `triggered=0` | Retries trigger 3× (60s apart); Claude fallback for Phase 1 authoring failures only |
| RCA session returns 404 | Skipped instantly — no retry loop |
| HE session status `completed` | Treated as in-progress retry (not passed) — waits for final result |
| Auto-improve commit | `[skip ci]` prevents re-triggering the pipeline |
| Custom URL run | Objectives generated for that URL; never overwrites default saucedemo objectives |
| First run (no history) | Cross-run heal skips gracefully — nothing to heal |
| Private Google Doc URL | Download fails — make the doc public ("Anyone with link can view") |