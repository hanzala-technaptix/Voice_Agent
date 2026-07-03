"""
Lead Collector — Phase 2
========================
FastAPI service that ingests leads from any source (form, CSV, Apollo, n8n)
and writes them into the Voice-Agent Google Sheet CRM after validation +
duplicate check.

Endpoints
---------
POST /leads          single lead          {name, phone, ...}
POST /leads/bulk     up to 500 at once    {leads: [{...}, ...]}
GET  /leads/stats    counts by status
GET  /health         dependency check

CRM columns (the sheet must have a header row in this exact order):
    Lead ID | name | email | company | role | Phone | Call Status |
    notes | Call Attempt | Next Call At | Last Updated | Call Outcome | Transcript

Run:
    uvicorn lead_collector:app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

# Google Sheets API — service-account auth (no OAuth dance, no token refresh).
# Why a service account: this runs headless on a server, no user flow.
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ----------------------------------------------------------------- config ---

SHEET_ID = os.getenv("LEAD_SHEET_ID", "")
SHEET_TAB = os.getenv("LEAD_SHEET_TAB", "Lead")  # the tab name inside the spreadsheet
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json")
DEFAULT_SOURCE = os.getenv("DEFAULT_LEAD_SOURCE", "manual")

# the column order MUST match the sheet header row
COLUMNS = [
    "Lead ID", "name", "email", "company", "role", "Phone", "Call Status",
    "notes", "Call Attempt", "Next Call At", "Last Updated", "Call Outcome", "Transcript",
]
COL_INDEX = {c: i for i, c in enumerate(COLUMNS)}

# default values for fresh leads
NEW_STATUS = "New Lead"

# ----------------------------------------------------------------- logging --

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("lead-collector")

# also append every operation as JSON to leads.log for audit
_audit_path = os.getenv("LEAD_AUDIT_LOG", "leads.log")


def audit(event: str, **kwargs) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    try:
        with open(_audit_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("audit write failed")


# ----------------------------------------------------------------- models --

# E.164: + then 1-9, then 6-14 more digits. Same regex as dispatch.py for symmetry.
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
# Loose email check — strict RFC validation is famously a rabbit hole; this catches typos.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Names: at least one letter, max 100 chars, no control chars. Allow unicode letters.
_NAME = re.compile(r"^[^\x00-\x1f]{1,100}$")


def normalize_phone(raw: str, default_country_code: str = "+1") -> str:
    """Best-effort normalize to E.164. Accepts: '(415) 555-1234', '415-555-1234',
    '4155551234', '+14155551234'. If already starts with '+', only strip non-digits
    after it. If 10 digits, assume default country code."""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw[1:])
        return f"+{digits}"
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:                      # US/CA without country code
        return f"{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if digits else ""


class Lead(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone: str
    company: str = ""
    email: str = ""
    role: str = ""
    source: str = DEFAULT_SOURCE
    notes: str = ""

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = v.strip()
        if not _NAME.match(v):
            raise ValueError("name contains invalid characters")
        if not re.search(r"[A-Za-z\u00C0-\u024F\u0600-\u06FF]", v):
            # Must contain at least one letter (Latin, Latin-ext, or Arabic-script
            # to support Pakistani names like اسلام آباد transliterations).
            raise ValueError("name must contain at least one letter")
        return v

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str) -> str:
        v = normalize_phone(v)
        if not _E164.match(v):
            raise ValueError(f"phone must be E.164 (got {v!r})")
        return v

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        v = v.strip().lower()
        if v and not _EMAIL.match(v):
            raise ValueError("email looks malformed")
        return v


class BulkLeads(BaseModel):
    leads: list[Lead] = Field(..., min_length=1, max_length=500)


# ----------------------------------------------------------------- sheets --


class SheetsClient:
    """Thin wrapper around Google Sheets API v4. One client per process; the
    underlying httplib2 is not async but each request is short enough that we
    just run it in the default threadpool via FastAPI's sync dispatch path."""

    def __init__(self, sheet_id: str, tab: str):
        self.sheet_id = sheet_id
        self.tab = tab
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if not os.path.exists(GOOGLE_CREDS_PATH):
            raise RuntimeError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON not found at {GOOGLE_CREDS_PATH!r}. "
                "Create a Google Cloud service account, download its JSON key, "
                "share the sheet with the service-account email, and point "
                "GOOGLE_SERVICE_ACCOUNT_JSON at the file."
            )
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # ---- reads

    def fetch_all_rows(self) -> list[list[str]]:
        """Return the full data range (excluding the header row)."""
        rng = f"{self.tab}!A2:{chr(ord('A') + len(COLUMNS) - 1)}"
        try:
            resp = self.svc.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range=rng
            ).execute()
        except HttpError as e:
            raise RuntimeError(f"Sheets read failed: {e}") from e
        return resp.get("values", [])

    def existing_phones(self) -> set[str]:
        rows = self.fetch_all_rows()
        phones: set[str] = set()
        p_idx = COL_INDEX["Phone"]
        for row in rows:
            if len(row) > p_idx and row[p_idx].strip():
                phones.add(row[p_idx].strip())
        return phones

    def status_counts(self) -> dict[str, int]:
        rows = self.fetch_all_rows()
        s_idx = COL_INDEX["Call Status"]
        counts: dict[str, int] = {}
        for row in rows:
            status = row[s_idx] if len(row) > s_idx else ""
            counts[status or "(blank)"] = counts.get(status or "(blank)", 0) + 1
        return counts

    # ---- writes

    def append_rows(self, rows: list[list[str]]) -> int:
        """Append rows; returns number written. Uses USER_ENTERED so dates parse."""
        if not rows:
            return 0
        try:
            self.svc.spreadsheets().values().append(
                spreadsheetId=self.sheet_id,
                range=f"{self.tab}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
        except HttpError as e:
            if e.resp.status == 403:
                email = ""
                try:
                    import json as _json
                    with open(GOOGLE_CREDS_PATH, encoding="utf-8") as cf:
                        email = _json.load(cf).get("client_email", "")
                except Exception:  # noqa: BLE001
                    pass
                hint = (
                    f"Share the Google Sheet with the service account as Editor"
                    + (f" ({email})" if email else "")
                )
                raise RuntimeError(f"Sheets append failed: permission denied. {hint}") from e
            raise RuntimeError(f"Sheets append failed: {e}") from e
        return len(rows)

    def ensure_header(self) -> None:
        """If row 1 is empty, write the header. Idempotent for an existing header."""
        try:
            resp = self.svc.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"{self.tab}!A1:{chr(ord('A') + len(COLUMNS) - 1)}1",
            ).execute()
        except HttpError as e:
            raise RuntimeError(f"Sheets header check failed: {e}") from e
        row = (resp.get("values") or [[]])[0]
        if row == COLUMNS:
            return
        if any(c.strip() for c in row):
            logger.warning(
                "Sheet header doesn't match expected columns. "
                "Expected: %s. Got: %s. Leaving as-is to avoid clobbering data.",
                COLUMNS, row,
            )
            return
        end_col = chr(ord("A") + len(COLUMNS) - 1)
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"{self.tab}!A1:{end_col}1",
            valueInputOption="USER_ENTERED",
            body={"values": [COLUMNS]},
        ).execute()
        logger.info("Wrote header row to %s!A1:%s1", self.tab, end_col)


# ----------------------------------------------------------------- app -----

app = FastAPI(title="Technaptix Lead Collector")
_sheets: Optional[SheetsClient] = None


def sheets() -> SheetsClient:
    global _sheets
    if _sheets is None:
        if not SHEET_ID:
            raise HTTPException(500, "LEAD_SHEET_ID not configured")
        _sheets = SheetsClient(SHEET_ID, SHEET_TAB)
        _sheets.ensure_header()
    return _sheets


def _row_for(lead: Lead) -> list[str]:
    now_iso = datetime.now(timezone.utc).isoformat()
    return [
        f"LD-{uuid.uuid4().hex[:8].upper()}",  # Lead ID
        lead.name,                              # name
        lead.email,                             # email
        lead.company,                           # company
        lead.role,                              # role
        lead.phone,                             # Phone
        NEW_STATUS,                             # Call Status
        lead.notes,                             # notes
        "0",                                    # Call Attempt
        "",                                     # Next Call At
        now_iso,                                # Last Updated
        "",                                     # Call Outcome
        "",                                     # Transcript
    ]


@app.post("/leads")
async def create_lead(lead: Lead):
    """Single-lead ingest. Fails on duplicate phone."""
    sc = sheets()
    existing = sc.existing_phones()
    if lead.phone in existing:
        audit("duplicate_skip", phone=lead.phone, name=lead.name)
        raise HTTPException(409, f"phone {lead.phone} already in CRM")
    row = _row_for(lead)
    sc.append_rows([row])
    audit("lead_added", lead_id=row[0], phone=lead.phone, source=lead.source)
    return {"status": "ok", "lead_id": row[0]}


@app.post("/leads/bulk")
async def create_leads_bulk(payload: BulkLeads):
    """Bulk ingest. Skips duplicates and invalid rows; reports per-row status."""
    sc = sheets()
    existing = sc.existing_phones()
    accepted: list[list[str]] = []
    skipped: list[dict] = []
    seen_in_batch: set[str] = set()
    for lead in payload.leads:
        if lead.phone in existing or lead.phone in seen_in_batch:
            skipped.append({"phone": lead.phone, "reason": "duplicate"})
            continue
        seen_in_batch.add(lead.phone)
        accepted.append(_row_for(lead))
    if accepted:
        try:
            sc.append_rows(accepted)
        except RuntimeError as e:
            audit("bulk_failure", error=str(e), attempted=len(accepted))
            raise HTTPException(502, str(e))
    audit("bulk_ingest", accepted=len(accepted), skipped=len(skipped))
    return {
        "status": "ok",
        "accepted": len(accepted),
        "skipped": len(skipped),
        "skipped_detail": skipped,
        "lead_ids": [r[0] for r in accepted],
    }


@app.get("/leads/stats")
async def stats():
    sc = sheets()
    counts = sc.status_counts()
    return {"total": sum(counts.values()), "by_status": counts}


@app.get("/health")
async def health():
    out = {"ok": True, "sheet_id_set": bool(SHEET_ID), "creds_present": os.path.exists(GOOGLE_CREDS_PATH)}
    try:
        sc = sheets()
        sc.fetch_all_rows()
        out["sheet_reachable"] = True
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["sheet_reachable"] = False
        out["error"] = str(e)
    return out


# ----------------------------------------------------------------- CLI -----

def _cli_import_csv(path: str) -> None:
    """Convenience: `python lead_collector.py import-csv leads.csv`. Skips
    duplicates, prints a summary. CSV headers (any subset): name, phone, company,
    email, role, source, notes."""
    if not os.path.exists(path):
        alt = f"{path}v" if not path.lower().endswith(".csv") else None
        msg = f"CSV file not found: {path!r}"
        if alt and os.path.exists(alt):
            msg += f" — did you mean {alt!r}?"
        elif not path.lower().endswith(".csv"):
            msg += " — use the .csv extension (e.g. leads.csv or sample_leads.csv)"
        raise SystemExit(msg)
    sc = SheetsClient(SHEET_ID, SHEET_TAB)
    sc.ensure_header()
    existing = sc.existing_phones()
    accepted: list[list[str]] = []
    skipped = 0
    errors: list[str] = []
    seen_in_batch: set[str] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, start=2):  # row 1 = header
            try:
                lead = Lead(
                    name=raw.get("name", "").strip(),
                    phone=raw.get("phone", "").strip(),
                    company=raw.get("company", "").strip(),
                    email=raw.get("email", "").strip(),
                    role=raw.get("role", "").strip(),
                    source=raw.get("source", DEFAULT_SOURCE).strip() or DEFAULT_SOURCE,
                    notes=raw.get("notes", "").strip(),
                )
            except Exception as e:  # noqa: BLE001 — surface invalid CSV rows
                errors.append(f"row {i}: {e}")
                continue
            if lead.phone in existing or lead.phone in seen_in_batch:
                skipped += 1
                continue
            seen_in_batch.add(lead.phone)
            accepted.append(_row_for(lead))
    if accepted:
        sc.append_rows(accepted)
    print(f"accepted={len(accepted)} skipped_duplicates={skipped} invalid={len(errors)}")
    for e in errors[:20]:
        print(f"  - {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "import-csv":
        _cli_import_csv(sys.argv[2])
    else:
        print("Usage:")
        print("  uvicorn lead_collector:app --host 0.0.0.0 --port 8100")
        print("  python lead_collector.py import-csv leads.csv")
