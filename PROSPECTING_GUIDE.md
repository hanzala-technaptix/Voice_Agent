# Prospecting Flow — company → decision makers → CRM → agent calls

This adds a discovery layer in front of the dialer. Instead of importing leads
by hand, you give it an ICP (or a list of company domains) and it finds the
CEO / CTO / Director, reveals their phone, and drops them into the Lead sheet as
`New Lead` — which Flow A then dials automatically.

## Pieces

| File | Role |
|------|------|
| `tools/lusha_prospect.py` | Lusha client: decision-makers, ICP contact search, enrich |
| `prospecting.py` | FastAPI service (port 8300): discover → enrich → write to sheet |
| `Flow_F_Prospecting.json` | n8n webhook `/prospect` → calls the service |
| `test_lusha.py` | one-company end-to-end test |

## Requirements

- Lusha **Premium or Scale** plan with API access (Prospecting + Decision-Makers
  are not on lower tiers; you'll get 401/403 otherwise).
- `LUSHA_API_KEY` in `.env`.
- Same Google Sheets creds the lead collector uses.

## .env

```
LUSHA_API_KEY=...
PROSPECT_MAX_WRITE=25            # safety cap on rows written per request
PROSPECT_SENIORITIES=owner,cxo,vp,director,head
```

## Run

```powershell
uvicorn prospecting:app --host 0.0.0.0 --port 8300
```

## Test first (no credits wasted on the sheet)

```powershell
python test_lusha.py somecompany.com --no-write
```

If people come back, run it for real:

```powershell
python test_lusha.py somecompany.com
```

## Use it

**You already know the companies:**

```powershell
curl.exe -X POST http://localhost:8300/prospect/companies `
  -H "Content-Type: application/json" `
  -d "@companies.json"
```

`companies.json`:
```json
{ "domains": ["acme.com", "globex.com"], "max_per_company": 5, "write": true }
```

**You want to discover companies by ICP:**

```json
{
  "industries": ["Manufacturing"],
  "revenues": ["10M-50M"],
  "countries": ["Pakistan"],
  "seniorities": ["cxo", "director"],
  "size": 40,
  "write": true
}
```

POST that to `/prospect/search`. Set `"write": false` to preview without
touching the sheet.

## n8n

Import `Flow_F_Prospecting.json`, activate it. It exposes `POST /prospect`:
- payload has `domains` → routes to `/prospect/companies`
- payload has ICP filters → routes to `/prospect/search`

## What this can and can't filter

**Can:** industry, revenue band, employee size, country, company name/domain,
contact seniority, and "has a phone on file" (`existingDataPoints: ["phone"]`),
plus DNC exclusion on Scale plans.

**Can't:** "companies that hit a revenue/growth target this year" — Lusha has no
such field. The closest real proxy is a growth **signal** (hiring surge,
headcount jump, funding) combined with a revenue band. If you want that, it's a
Scale-tier signals filter; tell me and I'll wire it into `build_contact_filters`.

## Note on filter values

Lusha expects specific codes for some filters (industry IDs, revenue/size bands,
seniority codes). If a search returns 0 with a valid plan, the filter values are
probably wrong. Use Lusha's filter-discovery endpoints (e.g.
`/prospecting/filters/companies/industriesLabels`) to fetch valid values, or
pass a hand-built filter object via `"raw_filters"` in the `/prospect/search`
body to bypass the helper entirely.
