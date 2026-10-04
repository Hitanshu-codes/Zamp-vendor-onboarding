# Vendor Onboarding Automation

A vendor submits a form and three documents (tax registration certificate, bank letter or cancelled cheque, certificate of incorporation). The system returns **approved / pending / rejected**, shows every check with its evidence and reasoning, and then either sends the vendor one specific request for fixes or puts the case in front of a human reviewer.

> **AI reads the documents, plain rules make the decision, people handle the risky cases.**

- **Step-by-step setup, publish and deploy:** [`SETUP.md`](SETUP.md)
- **How it works (plain English):** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **5-minute live demo script:** [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) · **video script:** [`docs/VIDEO_SCRIPT.md`](docs/VIDEO_SCRIPT.md)
- **Every design decision, one line each:** [`docs/DECISIONS.md`](docs/DECISIONS.md)
- **The original brief and the owner's amendments:** [`docs/PROMPT.md`](docs/PROMPT.md)

## Run locally (one command after setup, no API key needed)

```bash
py -3.12 -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000. The database is created and seeded on first boot. Without a `.env` the app runs in **replay** mode: all 28 sample scenarios work offline from recorded AI answers. Copy `.env.example` to `.env` to configure keys and options. On macOS/Linux use `python3.12` and `.venv/bin/python`.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

About 220 tests, all offline (API keys are blanked in tests). They cover the decision engine (including property-based tests for the "nothing but passing checks can approve" invariant), validators, name and account-holder logic, injection and hidden-text detection, the LLM client's failure modes, every sample scenario through the real pipeline in replay mode, the kill switch never approving, emails containing no vendor text beyond cleaned-up names, and HTML escaping of `<script>` payloads everywhere.

Run the scenarios directly:

```bash
.venv/Scripts/python.exe scripts/run_scenarios.py replay all
```

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | (none) | Google Gemini key. Needed for `live`, `record` and new inputs in `auto`. |
| `LLM_MODE` | `replay` (code), `auto` (Docker/Render) | `auto`: use a recorded answer if one exists, otherwise call Gemini live and record it · `live` · `record` · `replay` (offline; missing recording → manual review). Also switchable in **Settings**. |
| `MODEL_EXTRACT`, `MODEL_JUDGE` | `gemini-3.5-flash-lite` | Models for document reading and second opinions (chosen after benchmarking; see DECISIONS #20). |
| `SEARCH_PROVIDER` | `gemini` | `gemini` (Google Search grounding, falling back to curated official sources) · `directory` · `fixture` |
| `DEMO_PASSWORD` | (none) | Shared password for the whole app. Empty = no login (local only). |
| `SESSION_SECRET` | random per boot | Set it so logins survive restarts. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Used in vendor email links. |
| `EMAIL_PROVIDER` | `outbox` | `outbox` (store and preview only) or `resend` (deliver vendor emails). |
| `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_REDIRECT_TO` | | Resend delivery. Until a domain is verified in Resend, set `EMAIL_REDIRECT_TO` to your Resend account email; every vendor email goes there, marked with the original recipient. |
| `DAILY_LLM_CALL_CAP`, `SUBMISSIONS_PER_IP_PER_HOUR` | 500, 60 | Budget and abuse protection for the public demo. |
| Thresholds (`NAME_FUZZY_*`, `SANCTIONS_FUZZY_THRESHOLD`, `RISK_POINTS_REVIEW_THRESHOLD`, …) | see `.env.example` | Shown read-only in Settings. |

## Deploy

### Docker (any host)

```bash
docker build -t vendor-onboarding .
```

```bash
docker run -p 8000:8000 -e DEMO_PASSWORD=change-me -e GEMINI_API_KEY=your-key vendor-onboarding
```

The image runs as a non-root user, serves on `$PORT` (default 8000), exposes `/healthz`, seeds itself on boot, and defaults to `LLM_MODE=auto`.

### Render (blueprint included)

1. Push this repository to GitHub.
2. In Render: **New → Blueprint →** select the repo. `render.yaml` creates a Docker web service on the free plan.
3. Enter the secrets when prompted: `GEMINI_API_KEY`, `DEMO_PASSWORD`, `PUBLIC_BASE_URL` (your `https://….onrender.com` URL) and, optionally, the Resend settings.
4. Deploy, open the URL and sign in with `DEMO_PASSWORD`.

**Free tier notes:** the instance sleeps after about 15 minutes idle (the first request takes 30–60 s), and the disk is ephemeral, so data resets on every restart or redeploy. That's acceptable for a demo: the app re-seeds itself, and the USA becomes "first seen" again. **Open the URL a few minutes before a demo.** For persistence, use a paid instance with a disk mounted at `DATA_DIR`.

### Railway

Create a service from the repo; Railway detects the Dockerfile. Add the same variables as above. Optionally attach a volume at `/app/data` for persistence.

## Project layout

```
app/
  main.py, config.py, db.py, clock.py, events.py, audit.py   core
  pipeline/          runner, 11 stages, check registry (inputs drive partial re-runs), forms
  decision/          engine.py (pure decide()), reason_codes.py (catalogue)
  domain/            names, account holder, tax IDs, banking, sanctions, watchlist, vendor master, self-test
  llm/               client (structured output, record/replay, kill switch), prompts, schemas, tasks
  security/          injection scan + hidden-text detection, auth, sanitisers
  comms/             email templates, asks, outbox (Resend)
  review.py, scheduler.py, search.py, tax_registry.py, watchlist_admin.py
  web/               routes, templates, static (no build step)
samples/             28 generated scenarios (form.json + PDFs + expected.json)
recordings/          recorded AI answers and fetched pages for offline replay
fixtures/search/     offline and poisoned search fixtures
scripts/             generate_samples.py, run_scenarios.py, seed.py, llm_probe.py
tests/
```

All data is fictional: companies, people, tax IDs, bank accounts, and the sanctions list.
