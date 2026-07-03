"""Lead acquisition service.

Receives raw inbound leads, optionally enriches via Lusha, scores them, and
writes qualified leads into the Google Sheets CRM using the existing
lead_collector sheet schema.

Run:
    uvicorn lead_acquisition:app --host 0.0.0.0 --port 8200
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, ValidationError

import lead_collector as lc
from tools import lead_qualification, lusha

load_dotenv()

logger = logging.getLogger("lead-acquisition")
logging.basicConfig(level=logging.INFO)

ACQUISITION_AUTO_INGEST_MIN_SCORE = int(os.getenv("ACQUISITION_AUTO_INGEST_MIN_SCORE", "65"))
ACQUISITION_SOURCE = os.getenv("ACQUISITION_SOURCE", "lead_acquisition")

app = FastAPI(title="Technaptix Lead Acquisition")


class AcquisitionLead(BaseModel):
    name: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    company: str = ""
    email: str = ""
    source: str = ACQUISITION_SOURCE
    notes: str = ""
    title: str = ""
    linkedin_url: str = ""
    campaign: str = ""
    timezone: str = ""
    auto_ingest: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("name is required")
        return value.strip()

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("phone is required")
        return value.strip()

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return (value or "").strip().lower()


class BulkAcquisition(BaseModel):
    leads: list[AcquisitionLead] = Field(..., min_length=1, max_length=500)


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return lead_qualification.normalize_lead(payload)


async def _maybe_enrich(lead: dict[str, Any]) -> dict[str, Any]:
    if not lusha.is_configured():
        return lead

    enriched = await lusha.enrich_contact(
        name=lead.get("name", ""),
        company=lead.get("company", ""),
        email=lead.get("email", ""),
        phone=lead.get("phone", ""),
        linkedin_url=lead.get("linkedin_url", ""),
    )
    if not enriched:
        return lead

    merged = dict(lead)
    for key in ("name", "company", "email", "phone", "title", "linkedin_url"):
        if not merged.get(key) and enriched.get(key):
            merged[key] = enriched.get(key)
    if not merged.get("source"):
        merged["source"] = enriched.get("source", lead.get("source", ACQUISITION_SOURCE))
    return merged


def _score_lead(lead: dict[str, Any]) -> dict[str, Any]:
    return lead_qualification.score_lead(lead)


def _create_lead_record(lead: dict[str, Any]) -> dict[str, Any]:
    try:
        validated = lc.Lead(
            name=lead.get("name", ""),
            phone=lead.get("phone", ""),
            company=lead.get("company", ""),
            email=lead.get("email", ""),
            source=lead.get("source", ACQUISITION_SOURCE),
            notes=lead.get("notes", ""),
        )
    except ValidationError as exc:
        raise HTTPException(422, detail=str(exc))

    sheets = lc.sheets()
    existing = sheets.existing_phones()
    if validated.phone in existing:
        raise HTTPException(409, detail=f"phone {validated.phone} already in CRM")

    row = lc._row_for(validated)
    sheets.append_rows([row])
    logger.info("acquired lead: %s %s", validated.name, validated.phone)
    return {
        "lead_id": row[0],
        "phone": validated.phone,
        "name": validated.name,
        "company": validated.company,
        "email": validated.email,
        "source": validated.source,
    }


@app.post("/acquire")
async def acquire_lead(lead: AcquisitionLead):
    payload = _normalize_payload(lead.model_dump())
    enriched = await _maybe_enrich(payload)
    scored = _score_lead(enriched)

    ingest = lead.auto_ingest or scored["score"] >= ACQUISITION_AUTO_INGEST_MIN_SCORE
    intake = ingest
    result: dict[str, Any] = {
        "status": "ok",
        "lead": enriched,
        "score": scored,
        "auto_ingest": intake,
        "recommended_action": scored["recommended_action"],
        "ingested": False,
        "lead_id": None,
    }

    if ingest:
        record = _create_lead_record(enriched)
        result["ingested"] = True
        result["lead_id"] = record["lead_id"]

    return result


@app.post("/acquire/bulk")
async def acquire_leads_bulk(payload: BulkAcquisition):
    results: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for lead in payload.leads:
        payload_dict = _normalize_payload(lead.model_dump())
        enriched = await _maybe_enrich(payload_dict)
        scored = _score_lead(enriched)
        ingest = lead.auto_ingest or scored["score"] >= ACQUISITION_AUTO_INGEST_MIN_SCORE
        record: dict[str, Any] = {
            "lead": enriched,
            "score": scored,
            "ingested": False,
            "reason": "",
        }
        try:
            if ingest:
                created = _create_lead_record(enriched)
                record["ingested"] = True
                record["lead_id"] = created["lead_id"]
                accepted.append(created)
            else:
                record["reason"] = "score_below_threshold"
                skipped.append(record)
        except HTTPException as exc:
            record["reason"] = str(exc.detail or exc)
            skipped.append(record)
        results.append(record)

    return {
        "status": "ok",
        "accepted": len([r for r in results if r["ingested"]]),
        "skipped": len([r for r in results if not r["ingested"]]),
        "results": results,
    }


@app.get("/health")
def health():
    out: dict[str, Any] = {
        "ok": True,
        "sheet_id": bool(lc.SHEET_ID),
        "creds_present": os.path.exists(lc.GOOGLE_CREDS_PATH),
        "lusha_configured": lusha.is_configured(),
    }
    try:
        sheets = lc.sheets()
        sheets.fetch_all_rows()
        out["sheet_reachable"] = True
    except Exception as exc:
        out["ok"] = False
        out["sheet_reachable"] = False
        out["error"] = str(exc)
    return out
