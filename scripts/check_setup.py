"""
check_setup.py — Preflight verifier. Run BEFORE every demo.

Verifies, in order:
  1. All required env vars are present (via core.config validation).
  2. LiveKit credentials work (list_rooms succeeds).
  3. Cal.com API key works AND has at least one slot in the next 5 days.
  4. OpenAI key is non-empty (full network check is overkill;
     LiveKit will surface real auth errors on first call).
  5. Google Sheets service account can read the lead sheet (deferred).
  6. SIP outbound trunk id is set and format-valid (ST_xxxx).

Exit code 0 = all green, anything else = at least one check failed.
"""

import asyncio
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure project root is on sys.path when run as `python scripts/check_setup.py`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def check_config() -> bool:
    try:
        from core.config import settings  # noqa: F401 — triggers validation on import
    except Exception as e:  # noqa: BLE001
        fail(f"configuration invalid: {e}")
        return False
    ok("core.config validated all required .env variables")
    return True


async def check_livekit() -> bool:
    try:
        from livekit import api
    except ImportError:
        fail("livekit-api not installed")
        return False
    lkapi = api.LiveKitAPI()
    try:
        await lkapi.room.list_rooms(api.ListRoomsRequest())
        ok("LiveKit credentials valid")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"LiveKit auth failed: {e}")
        return False
    finally:
        await lkapi.aclose()


async def check_calcom() -> bool:
    try:
        from integrations import calcom
    except ImportError as e:
        fail(f"could not import integrations.calcom: {e}")
        return False
    try:
        slots = await calcom.get_available_slots(days_ahead=5, max_slots=1)
    except Exception as e:  # noqa: BLE001
        fail(f"Cal.com slot fetch failed: {e}")
        return False
    if not slots:
        fail("Cal.com returned ZERO slots in the next 5 days — check event-type availability")
        return False
    ok(f"Cal.com OK ({len(slots)} slot found; first = {slots[0]['label']})")
    return True


def check_sheets() -> bool:
    print("  ! lead_collector not yet restored — skipping Sheets check")
    return True


def check_envs() -> bool:
    if not check_config():
        return False
    from core.config import settings

    trunk = settings.sip_outbound_trunk_id
    if not re.match(r"^ST_[A-Za-z0-9]+$", trunk):
        fail(f"SIP_OUTBOUND_TRUNK_ID format invalid (expected ST_xxxx): {trunk!r}")
        return False
    ok(f"SIP outbound trunk configured ({trunk})")
    if not settings.n8n_results_webhook:
        print("  ! N8N_RESULTS_WEBHOOK not set — calls.log only; sheet won't auto-update")
    else:
        ok("N8N_RESULTS_WEBHOOK set")
    return True


async def main() -> int:
    print("Preflight check — Technaptix Voice Agent")
    print("=" * 50)
    print("[1] Environment")
    e_ok = check_envs()
    print("[2] LiveKit")
    lk_ok = await check_livekit() if e_ok else False
    print("[3] Cal.com")
    cal_ok = await check_calcom() if e_ok else False
    print("[4] Google Sheets")
    sh_ok = check_sheets()
    print("=" * 50)
    all_ok = all([e_ok, lk_ok, cal_ok, sh_ok])
    print("RESULT:", "READY ✓" if all_ok else "NOT READY ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
