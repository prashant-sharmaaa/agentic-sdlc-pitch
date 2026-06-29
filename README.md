# Agentic SDLC — LambdaTest

> Requirements go in. **KaneAI authors the tests.** HyperExecute runs them. AI explains failures. Objectives auto-improve every run.

---

## What this is

A fully automated testing pipeline where **KaneAI** — LambdaTest's AI browser agent — authors every test case from a plain-English objective. No selectors. No scripts. No page objects.

You give KaneAI an objective:

> *"Login to saucedemo as standard_user, add Sauce Labs Backpack to cart, and verify the button changes to 'Remove'."*

KaneAI opens a real browser, figures out the UI, executes the steps, and saves the verified test directly into LambdaTest Test Manager. HyperExecute runs it in parallel. If it fails, Claude explains why and rewrites the objective — automatically, before the next run.

---

## The Pipeline

```
Push to main  ──────────────────────────────────────────────────────────────►
                                                                              │
        [optional: requirements URL provided via manual dispatch]             │
                    ↓                                                         │
           Claude extracts ACs                                                │
                    ↓                                                         │
           Claude writes crisp objectives (max 5)                            │
                    ↓                                                         │
              committed to ci/objectives.json                                 │
                                                                              │
◄─────────────────────────────────────────────────────────────────────────────┘
                    ↓
         kane-cli runs each objective on a real browser (KaneAI)
         ├── inline self-heal: if it fails, Claude rewrites + retries immediately
         └── saves verified test to LambdaTest Test Manager
                    ↓
         Test Run created → linked to HyperExecute
                    ↓
         HyperExecute runs all tests in parallel (5 VMs)
         └── pipeline polls until complete
                    ↓
         AI Root Cause Analysis
         ├── LT AI RCA (when available)
         └── Claude RCA fallback from authoring failure detail
                    ↓
         Traceability Matrix → GitHub Step Summary
         └── includes Test Run Report link
                    ↓
         Auto-improve: Claude rewrites objectives for all failed SCs
         └── commits improved objectives.json back to main [skip ci]
```

On the **next push**, the pipeline starts from the improved objectives automatically.

---

## Triggers

| How | What happens |
|-----|-------------|
| **Push to `main`** (`ci/`, `requirements/`, `scenarios/`, `hyperexecute.yaml`) | Pipeline auto-runs using committed `objectives.json` |
| **Manual dispatch — no URL** | Same as push — uses committed `objectives.json` |
| **Manual dispatch + requirements URL** | Downloads doc → Claude extracts ACs → Claude generates objectives → full pipeline |

> Auto-improve commits use `[skip ci]` in the message so they don't re-trigger the pipeline.

---

## Stage-by-stage

### Stage 1 — Requirements Analysis *(runs only when a requirements URL is provided)*

Claude reads the document and extracts Acceptance Criteria.

**Supported URL formats:**

| Source | Example URL | Requirement |
|--------|-------------|-------------|
| Google Docs | `docs.google.com/document/d/<ID>/edit` | Share → Anyone with link can view |
| Google Drive file | `drive.google.com/file/d/<ID>/view` | Share → Anyone with link can view |
| GitHub file | `github.com/user/repo/blob/main/reqs.md` | Public repo or raw URL |
| GitHub Gist | `gist.github.com/user/<ID>` | Public gist |
| Dropbox | `dropbox.com/s/.../file.txt?dl=0` | Shared link |
| Any raw URL | `https://example.com/requirements.txt` | Publicly accessible |

All formats are auto-detected and converted — paste the URL exactly as you'd share it.

**Output:** `requirements/analyzed_requirements.json`

---

### Stage 2 — Objective Generation *(runs only when a requirements URL is provided)*

Claude generates up to 5 crisp, intent-based objectives from the extracted ACs.

**Format enforced (strictly):**
```
Login to <url> as <user> with password <pass>, <one action>, and verify <one assertion>.
```

**Good:**
```
Login to https://www.saucedemo.com/ as standard_user with password secret_sauce,
add Sauce Labs Backpack to the cart, and verify the button changes to 'Remove'.
```

**Bad (never do this):**
```
Navigate to the URL, type 'standard_user' into the username field, click the
button below the '$29.99' price label, scroll down to find...
```

**Output:** `ci/objectives.json` — committed to the repo for reuse across runs.

---

### Stage 3 — KaneAI Authoring *(Phase 1)*

`kane-cli run` executes each objective on a real browser. KaneAI uses AI vision — no pre-written selectors or scripts. Each verified test is saved into LambdaTest Test Manager.

- **2 SCs run in parallel**
- **600s timeout per SC** — no step limit (kane-cli decides how many steps)
- **Inline self-heal:** on failure, Claude reads the kane-cli failure detail and rewrites the objective, retrying immediately in the same run
- **Cross-run self-heal:** `run_history.json` is saved as a GitHub artifact; on the next run it's restored and Claude pre-heals any objectives that failed last time before the run starts

---

### Stage 4 — Test Run Creation *(Phase 2)*

Creates a LambdaTest Test Manager test run, links all authored test cases, and sets the execution environment.

| Setting | Value |
|---------|-------|
| Environment | Windows 10, Firefox (latest), desktop web |
| Concurrency | 5 parallel VMs |
| Retry on failure | 1 retry |

---

### Stage 5 — HyperExecute Execution *(Phase 3)*

Triggers HyperExecute via KaneAI TM API. The pipeline polls until the job reaches a final state before proceeding.

**Output links (in Step Summary):**
- HyperExecute job dashboard
- 📋 Test Run Report (`test-manager.lambdatest.com/...?type=report`)

---

### Stage 6 — Root Cause Analysis

Two-layer RCA for every failed scenario:

**Layer 1 — LT AI RCA** (when sessions are indexed):
- Triggers `POST /insights/api/v3/public/rca/generate`
- Polls per-session RCA endpoint (404 → skips immediately, no retry loop)
- Summarised to 3 bullets by Claude Haiku

**Layer 2 — Claude RCA fallback** (always available):
Uses the kane-cli authoring failure detail captured in Phase 1:
```
Objective:              what was asked of kane-cli
What CLI did:           what the agent attempted / where it got stuck
What needs to be done:  concrete fix to the objective or test
```

**Output:** `ci/rca_results.json`

---

### Stage 7 — Traceability Matrix

Full matrix linking every result back to its origin — appears in GitHub Step Summary after every run.

```
AC → Objective → SC → TM Test Case → HE Result → AI RCA
```

---

### Stage 8 — Auto-Improve *(end of every run)*

After HE results and RCA are available, Claude rewrites objectives for **all failed SCs**:

- Phase 1 authoring failure → uses kane-cli failure detail
- HE execution failure → uses RCA text (LT AI or Claude)

The improved `objectives.json` is committed back to `main` with `[skip ci]`. The next push automatically starts from better objectives.

---

## Forking this repo

### 1. GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Description | Where to get it |
|--------|-------------|-----------------|
| `LT_USERNAME` | LambdaTest account username | [accounts.lambdatest.com/security](https://accounts.lambdatest.com/security) |
| `LT_ACCESS_KEY` | LambdaTest access key | [accounts.lambdatest.com/security](https://accounts.lambdatest.com/security) |
| `ANTHROPIC_API_KEY` | Claude API key | [console.anthropic.com](https://console.anthropic.com) |
| `KANE_PROJECT_ID` | KaneAI project ID | KaneAI → your project → Settings → copy the project ID |
| `KANE_FOLDER_ID` | KaneAI folder ID | KaneAI → your project → Folders → select folder → copy ID |

### 2. Code changes for your project

Open `ci/flow2_pipeline.py` and update these two constants at the top:

```python
PROJECT_ID    = "YOUR_KANE_PROJECT_ID"     # same as KANE_PROJECT_ID secret
ENVIRONMENT_ID = YOUR_ENVIRONMENT_ID        # integer — see note below
```

**Finding your `ENVIRONMENT_ID`:**  
In LambdaTest Test Manager → Environments → create or select an environment → the ID appears in the URL or API response.

### 3. Update objectives

Replace `ci/objectives.json` with objectives for your app:

```json
[
  {
    "id": "SC-001",
    "ac_id": "AC-001",
    "name": "SC-001: short description",
    "objective": "Login to https://yourapp.com as user with password pass, do one thing, and verify one result."
  }
]
```

Keep objectives short and intent-based — one action, one assertion, credentials inline. See the [Objective format](#stage-2----objective-generation-runs-only-when-a-requirements-url-is-provided) section above.

### 4. Push

```bash
git push origin main
```

Pipeline triggers automatically. Watch the run at **Actions → Agentic SDLC — KaneAI Pipeline**.

---

## Manual dispatch options

**Actions → Agentic SDLC — KaneAI Pipeline → Run workflow**

| Field | Options | Use when |
|-------|---------|----------|
| `requirements_url` | Any URL or blank | Provide a doc URL to generate new objectives; leave blank to use committed `objectives.json` |
| `from_step` | `1`, `2`, `3` | `1` = full run with URL; `3` = skip to pipeline (default) |

---

## Running locally

```bash
pip install -r requirements.txt
npm install -g @testmuai/kane-cli@latest

# Login and configure kane-cli
kane-cli login --username $LT_USERNAME --access-key $LT_ACCESS_KEY
kane-cli config project YOUR_KANE_PROJECT_ID
kane-cli config folder  YOUR_KANE_FOLDER_ID

# Run the full pipeline
LT_USERNAME=<u> LT_ACCESS_KEY=<k> ANTHROPIC_API_KEY=<k> python3 ci/flow2_pipeline.py

# Run a single SC for testing
LT_USERNAME=<u> LT_ACCESS_KEY=<k> ANTHROPIC_API_KEY=<k> python3 ci/flow2_pipeline.py --sc SC-001

# Skip Phase 1 (reuse last kane-cli sessions)
LT_USERNAME=<u> LT_ACCESS_KEY=<k> python3 ci/flow2_pipeline.py --skip-phase1
```

---

## Edge cases

| Situation | Behaviour |
|-----------|-----------|
| kane-cli SC fails authoring | Inline Claude heal + immediate retry; cross-run heal on next push |
| All SCs fail Phase 1 | Pipeline aborts before creating HE job |
| LT AI RCA unavailable (`triggered=0`) | Claude generates RCA from kane-cli failure detail |
| RCA session returns 404 | Skipped instantly — no retry loop |
| HE not finished when RCA runs | Pipeline polls until all sessions reach final state |
| Auto-improve commit pushes to main | `[skip ci]` prevents re-triggering the pipeline |
| First run (no prior artifact) | Cross-run heal step skips gracefully |
| Requirements URL is a private Google Doc | Download fails — make the doc publicly shared first |
