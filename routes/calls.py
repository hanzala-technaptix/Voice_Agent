"""Call dispatch route — POST /call."""

from fastapi import APIRouter

from schemas.calls import CallRequest
from services.call_dispatch import trigger_call

router = APIRouter()


@router.post("/call")
async def create_call(req: CallRequest):
    return await trigger_call(req)
