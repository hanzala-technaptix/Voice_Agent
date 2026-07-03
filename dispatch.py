"""
Call dispatcher — SIP/PSTN outbound (direct phone dial).

n8n Flow A POSTs one lead at a time to /call. We create a LiveKit room,
dispatch the AI agent worker, and dial the prospect's phone via the configured
SIP outbound trunk. No browser links or join URLs.

Run:
    uvicorn dispatch:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import logging
import os
import re
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from livekit import api

load_dotenv()

logger = logging.getLogger("dispatch")
AGENT_NAME = "outbound-caller"
MAX_CONCURRENT_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "3"))
# Only used by /health as a config sanity check — the AGENT does the dialing
# now and reads the SIP trunk/caller-id/ring-timeout from the shared .env.
SIP_OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

app = FastAPI(title="Technaptix Voice Agent Dispatcher (SIP Outbound)")
_concurrency = asyncio.Semaphore(MAX_CONCURRENT_CALLS)


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


async def _create_dispatch_with_retry(metadata: dict, room_name: str) -> str:
    last_err: Exception | None = None
    for attempt in range(3):
        lkapi = api.LiveKitAPI()
        try:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME,
                    room=room_name,
                    metadata=json.dumps(metadata),
                )
            )
            return dispatch.id
        except Exception as e:  # noqa: BLE001
            last_err = e
            await asyncio.sleep(0.5 * (2 ** attempt))
        finally:
            await lkapi.aclose()
    raise HTTPException(502, f"agent dispatch failed after 3 attempts: {last_err}")


@app.post("/call")
async def trigger_call(req: CallRequest):
    """Create room + dispatch the agent. The AGENT places the actual phone call
    (via create_sip_participant with wait_until_answered=True), so we do NOT
    dial here. This keeps /call fast (returns in <1s) instead of blocking for
    the full ring duration, which matters for the n8n HTTP node."""
    async with _concurrency:
        room_name = f"call-{uuid4().hex[:10]}"
        metadata = req.model_dump(exclude_none=True)
        if not metadata.get("timezone"):
            metadata.pop("timezone", None)

        identity = f"lead-{(req.lead_id or req.row_id or uuid4().hex[:6])}"
        metadata["sip_identity"] = identity
        # phone is already validated to E.164 above; the agent reads it from metadata.

        dispatch_id = await _create_dispatch_with_retry(metadata, room_name)

    return {
        "status": "dispatched",
        "room": room_name,
        "dispatch_id": dispatch_id,
        "phone": req.phone,
        "lead_id": req.lead_id,
    }


@app.get("/health")
async def health():
    out: dict = {
        "agent": AGENT_NAME,
        "max_concurrent": MAX_CONCURRENT_CALLS,
        "sip_trunk_configured": bool(SIP_OUTBOUND_TRUNK_ID),
        "mode": "sip_outbound",
    }
    lkapi = api.LiveKitAPI()
    try:
        await lkapi.room.list_rooms(api.ListRoomsRequest())
        out["livekit_reachable"] = True
        out["ok"] = bool(SIP_OUTBOUND_TRUNK_ID)
        if not SIP_OUTBOUND_TRUNK_ID:
            out["error"] = "SIP_OUTBOUND_TRUNK_ID not set"
    except Exception as e:  # noqa: BLE001
        out["livekit_reachable"] = False
        out["ok"] = False
        out["error"] = str(e)
    finally:
        await lkapi.aclose()
    return out