"""LiveKit call dispatch service — room creation and agent dispatch."""

import asyncio
import json
import logging
import os
from uuid import uuid4

from fastapi import HTTPException
from livekit import api

from core.config import settings
from schemas.calls import CallRequest

logger = logging.getLogger("dispatch")

AGENT_NAME = settings.agent_name
MAX_CONCURRENT_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "3"))
# Only used by /health as a config sanity check — the AGENT does the dialing
# now and reads the SIP trunk/caller-id/ring-timeout from the shared .env.
SIP_OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()

_concurrency = asyncio.Semaphore(MAX_CONCURRENT_CALLS)


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


async def trigger_call(req: CallRequest) -> dict:
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
