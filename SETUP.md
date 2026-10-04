# Setup: run, publish and deploy

A step-by-step guide from a fresh machine to a public, password-protected demo URL.

| Step | Time | You need |
|---|---|---|
| 1. Run locally | 5 min | Python 3.12, Git |
| 2. Add API keys (optional) | 5 min | Gemini API key, Resend API key |
| 3. Check everything works | 5 min | (none) |
| 4. Publish to GitHub | 2 min | GitHub account + GitHub CLI (`gh`) |
| 5. Deploy on Render | 10 min | Render account (free) |
| 6. Before every demo | 3 min | (none) |

Commands are shown for **Windows (PowerShell)**. macOS/Linux equivalents are noted where they differ.

---

## 1. Run locally

### 1.1 Install the prerequisites
- **Python 3.12**: https://www.python.org/downloads/ (on Windows, tick "Add python.exe to PATH"). Check it with `py -3.12 --version`.
- **Git**: https://git-scm.com/downloads

### 1.2 Get the code
If you already have the folder, skip this. Otherwise, after step 4:

```powershell
git clone https://github.com/<your-account>/vendor-onboarding.git
```

```powershell
cd vendor-onboarding
```

### 1.3 Create a virtual environment and install dependencies

```powershell
py -3.12 -m venv .venv
```

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

*macOS/Linux:* `python3.12 -m venv .venv`, then `.venv/bin/python -m pip install -r requirements-dev.txt`.

### 1.4 Start the app

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Open **http://localhost:8000**. On first start the database is created in `data/` and filled with demo data (vendors, a mock sanctions list, watchlist entries and tax rules).

**No keys are needed.** Without a `.env` file the app runs in **replay** mode, and all 28 sample scenarios work offline from recorded AI answers. Pick one under **Submit → Load sample scenario**.

Stop the server with `Ctrl+C`.

---

## 2. Add API keys (optional, needed for new documents)

### 2.1 Create your `.env`

```powershell
copy .env.example .env
```

*macOS/Linux:* `cp .env.example .env`

Open `.env` in an editor and fill in what you need. **Never commit this file.** It is already in `.gitignore`.

### 2.2 Gemini (reads documents you upload yourself)
1. Create a key at https://aistudio.google.com/apikey
2. In `.env`:
   ```
   GEMINI_API_KEY=your-key
   LLM_MODE=auto
   ```
   `auto` uses a recorded answer when one exists (the samples), otherwise it calls Gemini live and records the answer.

> **Web search note:** live Google Search grounding needs a Gemini key with billing enabled. Without it, search calls return "quota exceeded" and the app automatically falls back to a short list of official tax-authority pages (still fetched live, scanned and self-tested). Everything works either way.

### 2.3 Resend (send real vendor emails; optional)
By default emails are only stored and shown in the app (`EMAIL_PROVIDER=outbox`). To actually send them:
1. Create a key at https://resend.com/api-keys
2. In `.env`:
   ```
   EMAIL_PROVIDER=resend
   RESEND_API_KEY=your-key
   EMAIL_FROM=Vendor Onboarding <onboarding@resend.dev>
   EMAIL_REDIRECT_TO=the-email-you-signed-up-to-resend-with@example.com
   ```
   Until you verify your own domain in Resend, it only delivers to your account's email address. `EMAIL_REDIRECT_TO` sends every vendor email there, marked with the original recipient (the sample vendors' addresses are fictional).

### 2.4 Password (recommended whenever the app is reachable by others)
```
DEMO_PASSWORD=choose-a-long-password
SESSION_SECRET=any-long-random-string
```
With `DEMO_PASSWORD` empty there is no login, which is fine on your own machine only.

Restart the server after editing `.env`.

---

## 3. Check everything works

Run the automated tests (about 4 minutes, fully offline; they never use your keys):

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Expected: `236 passed` (or more).

Run every sample scenario through the real pipeline and compare it with the expected outcome:

```powershell
.venv\Scripts\python.exe scripts\run_scenarios.py replay all
```

Expected: `28/28 scenarios as expected`.

Optional: if you change a sample or a prompt, record fresh AI answers (uses your Gemini key):

```powershell
.venv\Scripts\python.exe scripts\run_scenarios.py record S0 S1
```

---

## 4. Publish to GitHub

### 4.1 Make sure nothing secret is tracked
This should print `.env` (meaning it is ignored):

```powershell
git check-ignore .env
```

And this should print nothing:

```powershell
git ls-files .env data data-preview
```

### 4.2 Create the repository and push
Install the GitHub CLI from https://cli.github.com/ if you don't have it, then sign in:

```powershell
gh auth login
```

Create a **private** repository from this folder and push everything in one step:

```powershell
gh repo create vendor-onboarding --private --source . --push
```

*Without the GitHub CLI:* create an empty private repository on github.com, then run `git remote add origin https://github.com/<you>/vendor-onboarding.git` and `git push -u origin main`.

Later changes:

```powershell
git push
```

---

## 5. Deploy on Render (free)

The repository includes `render.yaml` (a Render Blueprint) and a `Dockerfile`, so Render knows how to build and run the app.

### 5.1 Create the service
1. Sign up at https://render.com and **connect your GitHub account** (this lets Render read your private repo).
2. **New → Blueprint →** choose the `vendor-onboarding` repository → **Apply**.
3. Render asks for the values marked secret in `render.yaml`:

   | Variable | Value |
   |---|---|
   | `GEMINI_API_KEY` | your Gemini key |
   | `DEMO_PASSWORD` | the password you'll give evaluators |
   | `PUBLIC_BASE_URL` | leave blank for now (set it in 5.2) |
   | `RESEND_API_KEY`, `EMAIL_REDIRECT_TO` | only if you want real emails (also set `EMAIL_PROVIDER` to `resend`) |

   `SESSION_SECRET` is generated automatically. `LLM_MODE` is already `auto`.
4. Wait for the first build (about 3–5 minutes). Render shows the URL, e.g. `https://vendor-onboarding.onrender.com`.

### 5.2 Finish configuration
1. **Environment →** set `PUBLIC_BASE_URL` to your Render URL (it's used in the links inside vendor emails) → **Save**. Render redeploys.
2. Open the URL, sign in with `DEMO_PASSWORD`, and run sample **S0**. It should finish as **Approved** in a few seconds.
3. Open `https://<your-url>/healthz`. It should show `{"status":"ok"}`.

### 5.3 What the free plan means
- **It sleeps** after about 15 minutes without traffic. The first request after that takes 30–60 seconds.
- **No persistent disk:** every restart or redeploy starts from fresh demo data. That's intentional for a demo: the app re-seeds itself, and the USA becomes a "first-seen" country again.
- For persistence, choose a paid instance, add a disk mounted at `/app/data`, and keep `DATA_DIR=/app/data`.

### Alternatives
- **Railway:** New Project → Deploy from GitHub repo. It detects the `Dockerfile`. Add the same variables as in 5.1, plus `PORT` if Railway doesn't set it. Optionally add a volume at `/app/data`.
- **Any Docker host** (requires Docker Desktop running locally):

  ```powershell
  docker build -t vendor-onboarding .
  ```

  ```powershell
  docker run -p 8000:8000 -e DEMO_PASSWORD=change-me -e GEMINI_API_KEY=your-key vendor-onboarding
  ```

---

## 6. Before every demo or recording

1. **Wake the app:** open the URL 2–3 minutes before you start (free instances sleep).
2. **Settings → Reset demo data** for a clean slate.
3. **Settings:**
   - LLM mode **Auto**, LLM enabled **on**.
   - **Evaluator view on**, so the sample list doesn't reveal expected outcomes.
   - For the video, tax-rule search provider = **Curated official sources only** (keeps the run view clean).
4. Run order matters: **S1 before S7a** (S7a fixes S1's vendor) and **S4a before S4b/S4c** (S4a learns the US rule).
5. Scripts:
   - [`docs/VIDEO_SCRIPT.md`](docs/VIDEO_SCRIPT.md): five-minute recorded video
   - [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md): live demo, with an "If asked" list of every other scenario

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `py` is not recognised | Python not on PATH | Reinstall Python 3.12 with "Add to PATH", or use the full path to `python.exe` |
| A new upload goes to manual review with `LLM_UNAVAILABLE` | No Gemini key, kill switch off, daily cap reached, or Gemini down | Set `GEMINI_API_KEY`, set LLM mode to Auto/Live, check **Settings → LLM enabled**. Failing closed to a person is the intended behaviour. |
| Tax-rule stage says "Live web search unavailable … 429" | The Gemini key has no search quota | Harmless: the app used curated official sources. Enable billing on the key, or set the provider to "Curated official sources only". |
| Emails show status `failed` | Wrong Resend key, or sending to an address Resend won't accept | Check `RESEND_API_KEY`; set `EMAIL_REDIRECT_TO` to your Resend account email |
| Login keeps failing on Render | Password mismatch, or `SESSION_SECRET` changed | Re-enter `DEMO_PASSWORD` in Render → Environment; clear cookies |
| Data disappeared on Render | Free instance restarted (no persistent disk) | Expected; demo data re-seeds automatically |
| `docker` says it cannot connect | Docker Desktop is not running | Start Docker Desktop and wait until it says "Engine running" |
| Port 8000 already in use | Another server is running | Stop it, or start with `--port 8001` |
