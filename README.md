# Technaptix Voice Agent

Outbound AI voice agent: leads in Google Sheets → SIP phone dial → LiveKit AI conversation → Cal.com booking → transcript and outcome back to the sheet.

**No browser links. No Retell.** Direct PSTN via LiveKit SIP trunk.

---

## Architecture

```
Google Sheet (Lead)
       │
       ▼
  n8n Flow A ──POST /call──► dispatch.py ──SIP──► prospect's phone rings
       │                            │
       │                            ▼
       │                      agent.py (Groq LLM + Deepgram STT/TTS)
       │                            │
       │                     Cal.com booking (mid-call)
       │                            │
       └◄── n8n Flow B ◄── webhook (transcript + outcome)
```

| Layer | Component | Port |
|-------|-----------|------|
| CRM | Google Sheet + `lead_collector.py` | 8100 |
| Dialer | `dispatch.py` (SIP outbound) | 8000 |
| Voice AI | `agent.py` (LiveKit worker) | — |
| Orchestration | n8n Flow A, B, D | — |

---

## Repo layout

| Path | Purpose |
|------|---------|
| `agent.py` | LiveKit agent — conversation, Cal.com tools, transcript |
| `dispatch.py` | Creates room + agent dispatch + SIP dial |
| `lead_collector.py` | Ingest leads into the sheet |
| `transcript_utils.py` | Transcript flattening + outcome classification |
| `prompts.py` | Sales script (edit `PITCH`) |
| `tools/calcom.py` | Cal.com API client |
| `check_setup.py` | Preflight checks — run before every demo |
| `livekit/outbound-trunk.json` | SIP trunk template for Twilio |
| `n8n/Flow_A_Dialer.json` | Sheet trigger → SIP dial |
| `n8n/Flow_B_Results.json` | Webhook → update sheet + email |
| `n8n/Flow_C_Lead_Intake.json` | Optional webhook → collector |
| `n8n/Flow_D_Recovery.json` | Stuck-row recovery + auto-retry |
| `n8n/Flow_E_Acquisition.json` | Optional lead acquisition webhook |
| `config.py` | Validates required `.env` variables on import |

---

## Prerequisites

- LiveKit Cloud project + **SIP outbound trunk** (Twilio)
- Deepgram, Groq, OpenAI, Cal.com API keys
- Google Sheet (tab `Lead`) + service account JSON
- n8n (self-hosted or cloud)
- Python 3.11+

---

## Setup

### 1. Environment

```powershell
cd voice-agent-mvp
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Create .env in the project root and fill in every value (see config.py)
```

### 2. SIP trunk (one-time)

```bash
curl -sSL https://get.livekit.io/cli | bash
lk cloud auth
# Edit livekit/outbound-trunk.json with Twilio creds
lk sip outbound create livekit/outbound-trunk.json
# Copy ST_xxxx → .env SIP_OUTBOUND_TRUNK_ID
```

### 3. Google Sheet

Share the sheet with your service-account email (Editor). Place `service-account.json` in the project root.

Header row (tab `Lead`) — `lead_collector` writes this automatically on first run:

```
Lead ID | Name | Company | Phone | Email | Source | Created At |
Call Status | Call Outcome | Interested | Follow Up Required |
Notes | Transcript | Call Attempt | Next Call At | Last Updated
```

**Dial triggers:** `Call Status` = `New Lead`, `Call`, `Retry`, or `Follow-up`

### 4. n8n

Import from `n8n/`:

| Flow | Activate? | Role |
|------|-----------|------|
| Flow A | Yes | Dials on new/updated rows |
| Flow B | Yes | Receives call results |
| Flow C | Optional | Public lead webhook |
| Flow D | Yes | Recovery + retries |
| Flow E | Optional | Ingest acquisition leads |

**n8n variables:** `LEAD_SHEET_ID`, `DISPATCHER_URL`, `OWNER_EMAIL`

Copy Flow B production webhook URL → `.env` `N8N_RESULTS_WEBHOOK`, then restart the agent.

For acquisition, import `n8n/Flow_E_Acquisition.json`, then configure the webhook URL and point its HTTP node at `http://localhost:8200/acquire`.

### 5. Preflight

```powershell
python check_setup.py
```

Must print `RESULT: READY ✓`.

---

## Run

Three terminals (venv activated):

```powershell
# Terminal 1 — agent worker
python agent.py download-files   # first time only
python agent.py start

# Terminal 2 — SIP dispatcher
uvicorn dispatch:app --host 0.0.0.0 --port 8000

# Terminal 3 — lead collector (optional)
uvicorn lead_collector:app --host 0.0.0.0 --port 8100
```
Optional acquisition service:

```powershell
uvicorn lead_acquisition:app --host 0.0.0.0 --port 8200
```
---

## Test

**Manual SIP dial:**

```powershell
curl -X POST http://localhost:8000/call `
  -H "Content-Type: application/json" `
  -d '{"phone":"+1YOURPHONE","name":"Test","email":"you@example.com","lead_id":"LD-TEST01"}'
```

Your phone rings → AI speaks → hang up → check `calls.log` and the sheet (via Flow B).

**Add a lead:**

```powershell
curl -X POST http://localhost:8100/leads `
  -H "Content-Type: application/json" `
  -d '{"name":"Demo","phone":"4155551234","company":"Acme","email":"demo@test.com"}'
```

Flow A picks up the new row within ~1 minute.

**Rehearse without real bookings:** set `DRY_RUN=true` in `.env`, restart agent.

---

## Call lifecycle

```
New Lead → Calling → (phone rings) → Booked / Declined / No Answer / Voicemail / Hung up
                ↑                              │
                └──────── Retry / Follow-up ◄──┘  (Flow D or manual)
```

Flow B writes: `Call Status`, `Transcript`, `Notes`, booking details.

---

## Compliance (US)

The agent discloses it is an AI in the first sentence. Use for invited callbacks or opted-in leads. Scrub DNC before campaigns. Two-party-consent states require recording disclosure.
