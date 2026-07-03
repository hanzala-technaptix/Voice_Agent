# Lead Acquisition — Ready for Data Extraction

## Status: ✓ Ready

The lead acquisition system is complete and ready to ingest raw leads from any source.

---

## What's New

### New Services
- **lead_acquisition.py** (port 8200) — Accepts raw leads, enriches them, scores them, and writes qualified records into the Google Sheet CRM.
- **tools/lusha.py** — Optional Lusha API enrichment wrapper (configurable via `LUSHA_API_KEY`).
- **tools/lead_qualification.py** — Scoring, normalization, and lead qualification logic.
- **n8n/Flow_E_Acquisition.json** — Optional n8n webhook to post acquisition leads.

### Existing Services Unchanged
- `dispatch.py` (port 8000) — SIP outbound unchanged
- `agent.py` — Voice agent unchanged
- `lead_collector.py` (port 8100) — Core lead ingestion unchanged

---

## Quick Start

### 1. Start the Acquisition Service

```powershell
uvicorn lead_acquisition:app --host 0.0.0.0 --port 8200
```

### 2. Send a Raw Lead

```powershell
curl -X POST http://localhost:8200/acquire `
  -H "Content-Type: application/json" `
  -d '{
    "name": "Jane Doe",
    "phone": "+14155551234",
    "company": "Acme Inc.",
    "email": "jane@example.com",
    "title": "VP Sales",
    "linkedin_url": "https://www.linkedin.com/in/janedoe",
    "source": "webhook",
    "auto_ingest": false
  }'
```

### 3. Check the Response

```json
{
  "status": "ok",
  "lead": {
    "name": "Jane Doe",
    "phone": "+14155551234",
    "company": "Acme Inc.",
    "email": "jane@example.com",
    "title": "VP Sales",
    "source": "webhook",
    "linkedin_url": "https://www.linkedin.com/in/janedoe",
    "campaign": "",
    "timezone": "",
    "notes": ""
  },
  "score": {
    "score": 90,
    "reasons": [
      "valid phone",
      "valid email",
      "company provided",
      "name provided",
      "source=webhook"
    ],
    "recommended_action": "push to voice dialer"
  },
  "auto_ingest": false,
  "recommended_action": "push to voice dialer",
  "ingested": false,
  "lead_id": null
}
```

---

## API Reference

### POST /acquire
Single lead acquisition with enrichment and scoring.

**Request:**
```json
{
  "name": "string",
  "phone": "string",
  "company": "string (optional)",
  "email": "string (optional)",
  "title": "string (optional)",
  "linkedin_url": "string (optional)",
  "campaign": "string (optional)",
  "timezone": "string (optional)",
  "source": "string (optional, default: lead_acquisition)",
  "notes": "string (optional)",
  "auto_ingest": "boolean (optional, default: false)"
}
```

**Response:**
```json
{
  "status": "ok",
  "lead": { ... enriched/normalized lead ... },
  "score": {
    "score": number (0-100),
    "reasons": [string],
    "recommended_action": "push to voice dialer | review before dialing"
  },
  "auto_ingest": boolean,
  "recommended_action": "string",
  "ingested": boolean,
  "lead_id": "string or null"
}
```

### POST /acquire/bulk
Bulk acquisition (up to 500 leads at once).

**Request:**
```json
{
  "leads": [
    { ... same as single lead ... },
    { ... }
  ]
}
```

**Response:**
```json
{
  "status": "ok",
  "accepted": number,
  "skipped": number,
  "results": [
    {
      "lead": { ... },
      "score": { ... },
      "ingested": boolean,
      "reason": "string or empty"
    }
  ]
}
```

### GET /health
Health check for the service.

**Response:**
```json
{
  "ok": boolean,
  "sheet_id": boolean,
  "creds_present": boolean,
  "lusha_configured": boolean,
  "sheet_reachable": boolean,
  "error": "string or null"
}
```

---

## Lead Scoring

Leads are scored 0–100 based on:
- **Valid phone** (+30) — must be E.164 format
- **Valid email** (+25) — must be valid format
- **Company provided** (+15)
- **Name provided** (+10)
- **Decision-maker title** (+20) — CEO, VP, Director, etc.
- **Inbound source** (+10) — if source is in `{webhook, referral, inbound, website, manual, organic}`
- **LinkedIn profile** (+10) — if linkedin_url provided

**Auto-ingest threshold:** default 65 (configurable via `ACQUISITION_AUTO_INGEST_MIN_SCORE`)

---

## Data Flow

```
Raw Lead (any source)
    ↓
  POST /acquire
    ↓
Normalize fields (name, phone, company, email, etc.)
    ↓
[Optional] Enrich via Lusha (if LUSHA_API_KEY set)
    ↓
Score lead (0–100)
    ↓
Check score against threshold
    ↓
If score >= threshold OR auto_ingest=true:
  → Validate and write to Google Sheet CRM
  → Return ingested=true + lead_id
Else:
  → Return ingested=false + recommended_action
```

---

## Integration Points

### Option 1: Direct HTTP
```powershell
curl -X POST http://localhost:8200/acquire `
  -H "Content-Type: application/json" `
  -d '{"name":"...","phone":"...","..."}'
```

### Option 2: n8n Flow E
Import `n8n/Flow_E_Acquisition.json` and configure the webhook. Flow E:
1. Receives raw leads via public webhook
2. POST to `/acquire`
3. Returns scoring result

### Option 3: CSV / Bulk Import
```powershell
curl -X POST http://localhost:8200/acquire/bulk `
  -H "Content-Type: application/json" `
  -d '{
    "leads": [
      {"name":"Lead 1","phone":"+1..."},
      {"name":"Lead 2","phone":"+1..."}
    ]
  }'
```

---

## Environment Variables

```
# Required (shared with voice agent)
LEAD_SHEET_ID=<your-sheet-id>
GOOGLE_SERVICE_ACCOUNT_JSON=service-account.json

# Optional acquisition settings
LUSHA_API_KEY=<lusha-api-key>
ACQUISITION_AUTO_INGEST_MIN_SCORE=65
```

---

## Next Steps

1. **Test with curl** — Use the examples above to send test leads
2. **Connect n8n** — Import `n8n/Flow_E_Acquisition.json` for webhook ingestion
3. **Configure Lusha** (optional) — Add `LUSHA_API_KEY` to `.env` for enrichment
4. **Monitor the sheet** — Qualified leads appear in the CRM automatically
5. **Dial with Flow A** — Once leads are in the sheet, n8n Flow A picks them up for voice outreach

---

## Troubleshooting

**503 / Sheet unreachable?**
- Verify `LEAD_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`
- Ensure service account email has Editor access to the sheet

**Score too low, lead not ingesting?**
- Set `auto_ingest: true` on the request to force ingestion
- Or increase `ACQUISITION_AUTO_INGEST_MIN_SCORE` in `.env`

**Lusha enrichment not working?**
- Check `LUSHA_API_KEY` is set in `.env`
- Service logs will show enrichment attempts

**Duplicate phone already in sheet?**
- Single leads return 409 Conflict
- Bulk import skips with `"reason": "duplicate"`

---

## Ready to Extract Data

The acquisition service is running and waiting for leads. You can start sending data from:
- Web forms (webhook)
- CSV imports (bulk API)
- Apollo, LinkedIn Sales Navigator, HubSpot, etc. (via Zapier/n8n)
- Manual curl requests

Check `health` endpoint to verify everything is connected, then POST your first lead.
