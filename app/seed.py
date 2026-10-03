"""Idempotent seed data. Every name, ID and account here is fictional.

Each table is only filled when it is empty, so running seed() twice changes nothing.
"""
from __future__ import annotations

from app import audit, clock, db
from app.domain.banking import normalize_account
from app.domain.identifiers import normalize_identifier
from app.domain.names import normalize_name
from app.domain.taxids import make_gstin

# (legal_name, country, tax_id, bank_account, bank_code, status, email_domain, phone)
VENDORS: list[tuple[str, str, str, str, str, str, str, str]] = [
    ("Northwind Logistics Private Limited", "IN", make_gstin("27", "AABCN4521K"), "50200012345678", "HDFC0001234", "active", "northwind-logistics.example", "+91 22 4000 1101"),
    ("Saffron Textiles Private Limited", "IN", make_gstin("24", "AAKCS7310M"), "002301556677", "ICIC0000023", "active", "saffrontextiles.example", "+91 79 4000 1102"),
    ("Bluepeak Software Solutions LLP", "IN", make_gstin("29", "AAQFB2209D"), "39876543210", "SBIN0004567", "active", "bluepeak.example", "+91 80 4000 1103"),
    ("Kaveri Agro Industries Limited", "IN", make_gstin("33", "AADCK8812P"), "6012345678901", "UTIB0000601", "active", "kaveriagro.example", "+91 44 4000 1104"),
    ("Meridian Office Supplies Private Limited", "IN", make_gstin("07", "AAGCM5567Q"), "915020033344455", "UTIB0000123", "active", "meridian-office.example", "+91 11 4000 1105"),
    ("Coastal Packaging Private Limited", "IN", make_gstin("32", "AAHCC3390L"), "10023456789", "SBIN0070011", "active", "coastalpack.example", "+91 484 400 1106"),
    ("Harbor & Main Consulting GmbH", "DE", "DE812345678", "DE44500105175407324931", "INGDDEFFXXX", "active", "harbormain.example", "+49 30 4000 1107"),
    ("Lindqvist Instruments GmbH", "DE", "DE298765431", "DE89370400440532013000", "COBADEFFXXX", "active", "lindqvist-instruments.example", "+49 89 4000 1108"),
    ("Thamesbridge Facilities Ltd", "GB", "GB123456789", "GB33BUKB20201555555555", "BUKBGB22", "active", "thamesbridge.example", "+44 20 4000 1109"),
    ("Pennine Print Works Ltd", "GB", "GB987654312", "GB94BARC10201530093459", "BARCGB22", "active", "pennineprint.example", "+44 113 400 1110"),
    ("Orchid Lane Events Pte Ltd", "SG", "201912345K", "0123456789", "DBSSSGSG", "active", "orchidlane.example", "+65 6000 1111"),
    ("Granite Ridge Hardware Private Limited", "IN", make_gstin("27", "AAJCG6104R"), "123456789012", "KKBK0000958", "blocked", "graniteridge.example", "+91 22 4000 1112"),
]

# Clearly fictional "sanctioned" entities, labelled MOCK-LIST-A everywhere in the UI.
SANCTIONS: list[tuple[str, list[str], str, str]] = [
    ("Volkhov Meridian Shipping Ltd", ["Volkhov Meridian Maritime"], "XX", "company"),
    ("Crimson Delta Trading FZE", ["Crimson Delta General Trading"], "XX", "company"),
    ("Obsidian Crest Holdings SA", [], "XX", "company"),
    ("Zarkov Ferrous Exports LLC", ["Zarkov Metals"], "XX", "company"),
    ("Pale Harbor Logistics Limited", [], "XX", "company"),
    ("Silverline Global Commodities Trading", ["Silverline Global Commodities"], "XX", "company"),
    ("Ivo Draganov Petrenko", ["I. D. Petrenko"], "XX", "individual"),
    ("Mirelle Okonkwo-Vance", [], "XX", "individual"),
]

# (label, severity, reason, [(kind, display_value)])
WATCHLIST: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    ("Granite Ridge Hardware (invoice fraud 2025)", "blocked", "Confirmed invoice fraud; payments recalled.",
     [("name", "Granite Ridge Hardware Private Limited"), ("bank_account", "123456789012"), ("tax_id", make_gstin("27", "AAJCG6104R")),
      ("email_domain", "graniteridge.example"), ("director", "Vikram Sethuraman")]),
    ("Quickfix Industrial Supplies (shell entity)", "blocked", "Shell company used to divert payments; bank account reused across names.",
     [("name", "Quickfix Industrial Supplies Private Limited"), ("bank_account", "918020045671234"), ("phone", "+91 98200 45671"),
      ("address", "Unit 4, Plot 77, Turbhe MIDC, Navi Mumbai"), ("director", "Rohan Malviya")]),
    ("Sunmark Exports (fake GST certificate)", "high", "Submitted a forged tax certificate in 2025; case closed without onboarding.",
     [("name", "Sunmark Exports Private Limited"), ("email_domain", "sunmark-exports.example"), ("tax_id", make_gstin("24", "AAXCS1197H"))]),
    ("Apex Prime Distributors (duplicate invoicing)", "high", "Duplicate invoicing under two vendor IDs.",
     [("name", "Apex Prime Distributors"), ("bank_account", "50100987654321"), ("phone", "+91 99000 12345")]),
    ("Lotus Bay Traders (late delivery disputes)", "watch", "Repeated delivery disputes; monitor only.",
     [("name", "Lotus Bay Traders"), ("email_domain", "lotusbay.example")]),
    ("Redwood Courier Services (address mismatch)", "watch", "Registered address could not be verified in 2025.",
     [("name", "Redwood Courier Services Private Limited"), ("address", "12 Old Mill Road, Pune")]),
]

# (country, tax_type, description, regex, checksum_algo, example, source_url, status, notes)
TAX_RULES: list[tuple[str, str, str, str, str | None, str, str, str, str | None]] = [
    ("IN", "GSTIN", "India GST identification number: 2-digit state code, 10-character PAN, entity number, 'Z', check character (mod-36).",
     r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", "gstin_mod36", "27AAPFU0939F1ZV",
     "https://tutorial.gst.gov.in/", "seed", None),
    ("IN", "PAN", "India Permanent Account Number: 5 letters (4th = holder type), 4 digits, 1 letter.",
     r"^[A-Z]{5}[0-9]{4}[A-Z]$", None, "AAPFU0939F", "https://www.incometax.gov.in/", "seed", None),
    ("DE", "VAT", "Germany VAT ID (USt-IdNr.): 'DE' followed by 9 digits.",
     r"^DE[0-9]{9}$", None, "DE812345678", "https://ec.europa.eu/taxation_customs/vies/", "seed", None),
    ("GB", "VAT", "UK VAT registration number: 'GB' + 9 or 12 digits, or GD/HA + 3 digits.",
     r"^GB([0-9]{9}|[0-9]{12}|GD[0-9]{3}|HA[0-9]{3})$", None, "GB123456789", "https://www.gov.uk/guidance/vat-registration-numbers", "seed", None),
    # Backup first-seen country, pre-warmed so the demo has a cached rule if live search is slow.
    ("SG", "UEN", "Singapore Unique Entity Number: 8 digits + letter (business), 9 digits + letter (local company), or T/S/R + yy + 2-letter type + 4 digits + letter.",
     r"^([0-9]{8}[A-Z]|[0-9]{9}[A-Z]|[TSR][0-9]{2}[A-Z]{2}[0-9]{4}[A-Z])$", None, "201912345K",
     "https://www.uen.gov.sg/", "provisional", "Pre-warmed backup rule (not learned live)."),
]


def _empty(conn, table: str) -> bool:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def seed() -> dict[str, int]:
    """Fill empty reference tables. Returns how many rows were inserted per table."""
    now = clock.now_iso()
    inserted: dict[str, int] = {}
    with db.connect() as conn:
        if _empty(conn, "vendors"):
            for name, country, tax_id, account, code, status, domain, phone in VENDORS:
                conn.execute(
                    "INSERT INTO vendors(legal_name, normalized_name, country, tax_id, bank_account, bank_code, status, email_domain, contact_phone, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (name, normalize_name(name), country, tax_id, normalize_account(account), code, status, domain, phone, now),
                )
            inserted["vendors"] = len(VENDORS)

        if _empty(conn, "sanctions_list"):
            for name, aliases, country, etype in SANCTIONS:
                conn.execute(
                    "INSERT INTO sanctions_list(name, aliases_json, country, entity_type, source_label) VALUES (?,?,?,?,?)",
                    (name, db.dumps(aliases), country, etype, "MOCK-LIST-A"),
                )
            inserted["sanctions_list"] = len(SANCTIONS)

        if _empty(conn, "watchlist"):
            for label, severity, reason, identifiers in WATCHLIST:
                cur = conn.execute(
                    "INSERT INTO watchlist(label, severity, reason, added_by, added_at, active) VALUES (?,?,?,?,?,1)",
                    (label, severity, reason, "seed", now),
                )
                for kind, display in identifiers:
                    conn.execute(
                        "INSERT INTO watchlist_identifiers(watchlist_id, kind, value_normalized, value_display) VALUES (?,?,?,?)",
                        (cur.lastrowid, kind, normalize_identifier(kind, display), display),
                    )
            inserted["watchlist"] = len(WATCHLIST)

        if _empty(conn, "tax_rules"):
            for country, ttype, desc, rx, algo, example, url, status, notes in TAX_RULES:
                conn.execute(
                    "INSERT INTO tax_rules(country, tax_type, description, regex, checksum_algo, example, source_url, status,"
                    " last_verified_at, created_at, created_by, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (country, ttype, desc, rx, algo, example, url, status, now, now, "seed", notes),
                )
            inserted["tax_rules"] = len(TAX_RULES)

    if inserted:
        audit.log("system", "seed", "database", None, inserted)
    return inserted

