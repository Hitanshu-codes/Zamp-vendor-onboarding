# Vendor Onboarding Automation

A vendor submits a form and three PDFs. The system returns **approved / pending / rejected**, shows every check and its reasoning, and either asks the vendor for a specific fix or sends the case to a human reviewer.

> AI reads the documents, plain rules make the decision, people handle the risky cases.

## Run locally (no API key needed)

```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000. The database is created and seeded on first boot. Copy `.env.example` to `.env` to configure it.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

Deployment, LLM modes and the demo script are documented as the build progresses (see `docs/`).
