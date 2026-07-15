"""Pydantic schemas for call dispatch endpoints."""

import re

from pydantic import BaseModel, field_validator

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class CallRequest(BaseModel):
    phone: str
    name: str = "there"
    company: str = ""
    email: str = ""
    notes: str = ""
    timezone: str = ""
    row_id: str = ""
    lead_id: str = ""
    trigger_type: str = "new"
    call_attempt: int = 1

    @field_validator("phone")
    @classmethod
    def _phone_e164(cls, v: str) -> str:
        # This dialer calls internationally (US, PK, SA, anywhere), so we must
        # NOT guess a country code. Every lead must already carry its own
        # country code in E.164 form (+<country><number>). We accept a "00"
        # international prefix as an alias for "+", clean separators, validate.
        raw = (v or "").strip()
        if raw.startswith("00"):
            raw = "+" + raw[2:]
        if not raw.startswith("+"):
            raise ValueError(
                f"phone must include a country code in E.164 form "
                f"(e.g. +14155550100, +923001234567, +966500000000); got {v!r}. "
                f"This dialer is international and cannot assume a country."
            )
        phone = "+" + re.sub(r"\D", "", raw[1:])
        if not E164_RE.match(phone):
            raise ValueError(f"phone is not valid E.164 after cleanup: {phone!r} (from {v!r})")
        return phone
