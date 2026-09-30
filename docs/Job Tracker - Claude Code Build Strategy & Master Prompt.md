# Build Strategy: Using Claude Code to Build the Job Application Tracker

## 1. Setup required on your end, before you open Claude Code

**Install Claude Code**

- Install per Anthropic's docs (search "Claude Code install" if you don't already have it)
- Run it from inside an empty project folder you create locally, e.g. `job-tracker/`

**Install uv**

- Follow the official install instructions for your OS
- Confirm with `uv --version`

**Anthropic API key**

- Create one from the Anthropic Console (console.anthropic.com), under API Keys
- Don't paste it into chat with Claude Code or commit it anywhere, you'll drop it into a local `.env` file once the project scaffold exists

**Google Cloud project + credentials**

- Create a new project in Google Cloud Console
- Enable the **Gmail API** and **Google Sheets API** for that project
- Configure the OAuth consent screen: testing mode, add yourself (and later your friends) as test users, scopes needed: `gmail.readonly`, `spreadsheets`, `userinfo.email`
- Create OAuth Client ID credentials (Web application type), download the `credentials.json` file
- Keep this file outside git entirely, you'll tell Claude Code where it lives locally, it should never be committed

**Local project folder**

- Create the empty folder, `git init` inside it, set up `.gitignore` first (before any code exists) with at minimum: `.env`, `credentials.json`, `token*.json`, `__pycache__/`, `.venv/`

**What you give Claude Code at the start of the session**

1. The technical spec (paste it in or point Claude Code at the file if you save it into the repo, e.g. `docs/spec.md`)
2. The master prompt below
3. Confirmation that `credentials.json` exists locally at a path you specify (don't paste its contents into chat)

## 2. Master prompt for Claude Code

Copy this into Claude Code as your starting instruction, after sharing the technical spec:

---

**Project: Job Application Tracker**

I'm building the project described in the attached technical spec. Build this incrementally, in the order below, and confirm each phase works before moving to the next. Don't jump ahead to later phases even if it seems fast to combine steps, I want to test as we go.

**Ground rules:**

- Use `uv` for all dependency management, not raw pip
- Follow the folder structure in the spec exactly
- Set up `.gitignore` before writing any code that touches secrets
- Never print, log, or write my API keys, OAuth credentials, or tokens into any file that isn't already gitignored
- If anything in the spec is ambiguous or you need a decision from me, ask rather than assuming
- Keep functions small and each module scoped to the single responsibility described in the spec (auth, gmail, llm, sheets, agent stay separate)

**Build order:**

**Phase 1: Scaffold** Set up the folder structure from the spec, `.gitignore`, `requirements`/`pyproject.toml` via uv, `.env.example` listing required env vars (without real values), empty `README.md`. Confirm the project runs (even just a "hello world" FastAPI route) before moving on.

**Phase 2: Google OAuth** Implement `/auth/login` and `/auth/callback`. I should be able to run the app locally, click connect, go through Google's consent screen, and see a success response with my tokens stored locally (not committed). Stop here and let me test this manually before continuing.

**Phase 3: Gmail fetch** Implement the Gmail client to pull messages since August 25, 2026, plus the pre-filter step to skip obvious noise (promotions/social categories). Print/log the count and subjects of candidate messages so I can sanity check before we send anything to an LLM. Stop and let me review before continuing.

**Phase 4: LLM extraction** Implement the classification + extraction call to Claude Haiku 4.5 per the spec's prompt structure, returning strict JSON (is_job_related, company, role, status, confidence). Include the error handling described in the spec (malformed JSON doesn't crash the run). Test against the candidate messages from Phase 3 and show me the extracted results before continuing.

**Phase 5: Sheets integration** Implement Sheet creation and row writing per the schema in the spec (including dedup by tracked message ID, and the summary counts section). Wire this up to Phases 3-4 so a full rescan populates the Sheet. This is the core v1 loop, extraction + Sheet population, so test this thoroughly.

**Phase 6: Stale detection + follow-up drafts** Implement the staleness check using the thresholds in the spec (7 days post-interview, 28 days post-applied-no-interview) and the follow-up draft generation, written to the Draft Follow-Up column. Never auto-send, this only writes drafts to the Sheet.

**Phase 7: Frontend** Build the single-page landing page described in the spec (Connect Gmail button, then post-connect state with Open Sheet / Rescan buttons). Keep it plain HTML/JS, no framework.

**Phase 8: Wrap-up** Write the real README (what it does, setup instructions, architecture overview), double check nothing secret is tracked in git (`git status`, review `.gitignore` coverage), and give me a final summary of what's built vs what's spec'd but not yet done (per the spec's "out of scope for v1" and "v1 scope" sections).

**After each phase**, tell me explicitly: what you built, how to test it, and what you need from me (if anything, like confirming OAuth worked) before you continue to the next phase.

---

## 3. Notes for you while running this

- Expect Phase 2 (OAuth) to be the most likely place something goes sideways, budget real time here, don't rush it
- If Claude Code suggests deploying before Phase 6 is done, push back, per the spec, local-first until the core loop works
- Keep the spec doc open alongside the Claude Code session so you can quickly correct it if it drifts from a decision you already made (e.g., staleness thresholds, pull range)