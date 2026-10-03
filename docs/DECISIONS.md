# Decisions

One line of rationale per decision. Newest at the bottom.

| # | Decision | Rationale |
|---|---|---|
| 1 | **Gemini (google-genai SDK) is the LLM**, not Claude. | Owner's choice. Wherever the brief says "Claude", read "the LLM". The architecture stays the same: the LLM only extracts and classifies, and code decides. |
| 2 | LLM output is constrained with Gemini **structured output** (JSON schema from pydantic), then re-validated with pydantic. | Gives the same guarantee as a forced schema tool, and the extraction calls get no tools at all, so injected text has nothing to trigger. |
| 3 | Tax-rule learning runs in **two calls**: (a) a Gemini + Google Search call that only returns candidate URLs, then (b) our code resolves redirects, keeps allow-listed hosts, fetches the page, and runs the injection scan; then (c) a separate no-tool call extracts the rule from that page text. | Gemini search grounding has no domain allowlist, so the allowlist is enforced in our code. The call that reads the untrusted page has no tools. |
| 4 | **USA (EIN) is the first-seen country.** EIN is therefore *not* a seed rule. Singapore UEN is pre-warmed as the backup, and Chile is the poisoned-search scenario (S5d). | Owner's choice. The US is learned live from irs.gov-type sources. |
| 5 | India (GSTIN + PAN) is the home country for the happy path. | Default from the brief. India has the richest checks (checksum, GSTIN↔PAN, PAN entity code). |
| 6 | **Render free tier, no persistent disk.** SQLite lives on ephemeral disk, and every cold start re-seeds. | Demo only. A side benefit: each fresh boot makes the US "first seen" again. Ping the URL before a demo, because free instances sleep and lose data on spin-down. |
| 7 | Vendor emails can be **sent for real via Resend** (`EMAIL_PROVIDER=resend`). `EMAIL_REDIRECT_TO` routes every vendor email to one inbox, and an `Idempotency-Key` is set to the outbox id. | Owner asked for real delivery. Resend only delivers to the account owner until a domain is verified, and sample vendors use fictional addresses. A send failure is shown on the email and never changes a decision. Internal briefs are never sent. |
| 8 | `LLM_MODE` defaults to **replay** in code (`.env.example` documents live). | Safe default: the app runs with no key and never spends budget by accident. |
| 9 | The audit log is append-only, **enforced by SQLite triggers** as well as by having no update/delete code. | Defence in depth. "Reset demo data" deletes the database file instead. |
| 10 | Plain `sqlite3` + pydantic, no ORM. One short-lived connection per unit of work, WAL mode. | Fewest moving parts. The data volume is tiny. |
| 11 | Severity gains a `soft` level (risk points only) next to info/fixable/escalate/reject. | The catalogue's soft codes (FREE_EMAIL_DOMAIN, etc.) need a severity that adds points without forcing a route. |
| 12 | Without `DEMO_PASSWORD` the login gate is off (local dev). With it, a signed cookie is used. | One-command local run. The deployed instance always sets the password. |
