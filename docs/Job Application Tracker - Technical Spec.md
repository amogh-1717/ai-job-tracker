# Job Application Tracker — Technical Spec

## 1. Overview

An AI-powered tool that connects to a user's Gmail, extracts job application data from messy inbox emails using an LLM, writes structured results to a Google Sheet, and flags stale applications with drafted follow-ups.

**Core problem solved:** manual tracking of job application status across dozens of messy, differently-formatted emails.

**AI's role (load-bearing, not decorative):** classification (is this email job-related?) and extraction (pull company/role/status from unstructured text) via Claude Haiku. Secondary agent behavior: detecting stale applications and drafting follow-up emails.

## 2. Architecture / Folder Structure

```
job-tracker/
├── backend/
│   ├── main.py                 # FastAPI app entrypoint, route definitions
│   ├── auth/
│   │   ├── oauth.py             # Google OAuth flow (init, callback, token storage)
│   │   └── token_store.py       # Simple token persistence (file or SQLite, per-user)
│   ├── gmail/
│   │   ├── client.py            # Gmail API wrapper: list/get messages
│   │   └── filters.py           # Pre-LLM filtering (skip promotions/social/etc.)
│   ├── llm/
│   │   ├── extractor.py         # Prompt + call to Claude Haiku, parses JSON response
│   │   └── prompts.py           # Prompt templates (classification + extraction, follow-up draft)
│   ├── sheets/
│   │   ├── client.py            # gspread wrapper: create sheet, read/write rows
│   │   └── schema.py            # Column definitions, summary tab logic
│   ├── agent/
│   │   └── stale_check.py       # Logic: find stale rows, trigger follow-up drafting
│   ├── models.py                # Pydantic models: ExtractedApplication, EmailRecord, etc.
│   ├── config.py                # Env var loading, constants (staleness threshold, etc.)
│   └── eval/
│       └── accuracy_check.py    # Manual-label comparison script for measuring extraction accuracy
├── frontend/
│   └── index.html                # Single-page landing (Connect Gmail / Rescan / Open Sheet)
├── .env.example                  # Template for required env vars (no real secrets)
├── .gitignore                    # .env, credentials.json, token files, __pycache__
├── requirements.txt
└── README.md
```

**Why this shape:** each integration (Gmail, LLM, Sheets) is isolated in its own module so you can test/debug one piece without the others. The `agent/` folder is separate from `llm/` because staleness-checking is business logic that *uses* the LLM, not LLM logic itself, keeps the extraction prompt reusable.

## 3. Components

### 3.1 Backend (FastAPI)

Routes needed:

- `GET /` — serves the landing page state
- `GET /auth/login` — redirects to Google's OAuth consent screen
- `GET /auth/callback` — handles the OAuth redirect, exchanges code for tokens, stores them, creates the user's Sheet if it doesn't exist, sets a session cookie identifying this user
- `POST /rescan` — triggers a fresh Gmail scan + extraction + Sheet update for the authenticated user
- `GET /status` — returns whether the user is connected and their Sheet link (used by frontend to switch states)

**Session/user identification:** even for personal use, once more than one person (you + a friend) uses this, the backend needs to know whose tokens to use on each request. Use a simple session cookie set on `/auth/callback`, mapped to that user's stored tokens (keyed by their Google account email). Don't skip this even for a "just me" v1, it's a small addition now and a painful retrofit later.

### 3.2 Auth

- Scopes: `gmail.readonly`, `spreadsheets`, `userinfo.email`
- Testing-mode OAuth consent screen (no verification needed at this scale)
- Tokens expire every 7 days in testing mode — store refresh token, handle re-auth gracefully (if a Gmail/Sheets call fails with an auth error, prompt reconnect rather than crashing)

### 3.3 Gmail integration

- Pull emails since August 25, 2026 (start of application season), not a rolling window — ensures nothing active gets missed on the first scan
- Pre-filter obviously irrelevant emails (promotions/social categories, common noise senders) before sending to the LLM — saves cost and reduces false positives
- **Dedup on rescan:** track processed Gmail message IDs (store in a hidden Sheet column, or a small local cache/table) and skip any message already classified in a prior scan. Without this, every rescan reprocesses the same old emails, wasting LLM calls and risking duplicate/conflicting rows if fuzzy company-matching misses.
- **Thread handling:** decide explicitly whether you're pulling by individual message or by thread. A single thread (e.g., confirmation → rejection weeks later) can span multiple messages; if you only check the latest message per thread you may miss an earlier status change, and if you process every message independently you may create duplicate rows for the same application. Recommended: process by thread, use the most recent message's extracted status, but scan all messages in the thread for the extraction (in case the rejection reply is short and the useful context is earlier in the thread).

### 3.4 LLM extraction

- One call per candidate email
- Prompt asks for strict JSON output: `{is_job_related: bool, company: str, role: str, status: enum, confidence: float}`
- Status enum: `applied | oa | interview | rejected | offer | ghosted | other`
- Low-confidence extractions still get written to the Sheet but flagged for manual review (e.g., a "Needs Review" note)
- **Error handling:** wrap the JSON parse of the LLM response in try/except. LLMs occasionally return malformed or non-schema-conforming output. On failure: log the raw response, skip that email (don't crash the whole rescan), and optionally retry once with a stricter prompt reminder before giving up.

### 3.5 Sheets integration

- One row per application: `Company | Role | Status | Date Applied | Last Updated | Notes | Draft Follow-Up`
- Separate summary section (top rows or second tab) with counts per status, computed via formula or backend-written
- On rescan: match existing rows by company+role (fuzzy match to handle "Google" vs "Google LLC"), update status/last-updated if changed, append new rows for new applications

### 3.6 Agent: stale detection

- Runs as part of `/rescan` (no separate scheduler needed for v1)
- Staleness thresholds vary by status, not a single flat rule:
  - Post-interview, no update: flag after 7 days
  - Post-applied, no interview yet: flag after 28 days
- For each row where `status` is non-terminal (not rejected/offer) and past its threshold: generate a draft follow-up via LLM, write it to the `Draft Follow-Up` column
- Never auto-sends. User copies/sends manually. This is a deliberate safety boundary.

### 3.7 Frontend

- Single HTML page, two states (disconnected / connected), described in earlier discussion
- No framework needed, vanilla JS handles the OAuth redirect and button state toggling based on `/status`

## 4. Data flow (end to end)

1. User clicks Connect Gmail → OAuth flow → backend stores tokens, creates Sheet
2. User clicks Rescan (or it's called automatically post-auth)
3. Backend fetches candidate emails from Gmail
4. Each candidate goes through LLM classification/extraction
5. Results merged into the Sheet (new rows added, existing rows updated)
6. Stale-check runs against current Sheet state, drafts follow-ups where needed
7. Summary counts update
8. User opens their Sheet to review

## 5. Extraction accuracy measurement

Worth building even though it's not user-facing, this is what turns "it uses AI" into a real technical claim you can cite.

- Manually label a small set (15-20 emails) with the correct classification and extraction values
- Run the pipeline against them, compare LLM output to your labels
- Track precision/recall on classification (is this job-related) and field-level accuracy on extraction (company, role, status correct)
- Costs about 20-30 minutes, gives you a concrete number ("X% classification accuracy on a hand-labeled test set") for your README and for interviews, much stronger than an unquantified "it uses AI to extract stuff"

## 6. Security / privacy notes

- No email content or tokens stored outside the user's own OAuth token (encrypted at rest if using a DB, or file-permission-restricted if local)
- `.env`, `credentials.json`, and any token files are gitignored, never committed
- README should state clearly this is a personal-use/demo project, not intended for production multi-tenant use without further hardening

## 7. Explicitly out of scope for v1

- Auto-sending follow-up emails
- LinkedIn contact matching (bonus, only if time allows after core works)
- Scheduled/automatic rescanning (manual button is enough for v1)
- Custom dashboard frontend (Sheets is the UI)

## 8. Tech stack summary

| Layer | Choice |
| --- | --- |
| Backend | Python, FastAPI |
| Dependency management | uv |
| Auth | google-auth-oauthlib, google-api-python-client |
| Gmail | Gmail API |
| LLM | Anthropic API, Claude Haiku 4.5 |
| Storage/UI | Google Sheets API (gspread) |
| Frontend | Static HTML/JS |
| Hosting | Render or Railway (backend), GitHub Pages or Vercel (frontend) |

**Dev workflow:** build and test entirely on localhost first. Deploy to Render/Vercel only once the core extraction → Sheet loop works end to end. Deploying early risks burning weekend time on infra debugging before the actual logic is proven.

## 9. v1 scope (definition of done)

Ship this, end to end, working, by the end of the weekend:

- Gmail OAuth connect
- Extraction pipeline (classify + extract) populates the Sheet
- Stale flagging with drafted follow-ups

Everything else in this spec (accuracy eval, LinkedIn matching, dedup/thread edge cases beyond the basics) is a bonus if time allows, not a blocker for calling this shipped.