"""Lead qualification utilities for acquisition and scoring."""

import re
from typing import Any

DECISION_MAKER_KEYWORDS = (
    "ceo",
    "founder",
    "owner",
    "president",
    "director",
    "vp",
    "vice president",
    "head of",
    "chief",
    "co-founder",
    "partner",
)

INBOUND_SOURCES = {
    "webhook",
    "referral",
    "inbound",
    "website",
    "manual",
    "organic",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _is_valid_email(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip().lower()))


def _is_valid_phone(phone: str) -> bool:
    return bool(phone and _PHONE_RE.match(phone.strip()))


def _title_score(title: str) -> int:
    title = (title or "").lower()
    for keyword in DECISION_MAKER_KEYWORDS:
        if keyword in title:
            return 20
    return 0


def score_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Return a quality score and rationale for a lead."""
    score = 0
    reasons: list[str] = []

    if _is_valid_phone(lead.get("phone", "")):
        score += 30
        reasons.append("valid phone")
    if _is_valid_email(lead.get("email", "")):
        score += 25
        reasons.append("valid email")
    if lead.get("company"):
        score += 15
        reasons.append("company provided")
    if lead.get("name"):
        score += 10
        reasons.append("name provided")
    title_bonus = _title_score(lead.get("title", ""))
    if title_bonus:
        score += title_bonus
        reasons.append("decision-maker title")
    source = (lead.get("source") or "").lower()
    if source in INBOUND_SOURCES:
        score += 10
        reasons.append(f"source={source}")
    if lead.get("linkedin_url"):
        score += 10
        reasons.append("linkedin profile")
    if score > 100:
        score = 100
    return {
        "score": score,
        "reasons": reasons,
        "recommended_action": (
            "push to voice dialer" if score >= 65 else "review before dialing"
        ),
    }


def normalize_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Normalize incoming acquisition fields into the expected shape."""
    return {
        "name": (lead.get("name") or lead.get("fullName") or lead.get("contact_name") or "").strip(),
        "phone": (lead.get("phone") or lead.get("mobile") or lead.get("telephone") or "").strip(),
        "email": (lead.get("email") or lead.get("email_address") or "").strip().lower(),
        "company": (lead.get("company") or lead.get("organization") or "").strip(),
        "title": (lead.get("title") or lead.get("job_title") or "").strip(),
        "source": (lead.get("source") or lead.get("utm_source") or lead.get("channel") or "lead_acquisition").strip(),
        "notes": (lead.get("notes") or lead.get("comments") or "").strip(),
        "linkedin_url": (lead.get("linkedin_url") or lead.get("linkedin") or lead.get("profile_url") or "").strip(),
        "campaign": (lead.get("campaign") or lead.get("utm_campaign") or "").strip(),
        "timezone": (lead.get("timezone") or "").strip(),
    }
