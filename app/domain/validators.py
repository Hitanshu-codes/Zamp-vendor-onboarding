"""Smaller deterministic checks: document dates and contact email."""
from __future__ import annotations

from datetime import date

from app.models import CheckOutcome, failed, passed, skipped, warned

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "mail.com",
    "yandex.com", "yandex.ru", "rediffmail.com", "qq.com", "163.com", "zoho.com", "web.de",
}


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def expiry_outcome(doc_label: str, expiry: str | None, today: date) -> CheckOutcome:
    d = parse_date(expiry)
    if d is None:
        return passed(f"The {doc_label} shows no expiry date.", expiry_date=expiry)
    if d < today:
        return failed("DOC_EXPIRED", f"The {doc_label} expired on {d.isoformat()}.", expiry_date=d.isoformat(), today=today.isoformat())
    return passed(f"The {doc_label} is valid until {d.isoformat()}.", expiry_date=d.isoformat())


def bank_letter_age_outcome(issue: str | None, today: date, max_age_days: int) -> CheckOutcome:
    d = parse_date(issue)
    if d is None:
        return passed("The bank document has no issue date (e.g. a cancelled cheque); age not checked.", issue_date=issue)
    age = (today - d).days
    if age > max_age_days:
        return failed("DOC_STALE", f"The bank letter is dated {d.isoformat()}, {age} days ago; we need one from the last {max_age_days} days.",
                      issue_date=d.isoformat(), age_days=age, max_age_days=max_age_days)
    if age < -1:
        return failed("DATES_IMPOSSIBLE", f"The bank letter is dated in the future ({d.isoformat()}).", issue_date=d.isoformat())
    return passed(f"The bank letter is {age} days old (limit {max_age_days}).", issue_date=d.isoformat(), age_days=age)


def date_order_outcome(registration: str | None, incorporation: str | None) -> CheckOutcome:
    r, i = parse_date(registration), parse_date(incorporation)
    if r is None or i is None:
        return skipped("Tax registration or incorporation date not available; order not checked.",
                       registration_date=registration, incorporation_date=incorporation)
    if r < i:
        return failed("DATES_IMPOSSIBLE", f"Tax registration ({r.isoformat()}) is dated before the company was incorporated ({i.isoformat()}).",
                      registration_date=r.isoformat(), incorporation_date=i.isoformat())
    return passed(f"Incorporated {i.isoformat()}, tax-registered {r.isoformat()}: order is consistent.",
                  registration_date=r.isoformat(), incorporation_date=i.isoformat())


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


def email_outcome(email: str, entity_type: str) -> CheckOutcome:
    domain = email_domain(email)
    if entity_type != "sole_proprietor" and domain in FREE_EMAIL_DOMAINS:
        return warned(f"Contact email uses a free provider ({domain}) for a company vendor. Not blocking; adds risk points.",
                      code="FREE_EMAIL_DOMAIN", domain=domain)
    return passed(f"Contact email domain '{domain}' is acceptable.", domain=domain)
