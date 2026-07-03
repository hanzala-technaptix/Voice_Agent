"""
Cal.com API v2 client.
Two functions the agent uses mid-call:
  - get_available_slots()  -> human-labelled open slots
  - book_meeting()         -> creates the booking; Cal.com then auto-sends the
                              calendar invite + Google Meet link to the attendee.

Docs: https://cal.com/docs/api-reference  (v2 endpoints version via the
`cal-api-version` header — bump these if Cal.com revs them).

Verified 2026-06-10 against the live docs:
  - GET  /v2/slots    -> cal-api-version: 2024-09-04
  - POST /v2/bookings -> cal-api-version: 2024-08-13
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("outbound-agent.calcom")

CAL_BASE = "https://api.cal.com/v2"
CAL_API_KEY = os.getenv("CALCOM_API_KEY", "")
EVENT_TYPE_ID = int(os.getenv("CALCOM_EVENT_TYPE_ID", "0"))
# Timezone slots are presented in (the prospect's). Keep in sync with prompts.py.
PROSPECT_TZ = os.getenv("PROSPECT_TZ", "America/New_York")

# Rehearsal mode: skip the real booking call and return a fake confirmation so we
# can practice the full call flow without creating real calendar events.
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

_SLOTS_VERSION = "2024-09-04"
_BOOKINGS_VERSION = "2024-08-13"


def _headers(api_version: str) -> dict:
    return {
        "Authorization": f"Bearer {CAL_API_KEY}",
        "cal-api-version": api_version,
        "Content-Type": "application/json",
    }


def _raise_for_status_loud(resp: httpx.Response, what: str) -> None:
    """Like resp.raise_for_status() but logs the response body first. Cal.com's
    most common booking failure (a required custom booking field) returns a 400
    whose body explains exactly which field is missing — don't swallow it."""
    if resp.is_error:
        logger.error("Cal.com %s failed: HTTP %s — %s", what, resp.status_code, resp.text[:1000])
    resp.raise_for_status()


async def get_available_slots(days_ahead: int = 5, max_slots: int = 4) -> list[dict]:
    """Return up to `max_slots` open slots over the next `days_ahead` days,
    spread across different days where possible."""
    tz = ZoneInfo(PROSPECT_TZ)
    start = datetime.now(timezone.utc) + timedelta(hours=4)  # never offer "in 5 minutes"
    end = start + timedelta(days=days_ahead)

    params = {
        "eventTypeId": EVENT_TYPE_ID,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "timeZone": PROSPECT_TZ,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{CAL_BASE}/slots", headers=_headers(_SLOTS_VERSION), params=params)
        _raise_for_status_loud(resp, "slots fetch")
        data = resp.json().get("data", {})

    # data shape (cal-api-version 2024-09-04, default format=time):
    #   { "2026-06-11": ["2026-06-11T13:00:00.000Z", ...], ... }
    # Older/range responses use [{"start": "..."}]; handle both defensively.
    slots: list[dict] = []
    for _day, day_slots in sorted(data.items()):
        if not day_slots:
            continue
        # take the first reasonable slot per day to spread options across days
        raw = day_slots[0]
        iso = raw["start"] if isinstance(raw, dict) else raw
        try:
            dt_local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        except (ValueError, AttributeError):
            logger.warning("could not parse Cal.com slot start %r — skipping", raw)
            continue
        slots.append(
            {
                "start": iso,
                "label": dt_local.strftime("%A, %B %d at %I:%M %p").replace(" 0", " "),
            }
        )
        if len(slots) >= max_slots:
            break
    return slots


async def book_meeting(start_iso: str, name: str, email: str, phone: str = "", company: str = "") -> dict:
    """Create the booking. Cal.com immediately emails the attendee a calendar
    invite containing the Google Meet link — no further action needed.

    In DRY_RUN mode this returns a fake confirmation and makes no API call."""
    tz = ZoneInfo(PROSPECT_TZ)
    dt_local = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(tz)
    start_label = dt_local.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")

    if DRY_RUN:
        logger.warning("DRY_RUN active — skipping real Cal.com booking for %s at %s", email, start_label)
        return {
            "booking_id": "DRY_RUN",
            "start": start_iso,
            "start_label": start_label,
            "meet_link": "sent via email (DRY_RUN — no real invite created)",
            "attendee_email": email,
            "dry_run": True,
        }

    body = {
        "start": start_iso,
        "eventTypeId": EVENT_TYPE_ID,
        "attendee": {
            "name": name,
            "email": email,
            "timeZone": PROSPECT_TZ,
            "language": "en",
        },
        "metadata": {
            "source": "technaptix-voice-agent",
            "phone": phone,
            "company": company,
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{CAL_BASE}/bookings", headers=_headers(_BOOKINGS_VERSION), json=body)
        _raise_for_status_loud(resp, "booking create")
        data = resp.json().get("data", {})

    return {
        "booking_id": data.get("uid") or data.get("id"),
        "start": start_iso,
        "start_label": start_label,
        "meet_link": (data.get("location") or data.get("meetingUrl") or "sent via email"),
        "attendee_email": email,
    }
