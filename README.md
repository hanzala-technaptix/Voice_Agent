# Technaptix Voice Agent

Outbound AI voice agent that automates outbound sales calls using LiveKit SIP, Deepgram, Groq, Google Sheets, n8n, and Cal.com.

**Workflow:** Leads in Google Sheets → SIP phone call → LiveKit AI conversation → Cal.com booking → Transcript and call outcome written back to Google Sheets.

**No browser links. No Retell.** Direct PSTN calling through LiveKit SIP.

---

# Architecture

```text
Google Sheet (Lead)
       │
       ▼
  n8n Flow A ──POST /call──► main.py (FastAPI) ──SIP──► Prospect's phone
       │                            │
       │                            ▼
       │                  agent/worker.py (Groq + Deepgram)
       │                            │
       │                    Cal.com booking
       │                            │
       └◄──── n8n Flow B ◄── Transcript + Outcome Webhook
```

| Layer      | Component                           | Port |
| ---------- | ----------------------------------- | ---- |
| Backend    | `main.py` + `routes/`               | 8000 |
| Voice AI   | `agent/worker.py`                   | —    |
| Automation | n8n Flows A, B, D                   | —    |

Flow C (`POST /leads`) is deferred until the lead collector service is restored.

---

# Repository Structure

| Path                          | Purpose                                                               |
| ----------------------------- | --------------------------------------------------------------------- |
| `main.py`                     | FastAPI app entry — wires routes only                                 |
| `routes/`                     | HTTP endpoints (`/call`, `/health`)                                  |
| `schemas/`                    | Pydantic request/response models                                      |
| `services/`                   | Business logic (LiveKit dispatch, etc.)                               |
| `core/`                       | Configuration and shared exceptions                                   |
| `agent/worker.py`             | LiveKit voice agent (conversation, tools, transcript, email, booking) |
| `agent/prompts.py`            | System prompt and sales script                                        |
| `post_call/`                  | Transcript, outcome classification, follow-up email                   |
| `integrations/calcom.py`      | Cal.com integration                                                   |
| `scripts/check_setup.py`      | Environment validation and preflight checks                           |
| `config/livekit/outbound-trunk.json` | LiveKit SIP trunk template                                     |
| `config/n8n/Flow_A_Dialer.json`      | Google Sheets → Dial call                                      |
| `config/n8n/Flow_B_Results.json`     | Store transcript and outcome                                   |
| `config/n8n/Flow_C_Lead_Intake.json` | Optional public lead intake (inactive until leads API restored) |
| `config/n8n/Flow_D_Recovery.json`    | Retry failed or stuck calls                                    |

---

# Prerequisites

* Python 3.11+
* LiveKit Cloud project
* LiveKit SIP Outbound Trunk
* Deepgram API Key
* Groq API Key
* OpenAI API Key (if configured)
* Cal.com API Key
* Google Sheets
* Google Service Account
* n8n (Cloud or Self-hosted)

---

# Installation

## 1. Clone

```powershell
git clone <repository-url>
cd voice-agent
```

## 2. Create Virtual Environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

## 3. Install Dependencies

```powershell
pip install -r requirements.txt
```

## 4. Configure Environment

Create a `.env` file in the project root and configure all required values.

Configuration validation is handled automatically by `core/config.py`.

---

# LiveKit SIP Setup

Create an outbound SIP trunk once.

```bash
curl -sSL https://get.livekit.io/cli | bash

lk cloud auth

# Edit config/livekit/outbound-trunk.json

lk sip outbound create config/livekit/outbound-trunk.json
```

Copy the generated trunk ID into:

```text
SIP_OUTBOUND_TRUNK_ID=ST_xxxxxxxxx
```

---

# Google Sheets Setup

Create a worksheet named:

```text
Lead
```

Share the spreadsheet with your Google Service Account.

Place:

```text
service-account.json
```

inside the project root.

Required columns:

```text
Lead ID
Name
Company
Phone
Email
Source
Created At
Call Status
Call Outcome
Interested
Follow Up Required
Notes
Transcript
Call Attempt
Next Call At
Last Updated
```

Rows with one of these statuses will be dialed:

* New Lead
* Call
* Retry
* Follow-up

---

# n8n Setup

Import the workflows inside the `config/n8n` folder.

| Workflow | Required | Purpose                                        |
| -------- | -------- | ---------------------------------------------- |
| Flow A   | Yes      | Starts outbound calls                          |
| Flow B   | Yes      | Receives transcripts and updates Google Sheets |
| Flow C   | Optional | Public lead intake endpoint                    |
| Flow D   | Yes      | Retry and recovery workflow                    |

Configure these n8n variables:

```text
LEAD_SHEET_ID
DISPATCHER_URL
OWNER_EMAIL
```

Copy the production webhook URL from Flow B into:

```text
N8N_RESULTS_WEBHOOK
```

Restart the agent after updating the webhook URL.

---

# Preflight Check

Run:

```powershell
python scripts/check_setup.py
```

Expected output:

```text
RESULT: READY ✓
```

---

# Running the System

Open two terminals.

## Terminal 1 — Voice Agent

```powershell
python -m agent.worker download-files
python -m agent.worker start
```

`download-files` is only required the first time.

---

## Terminal 2 — FastAPI Backend

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000
```

Or use the helper script:

```powershell
.\start-backend.ps1
```

---

# Testing

## Manual SIP Call

```powershell
curl -X POST http://localhost:8000/call `
  -H "Content-Type: application/json" `
  -d "{\"phone\":\"+1YOURPHONE\",\"name\":\"Test\",\"email\":\"you@example.com\",\"lead_id\":\"LD-TEST01\"}"
```

Expected result:

* Phone rings
* AI starts conversation
* Transcript generated
* Google Sheet updated through Flow B

---

## Add a Lead

Lead intake via `POST /leads` is deferred until the lead collector service is restored. For now, add leads directly to the Google Sheet or use n8n Flow A after manual sheet entry.

When restored, leads will be available at `POST http://localhost:8000/leads` on the same backend port.

---

## Dry Run

To disable real bookings:

```text
DRY_RUN=true
```

Restart the agent.

---

# Call Lifecycle

```text
New Lead
    │
    ▼
Calling
    │
    ▼
Phone Rings
    │
    ▼
Conversation
    │
    ▼
Booked
Declined
Voicemail
No Answer
Hung Up
    │
    ▼
Flow B Updates Google Sheet
```

Flow B records:

* Call Status
* Call Outcome
* Transcript
* Notes
* Booking Details
* Follow-up Information

---

# Logs

Production logs are written under the configured log directory and include:

* Call logs
* Transcript logs
* Latency logs
* Email logs

---

# Compliance

The AI identifies itself as an AI assistant at the beginning of every conversation.

Before running campaigns:

* Call only opted-in or invited leads.
* Honor Do Not Call (DNC) requirements.
* Follow applicable recording-consent laws.
* Verify compliance with local telemarketing regulations.

---

# Production Components

The production deployment consists of:

* LiveKit Cloud
* LiveKit SIP
* Groq LLM
* Deepgram STT/TTS
* Cal.com
* Google Sheets
* Google Service Account
* n8n
* FastAPI Dispatcher
* LiveKit Voice Agent
* Lead Collector
