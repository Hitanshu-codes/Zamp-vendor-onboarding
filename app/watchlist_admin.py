"""Watchlist administration outside review cases: manual entries, CSV import, (de)activation. All audited."""
from __future__ import annotations

import csv
import io

from app import audit, clock, db
from app.domain.identifiers import normalize_identifier

KINDS = ("name", "bank_account", "tax_id", "email_domain", "phone", "address", "director")
SEVERITIES = ("blocked", "high", "watch")
MAX_CSV_ROWS = 500


class WatchlistError(ValueError):
    pass


def add_entry(label: str, severity: str, reason: str, identifiers: list[tuple[str, str]], expires_at: str | None, actor: str,
              source_submission_id: int | None = None) -> int:
    label, reason = " ".join(label.split())[:120], " ".join(reason.split())[:500]
    if not label or len(reason) < 5:
        raise WatchlistError("A label and a reason are required.")
    if severity not in SEVERITIES:
        raise WatchlistError("Severity must be blocked, high or watch.")
    idents = [(k, v.strip()) for k, v in identifiers if k in KINDS and v and v.strip()]
    if not idents:
        raise WatchlistError("Add at least one identifier (kind and value).")
    wid = db.execute("INSERT INTO watchlist(label, severity, reason, source_submission_id, added_by, added_at, expires_at, active)"
                     " VALUES (?,?,?,?,?,?,?,1)", (label, severity, reason, source_submission_id, actor, clock.now_iso(), expires_at or None))
    for kind, value in idents:
        db.execute("INSERT INTO watchlist_identifiers(watchlist_id, kind, value_normalized, value_display) VALUES (?,?,?,?)",
                   (wid, kind, normalize_identifier(kind, value), value[:200]))
    audit.log(actor, "watchlist_added", "watchlist", wid, {"severity": severity, "identifiers": [k for k, _ in idents], "reason": reason})
    return wid


def set_active(entry_id: int, active: bool, reason: str, actor: str) -> None:
    if len(" ".join(reason.split())) < 5:
        raise WatchlistError("Please type a reason.")
    if db.query_one("SELECT 1 FROM watchlist WHERE id = ?", (entry_id,)) is None:
        raise WatchlistError("Entry not found.")
    db.execute("UPDATE watchlist SET active = ? WHERE id = ?", (1 if active else 0, entry_id))
    audit.log(actor, "watchlist_activated" if active else "watchlist_deactivated", "watchlist", entry_id, {"reason": reason})


def import_csv(text: str, actor: str) -> tuple[int, list[str]]:
    """Columns: label, severity, reason, kind, value[, expires_at]. Rows with the same label form one entry."""
    reader = csv.DictReader(io.StringIO(text))
    required = {"label", "severity", "reason", "kind", "value"}
    if not reader.fieldnames or not required <= {f.strip().lower() for f in reader.fieldnames}:
        raise WatchlistError("CSV needs the columns: label, severity, reason, kind, value (expires_at optional).")
    groups: dict[str, dict] = {}
    errors: list[str] = []
    for i, raw in enumerate(reader, start=2):
        if i > MAX_CSV_ROWS + 1:
            errors.append(f"Stopped after {MAX_CSV_ROWS} rows.")
            break
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if row["kind"] not in KINDS:
            errors.append(f"Row {i}: unknown kind '{row['kind'][:30]}'.")
            continue
        g = groups.setdefault(row["label"], {"severity": row["severity"], "reason": row["reason"], "expires": row.get("expires_at") or None, "idents": []})
        g["idents"].append((row["kind"], row["value"]))
    created = 0
    for label, g in groups.items():
        try:
            add_entry(label, g["severity"], g["reason"], g["idents"], g["expires"], actor)
            created += 1
        except WatchlistError as exc:
            errors.append(f"'{label[:40]}': {exc}")
    audit.log(actor, "watchlist_csv_import", "watchlist", None, {"entries_created": created, "errors": len(errors)})
    return created, errors
