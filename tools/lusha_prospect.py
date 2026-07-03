"""
Lusha PROSPECTING client (discover net-new contacts).

Two discovery paths, both ending in enrich -> phone/email:

  A) by_company_domains()  — you already know the target companies.
        domains -> /v3/contacts/decision-makers -> /v3/contacts/enrich

  B) search_contacts()     — ICP filters (industry, revenue, size, location,
        seniority). Discovers companies + the CEO/CTO/Director in one search.
        filters -> /prospecting/contact/search -> /prospecting/contact/enrich

Auth: api_key header. Plan: Prospecting + Decision-Makers require a Lusha
Premium/Scale plan. Some filters (signals, DNC) are Scale-only and 403 otherwise.
Rate limit is low (~5 req/min on several endpoints) — don't hammer it.

Docs: https://docs.lusha.com/apis/openapi
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("prospecting.lusha")

LUSHA_API_KEY = os.getenv("LUSHA_API_KEY", "").strip()
BASE = os.getenv("LUSHA_BASE_URL", "https://api.lusha.com").rstrip("/")
TIMEOUT = float(os.getenv("LUSHA_TIMEOUT_SEC", "25"))

# Default seniority codes Lusha accepts in contact filters. Override via env.
DEFAULT_SENIORITIES = [
    s.strip() for s in os.getenv(
        "PROSPECT_SENIORITIES", "owner,cxo,vp,director,head"
    ).split(",") if s.strip()
]

# Title tokens we keep when filtering decision-maker previews client-side.
TARGET_TITLE_TOKENS = (
    "ceo", "chief executive", "cto", "chief technology", "coo", "cfo",
    "founder", "co-founder", "owner", "president", "managing director",
    "director", "vp", "vice president", "head of",
)


def is_configured() -> bool:
    return bool(LUSHA_API_KEY)


def _headers() -> dict[str, str]:
    return {
        "api_key": LUSHA_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _post(path: str, body: dict) -> dict | None:
    url = f"{BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, json=body, headers=_headers())
        if r.status_code in (401, 403):
            logger.error("lusha %s -> %s (auth/plan): %s", path, r.status_code, r.text[:300])
            return None
        if r.status_code == 429:
            logger.warning("lusha %s -> 429 rate limited", path)
            return None
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        logger.warning("lusha %s request failed: %s", path, exc)
        return None
    except ValueError:
        logger.warning("lusha %s: non-JSON response", path)
        return None


# --------------------------------------------------------------- shaping ----

def _title_ok(title: str, seniority: str) -> bool:
    t = (title or "").lower()
    return any(tok in t for tok in TARGET_TITLE_TOKENS) or (seniority or "").lower() in (
        "cxo", "owner", "founder", "partner", "vp", "director", "head", "c_team"
    )


def _first_phone(c: dict) -> str:
    for p in (c.get("phoneNumbers") or c.get("phones") or []):
        if isinstance(p, dict):
            n = p.get("internationalNumber") or p.get("number")
            if n:
                return str(n).strip()
        elif isinstance(p, str) and p.strip():
            return p.strip()
    return ""


def _first_email(c: dict) -> str:
    for e in (c.get("emailAddresses") or c.get("emails") or []):
        if isinstance(e, dict):
            a = e.get("email") or e.get("address")
            if a:
                return str(a).strip().lower()
        elif isinstance(e, str) and e.strip():
            return e.strip().lower()
    return ""


def _company_name(c: dict) -> str:
    comp = c.get("company")
    if isinstance(comp, dict):
        return comp.get("name") or ""
    return c.get("companyName") or (comp if isinstance(comp, str) else "") or ""


def _full_name(c: dict) -> str:
    return c.get("name") or (f"{c.get('firstName','')} {c.get('lastName','')}").strip()


def _contact_id(c: dict):
    return c.get("contactId") or c.get("id")


def _shape_enriched(c: dict) -> dict[str, Any]:
    return {
        "contact_id": str(_contact_id(c) or ""),
        "name": _full_name(c),
        "title": c.get("jobTitle") or c.get("title") or "",
        "phone": _first_phone(c),
        "email": _first_email(c),
        "company": _company_name(c),
        "linkedin_url": c.get("linkedinUrl") or c.get("linkedin_url") or "",
    }


def _iter_contacts(payload: dict) -> list[dict]:
    data = payload.get("data") or payload.get("contacts") or payload.get("results") or []
    if isinstance(data, dict):
        data = list(data.values())
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


# --------------------------------------------------- PATH A: by domains -----

async def by_company_domains(
    domains: list[str], *, max_per_company: int = 5
) -> list[dict[str, Any]]:
    """domains -> decision-makers -> enrich. Returns flat enriched contacts."""
    if not is_configured():
        return []
    companies = [{"domain": d.strip(), "clientReferenceId": d.strip()}
                 for d in domains if d.strip()]
    if not companies:
        return []

    payload = await _post("/v3/contacts/decision-makers", {"companies": companies})
    if not payload:
        return []

    # gather preview contact ids, filtered to decision-maker titles
    groups = payload.get("data") or payload.get("companies") or payload.get("results") or []
    if isinstance(groups, dict):
        groups = list(groups.values())
    preview_ids: list[str] = []
    preview_meta: dict[str, dict] = {}
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        contacts = g.get("decisionMakers") or g.get("contacts") or g.get("data") or []
        kept = 0
        for c in contacts:
            if not isinstance(c, dict):
                continue
            title = c.get("jobTitle") or c.get("title") or ""
            if not _title_ok(title, c.get("seniority", "")):
                continue
            cid = str(_contact_id(c) or "")
            if not cid:
                continue
            preview_ids.append(cid)
            preview_meta[cid] = {"title": title, "name": _full_name(c)}
            kept += 1
            if kept >= max_per_company:
                break

    if not preview_ids:
        logger.info("decision-makers: no matching titles returned")
        return []

    enriched = await enrich_contacts(preview_ids, prospecting=False)
    # backfill title/name from preview when enrich omits them
    for e in enriched:
        meta = preview_meta.get(e["contact_id"], {})
        e["title"] = e["title"] or meta.get("title", "")
        e["name"] = e["name"] or meta.get("name", "")
    return enriched


# --------------------------------------------- PATH B: ICP contact search ---

def build_contact_filters(
    *,
    industries: list[str] | None = None,
    revenues: list[str] | None = None,
    sizes: list[str] | None = None,
    countries: list[str] | None = None,
    company_names: list[str] | None = None,
    domains: list[str] | None = None,
    seniorities: list[str] | None = None,
) -> dict:
    """Assemble a Lusha contact-search filter object from high-level inputs."""
    company_inc: dict[str, Any] = {}
    if industries:
        company_inc["mainIndustriesIds"] = industries  # or industriesLabels; see note
        company_inc["industriesLabels"] = industries
    if revenues:
        company_inc["revenues"] = revenues
    if sizes:
        company_inc["sizes"] = sizes
    if countries:
        company_inc["companyLocations"] = [{"country": c} for c in countries]
    if company_names:
        company_inc["names"] = company_names
    if domains:
        company_inc["domains"] = domains

    contact_inc: dict[str, Any] = {
        "seniority": seniorities or DEFAULT_SENIORITIES,
        "existingDataPoints": ["phone"],   # only people who have a phone on file
    }

    filters: dict[str, Any] = {"contacts": {"include": contact_inc}}
    if company_inc:
        filters["companies"] = {"include": company_inc}
    return filters


async def search_contacts(
    filters: dict, *, page: int = 0, size: int = 40, exclude_dnc: bool = True
) -> list[str]:
    """Run prospecting contact search. Returns a list of contact IDs (previews)."""
    if not is_configured():
        return []
    body = {
        "pages": {"page": page, "size": size},
        "filters": filters,
        "excludeDnc": exclude_dnc,
        "includePartialContact": False,
    }
    payload = await _post("/prospecting/contact/search", body)
    if not payload:
        return []
    ids: list[str] = []
    for c in _iter_contacts(payload):
        cid = _contact_id(c)
        if cid:
            ids.append(str(cid))
    logger.info("prospecting search: %d contact ids (total=%s)",
                len(ids), payload.get("totalResults"))
    return ids


async def enrich_contacts(
    contact_ids: list[str], *, prospecting: bool = True
) -> list[dict[str, Any]]:
    """Reveal phone+email. prospecting=True uses /prospecting/contact/enrich
    (for search_contacts IDs); False uses /v3/contacts/enrich (decision-makers)."""
    ids = [str(i) for i in contact_ids if i]
    if not is_configured() or not ids:
        return []

    if prospecting:
        body = {"contactIds": ids, "reveal": ["emails", "phones"]}
        payload = await _post("/prospecting/contact/enrich", body)
    else:
        body = {"contactIds": ids, "reveal": ["emails", "phones"]}
        payload = await _post("/v3/contacts/enrich", body)

    if not payload:
        return []
    out = [_shape_enriched(c) for c in _iter_contacts(payload)]
    return [e for e in out if e["contact_id"]]
