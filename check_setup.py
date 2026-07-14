"""
check_setup.py — Preflight verifier. Run BEFORE every demo.

Verifies, in order:
  1. All required env vars are present (via config.py validation).
  2. LiveKit credentials work (list_rooms succeeds).
  3. Cal.com API key works AND has at least one slot in the next 5 days.
  4. OpenAI key is non-empty (full network check is overkill;
     LiveKit will surface real auth errors on first call).
  5. Google Sheets service account can read the lead sheet.
  6. SIP outbound trunk id is set and format-valid (ST_xxxx).

Exit code 0 = all green, anything else = at least one check failed.
"""

import asyncio
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def check_config() -> bool:
    try:
        from config import settings  # noqa: F401 — triggers validation on import
    except Exception as e:  # noqa: BLE001
        fail(f"configuration invalid: {e}")
        return False
    ok("config.py validated all required .env variables")
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
        from tools import calcom
    except ImportError as e:
        fail(f"could not import tools.calcom: {e}")
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
    sheet_id = os.getenv("LEAD_SHEET_ID")
    if not sheet_id:
        fail("LEAD_SHEET_ID not set (lead collector + n8n need it)")
        return False
    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json")
    if not os.path.exists(creds_path):
        fail(f"service account JSON missing at {creds_path}")
        return False
    try:
        from leads.lead_collector import SheetsClient
    except ImportError as e:
        fail(f"could not import leads.lead_collector: {e}")
        return False
    try:
        sc = SheetsClient(sheet_id, os.getenv("LEAD_SHEET_TAB", "Lead"))
        rows = sc.fetch_all_rows()
        ok(f"Sheets reachable; {len(rows)} existing rows")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"Sheets read failed: {e}")
        return False


def check_envs() -> bool:
    if not check_config():
        return False
    from config import settings

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