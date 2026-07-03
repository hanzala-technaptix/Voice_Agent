"""
Prospecting service — the "search companies -> get decision makers -> sheet" flow.

POST /prospect/companies   you already have target domains
POST /prospect/search      ICP filters (industry, revenue, size, country, seniority)
GET  /health

Both endpoints: discover -> enrich (phone/email) -> dedupe -> write callable
contacts into the Google Sheet CRM as 'New Lead'. Flow A then dials them.

Run:
    uvicorn prospecting:app --host 0.0.0.0 --port 8300

Env:
    LUSHA_API_KEY                 required (Premium/Scale plan)
    LEAD_SHEET_ID / creds         same as lead_collector
    PROSPECT_MAX_WRITE            cap rows written per request (default 25)
    PROSPECT_SENIORITIES          default seniority codes for ICP search
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import lead_collector as lc
from tools import lusha_prospect as lp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prospecting")

MAX_WRITE = int(os.getenv("PROSPECT_MAX_WRITE", "25"))

app = FastAPI(title="Technaptix Prospecting")


class CompaniesRequest(BaseModel):
    domains: list[str] = Field(..., min_length=1, max_length=50)
    max_per_company: int = 5
    write: bool = True


class SearchRequest(BaseModel):
    industries: list[str] = []
    revenues: list[str] = []
    sizes: list[str] = []
    countries: list[str] = []
    company_names: list[str] = []
    domains: list[str] = []
    seniorities: list[str] = []
    size: int = 40            # results per page to pull from Lusha
    exclude_dnc: bool = True
    write: bool = True
    raw_filters: dict | None = None   # power-user passthrough


def _write_contacts(contacts: list[dict[str, Any]], do_write: bool) -> dict[str, Any]:
    """Dedupe + write callable contacts into the sheet. Returns a report."""
    callable_ = [c for c in contacts if c.get("phone")]
    report: dict[str, Any] = {
        "discovered": len(contacts),
        "callable": len(callable_),
        "written": 0,
        "skipped": 0,
        "rows": [],
    }
    if not callable_:
        return report

    if not do_write:
        report["rows"] = [
            {"name": c["name"], "title": c["title"], "phone": c["phone"],
             "email": c["email"], "company": c["company"], "written": False}
            for c in callable_[:MAX_WRITE]
        ]
        return report

    sc = lc.sheets()
    existing = sc.existing_phones()
    written = 0
    for c in callable_:
        if written >= MAX_WRITE:
            break
        try:
            lead = lc.Lead(
                name=c["name"] or "Unknown",
                phone=c["phone"],
                company=c["company"],
                email=c["email"],
                role=c["title"],
                source="lusha_prospecting",
                notes=f"Lusha prospecting · {c['title']}",
            )
        except Exception as exc:  # noqa: BLE001
            report["skipped"] += 1
            report["rows"].append({"name": c["name"], "error": str(exc), "written": False})
            continue
        if lead.phone in existing:
            report["skipped"] += 1
            report["rows"].append({"name": lead.name, "phone": lead.phone,
                                   "reason": "duplicate", "written": False})
            continue
        sc.append_rows([lc._row_for(lead)])
        existing.add(lead.phone)
        written += 1
        report["rows"].append({"name": lead.name, "title": lead.role,
                               "phone": lead.phone, "company": lead.company, "written": True})
    report["written"] = written
    return report


@app.post("/prospect/companies")
async def prospect_companies(req: CompaniesRequest):
    if not lp.is_configured():
        raise HTTPException(503, "LUSHA_API_KEY not configured")
    contacts = await lp.by_company_domains(req.domains, max_per_company=req.max_per_company)
    report = _write_contacts(contacts, req.write)
    return {"status": "ok", "mode": "domains", **report}


@app.post("/prospect/search")
async def prospect_search(req: SearchRequest):
    if not lp.is_configured():
        raise HTTPException(503, "LUSHA_API_KEY not configured")

    filters = req.raw_filters or lp.build_contact_filters(
        industries=req.industries or None,
        revenues=req.revenues or None,
        sizes=req.sizes or None,
        countries=req.countries or None,
        company_names=req.company_names or None,
        domains=req.domains or None,
        seniorities=req.seniorities or None,
    )

    ids = await lp.search_contacts(filters, size=req.size, exclude_dnc=req.exclude_dnc)
    if not ids:
        return {"status": "ok", "mode": "search", "discovered": 0, "callable": 0,
                "written": 0, "skipped": 0, "rows": [],
                "note": "No contacts. Check plan tier (Prospecting needs Premium/Scale) "
                        "or loosen filters."}

    contacts = await lp.enrich_contacts(ids[:100], prospecting=True)
    report = _write_contacts(contacts, req.write)
    return {"status": "ok", "mode": "search", "matched_ids": len(ids), **report}


@app.get("/health")
def health():
    out: dict[str, Any] = {
        "ok": True,
        "lusha_configured": lp.is_configured(),
        "sheet_id_set": bool(lc.SHEET_ID),
        "creds_present": os.path.exists(lc.GOOGLE_CREDS_PATH),
        "max_write": MAX_WRITE,
    }
    try:
        lc.sheets().fetch_all_rows()
        out["sheet_reachable"] = True
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["sheet_reachable"] = False
        out["error"] = str(exc)
    return out
