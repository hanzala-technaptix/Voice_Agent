"""Health check route — GET /health."""

from livekit import api
from fastapi import APIRouter

from services.call_dispatch import AGENT_NAME, MAX_CONCURRENT_CALLS, SIP_OUTBOUND_TRUNK_ID

router = APIRouter()


@router.get("/health")
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
