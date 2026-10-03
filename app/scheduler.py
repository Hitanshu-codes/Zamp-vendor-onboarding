"""Time-based follow-ups for vendor requests: reminders on day 3 and day 7, auto-close on AUTO_CLOSE_DAYS.

Driven by the (simulated) clock: tick() runs whenever the clock is advanced, on boot, and when the
Awaiting vendor page is opened, so the demo can show reminders and auto-close in seconds.
"""
from __future__ import annotations

from datetime import datetime

from app import audit, clock, db
from app.comms import outbox
from app.config import config

REMINDER_DAYS = (3, 7)


class _Ctx:
    def __init__(self, sub, form):
        self.submission_id = sub["id"]
        self.run_id = None
        self.form = form


async def tick() -> dict[str, int]:
    now = clock.now()
    counts = {"reminders": 0, "closed": 0}
    subs = db.query("SELECT * FROM submissions WHERE status = 'pending' AND route = 'vendor_followup' AND requested_at IS NOT NULL")
    for sub in subs:
        days = (now - datetime.fromisoformat(sub["requested_at"])).days
        version = db.query_one("SELECT form_json FROM submission_versions WHERE submission_id = ? AND version_no = ?",
                               (sub["id"], sub["current_version"]))
        form = db.loads(version["form_json"], {})
        items = [i["text"] for i in db.loads(sub["requested_items_json"], [])]
        if days >= config.auto_close_days:
            db.execute("UPDATE submissions SET status = 'rejected', route = 'none', closed_reason = 'auto_closed_no_response', updated_at = ? WHERE id = ?",
                       (clock.now_iso(), sub["id"]))
            await outbox.send_vendor_email(_Ctx(sub, form), "auto_close", "vendor_auto_close.txt", [])
            audit.log("system", "auto_closed", "submission", sub["id"], {"days_since_request": days})
            counts["closed"] += 1
            continue
        due_reminders = sum(1 for d in REMINDER_DAYS if days >= d)
        if due_reminders > sub["reminders_sent"]:
            db.execute("UPDATE submissions SET reminders_sent = ? WHERE id = ?", (due_reminders, sub["id"]))
            await outbox.send_vendor_email(_Ctx(sub, form), "reminder", "vendor_reminder.txt", [], items=items, due_date=sub["due_at"],
                                           resubmit_url=outbox.resubmit_url(sub["resubmit_token"]))
            audit.log("system", "reminder_sent", "submission", sub["id"], {"reminder": due_reminders, "days_since_request": days})
            counts["reminders"] += 1
    return counts
