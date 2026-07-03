"""Lusha lead enrichment and discovery helpers.

This wrapper is intentionally lightweight and optional. The service falls back to
basic acquisition behavior when no Lusha API key is configured.
"""

import os
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("outbound-agent.lusha")

LUSHA_API_BASE = os.getenv("LUSHA_API_BASE", "https://api.lusha.com")
LUSHA_API_KEY = os.getenv("LUSHA_API_KEY", "").strip()
LUSHA_PERSON_PATH = os.getenv("LUSHA_PERSON_PATH", "/v1/person")
LUSHA_SEARCH_PATH = os.getenv("LUSHA_SEARCH_PATH", "/v1/search")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {LUSHA_API_KEY}",
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(LUSHA_API_KEY)


async def enrich_contact(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    linkedin_url: str = "",
) -> dict[str, Any]:
    """Attempt to enrich a lead from Lusha. Returns raw JSON on success.

    If the configured endpoint does not match the current Lusha API, this will
    log the failure without breaking the caller.
    """
    if not is_configured():
        return {}

    payload: dict[str, Any] = {"name": name}
    if company:
        payload["company"] = company
    if email:
        payload["email"] = email
    if phone:
        payload["phone"] = phone
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url

    url = f"{LUSHA_API_BASE.rstrip('/')}{LUSHA_PERSON_PATH}"
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lusha enrich_contact failed: %s", exc)
            return {}


async def search_contacts(
    query: str,
    company: str = "",
    title: str = "",
    location: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Discover contacts by search criteria."""
    if not is_configured():
        return []

    payload: dict[str, Any] = {
        "query": query,
        "limit": limit,
    }
    if company:
        payload["company"] = company
    if title:
        payload["title"] = title
    if location:
        payload["location"] = location

    url = f"{LUSHA_API_BASE.rstrip('/')}{LUSHA_SEARCH_PATH}"
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                return data["data"]
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lusha search_contacts failed: %s", exc)
            return []
