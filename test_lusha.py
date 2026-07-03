"""
test_lusha.py — prove the whole flow on one company, end to end.

    python test_lusha.py acme.com              # discover + enrich + write to sheet
    python test_lusha.py acme.com --no-write   # discover + enrich only (no sheet rows)

Tests: decision-makers -> enrich -> sheet write, exactly what the service does.
Run --no-write first to confirm Lusha returns people before spending sheet rows.
"""

import asyncio
import sys

from dotenv import load_dotenv
load_dotenv()

from tools import lusha_prospect as lp  # noqa: E402
import lead_collector as lc             # noqa: E402


async def run(domain: str, write: bool) -> None:
    print("=" * 64)
    print("Lusha configured :", lp.is_configured())
    print("Domain           :", domain)
    print("Write to sheet   :", write)
    print("=" * 64)

    if not lp.is_configured():
        print("FATAL: LUSHA_API_KEY not set in .env")
        return

    print("\n[1+2] Decision-makers -> enrich (phone/email)...")
    contacts = await lp.by_company_domains([domain], max_per_company=5)
    if not contacts:
        print("  Nothing returned. Likely causes:")
        print("   - Plan lacks Prospecting/Decision-Makers (needs Premium/Scale)")
        print("   - 401/403 logged above (key/plan), or domain not in Lusha DB")
        return

    for c in contacts:
        print(f"   • {c['name']:<26} {c['title']:<24} "
              f"phone={c['phone'] or '—':<16} email={c['email'] or '—'}")

    callable_ = [c for c in contacts if c.get("phone")]
    if not callable_:
        print("\n  No revealable phone numbers — nothing callable to write.")
        return

    if not write:
        print(f"\n[3] --no-write set. Would write {len(callable_)} callable rows. Skipping.")
        return

    print(f"\n[3] Writing {len(callable_)} callable contacts to the sheet...")
    sc = lc.sheets()
    existing = sc.existing_phones()
    written = skipped = 0
    for c in callable_:
        try:
            lead = lc.Lead(name=c["name"] or "Unknown", phone=c["phone"],
                           company=c["company"] or domain, email=c["email"],
                           role=c["title"], source="lusha_test",
                           notes=f"Lusha decision-maker · {c['title']}")
        except Exception as exc:  # noqa: BLE001
            print(f"   skip {c['name']}: {exc}"); skipped += 1; continue
        if lead.phone in existing:
            print(f"   skip {lead.name}: already in CRM"); skipped += 1; continue
        sc.append_rows([lc._row_for(lead)])
        existing.add(lead.phone)
        written += 1
        print(f"   ✓ {lead.name} ({lead.phone})")

    print(f"\nDone. written={written} skipped={skipped}")
    print("These rows are now 'New Lead' — Flow A will dial them.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: python test_lusha.py example.com [--no-write]")
        sys.exit(1)
    asyncio.run(run(args[0].strip(), write="--no-write" not in sys.argv))
