"""Bank identifier validators: IBAN, IFSC, US ABA routing, SWIFT/BIC."""
from __future__ import annotations

import regex

from app.models import CheckOutcome, failed, passed

# IBAN length by country (ISO 13616 registry, common countries).
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AT": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29, "CH": 21, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IL": 23,
    "IS": 26, "IT": 27, "KW": 30, "KZ": 20, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31, "NL": 18,
    "NO": 15, "PK": 24, "PL": 28, "PT": 25, "QA": 29, "RO": 24, "SA": 24, "SE": 24, "SI": 19, "SK": 24, "TR": 26,
    "UA": 29,
}
IFSC_RE = regex.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
SWIFT_RE = regex.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
ABA_RE = regex.compile(r"^[0-9]{9}$")


def normalize_account(value: str) -> str:
    """Uppercase, strip spaces and dashes. Used for collision and watchlist matching."""
    return regex.sub(r"[\s\-]", "", (value or "").upper())


def looks_like_iban(value: str) -> bool:
    v = normalize_account(value)
    return bool(regex.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{8,30}$", v))


def iban_valid(value: str) -> tuple[bool, str]:
    v = normalize_account(value)
    country = v[:2]
    expected = IBAN_LENGTHS.get(country)
    if expected is None:
        return False, f"unknown IBAN country '{country}'"
    if len(v) != expected:
        return False, f"{country} IBANs have {expected} characters, this one has {len(v)}"
    rearranged = v[4:] + v[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    if int(digits) % 97 != 1:
        return False, "IBAN check digits are wrong (mod-97)"
    return True, "valid"


def aba_valid(routing: str) -> bool:
    r = normalize_account(routing)
    if not ABA_RE.match(r):
        return False
    d = [int(c) for c in r]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def ifsc_valid(code: str) -> bool:
    return bool(IFSC_RE.match(normalize_account(code)))


def swift_valid(code: str) -> bool:
    return bool(SWIFT_RE.match(normalize_account(code)))


def bank_country(account: str, bank_code: str) -> str | None:
    """Country implied by the bank identifiers, if any."""
    acc, code = normalize_account(account), normalize_account(bank_code)
    if looks_like_iban(acc):
        return acc[:2]
    if ifsc_valid(code):
        return "IN"
    if SWIFT_RE.match(code):
        return code[4:6]
    if ABA_RE.match(code):
        return "US"
    return None


def validate_bank(country: str, account: str, bank_code: str) -> CheckOutcome:
    """Format and checksum of the account number and bank code."""
    acc, code = normalize_account(account), normalize_account(bank_code)
    ev = {"account": acc, "bank_code": code, "vendor_country": country}
    problems: list[str] = []
    kinds: list[str] = []

    if looks_like_iban(acc):
        ok, why = iban_valid(acc)
        kinds.append("IBAN")
        if not ok:
            problems.append(why)
    elif country == "IN" or ifsc_valid(code):
        kinds.append("Indian account + IFSC")
        if not ifsc_valid(code):
            problems.append(f"IFSC '{code}' should be 4 letters, a zero, then 6 letters or digits")
        if not regex.match(r"^[0-9]{9,18}$", acc):
            problems.append("Indian account numbers have 9 to 18 digits")
    elif country == "US" or ABA_RE.match(code):
        kinds.append("US account + ABA routing")
        if not aba_valid(code):
            problems.append(f"routing number '{code}' fails the ABA checksum")
        if not regex.match(r"^[0-9]{4,17}$", acc):
            problems.append("US account numbers have 4 to 17 digits")
    else:
        kinds.append("account + SWIFT/BIC")
        if not swift_valid(code):
            problems.append(f"SWIFT/BIC '{code}' is not 8 or 11 characters in the standard format")
        if not regex.match(r"^[A-Z0-9]{5,34}$", acc):
            problems.append("account number has unexpected characters or length")

    ev["format"] = kinds[0]
    if problems:
        ev["problems"] = problems
        return failed("BANK_DETAILS_INVALID", f"Bank details look mistyped: {'; '.join(problems)}.", **ev)
    return passed(f"Bank details are well-formed ({kinds[0]}).", **ev)


def bank_country_outcome(vendor_country: str, account: str, bank_code: str, exempt: bool = False) -> CheckOutcome:
    bc = bank_country(account, bank_code)
    ev = {"vendor_country": vendor_country, "bank_country": bc, "category_exempt": exempt}
    if bc is None:
        return passed("Bank country could not be inferred from the identifiers; nothing to compare.", **ev)
    if bc == vendor_country:
        return passed(f"Bank is in the vendor's country ({bc}).", **ev)
    if exempt:
        return passed(f"Bank is in {bc}, vendor is in {vendor_country}; this vendor category is allowed foreign accounts.", **ev)
    return failed("BANK_COUNTRY_MISMATCH", f"The bank account is in {bc} but the vendor is registered in {vendor_country}. "
                  "Cross-border payment details need a person's review.", **ev)
