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
  n8n Flow A ──POST /call──► dispatch.py ──SIP──► Prospect's phone
       │                            │
       │                            ▼
       │                  agent.py (Groq + Deepgram)
       │                            │
       │                    Cal.com booking
       │                            │
       └◄──── n8n Flow B ◄── Transcript + Outcome Webhook
```

| Layer      | Component                           | Port |
| ---------- | ----------------------------------- | ---- |
| CRM        | Google Sheets + `leads/lead_collector.py` | 8100 |
| Dialer     | `dispatch.py`                       | 8000 |
| Voice AI   | `agent.py`                          | —    |
| Automation | n8n Flows A, B, D                   | —    |

---

# Repository Structure

| Path                          | Purpose                                                               |
| ----------------------------- | --------------------------------------------------------------------- |
| `agent.py`                    | LiveKit voice agent (conversation, tools, transcript, email, booking) |
| `dispatch.py`                 | Creates LiveKit room, dispatches agent, initiates SIP calls           |
| `leads/lead_collector.py`     | Imports leads into Google Sheets                                      |
| `config.py`                   | Loads and validates environment variables                             |
| `prompts.py`                  | System prompt and sales script                                        |
| `transcript_utils.py`         | Transcript formatting and outcome classification                      |
| `email_service.py`            | Sends follow-up emails                                                |
| `email_templates.py`          | Email templates                                                       |
| `tools/calcom.py`             | Cal.com integration                                                   |
| `check_setup.py`              | Environment validation and preflight checks                           |
| `livekit/outbound-trunk.json` | LiveKit SIP trunk template                                            |
| `n8n/Flow_A_Dialer.json`      | Google Sheets → Dial call                                             |
| `n8n/Flow_B_Results.json`     | Store transcript and outcome                                          |
| `n8n/Flow_C_Lead_Intake.json` | Optional public lead intake                                           |
| `n8n/Flow_D_Recovery.json`    | Retry failed or stuck calls                                           |

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

Configuration validation is handled automatically by `config.py`.

---

# LiveKit SIP Setup

Create an outbound SIP trunk once.

```bash
curl -sSL https://get.livekit.io/cli | bash

lk cloud auth

# Edit livekit/outbound-trunk.json

lk sip outbound create livekit/outbound-trunk.json
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

Import the workflows inside the `n8n` folder.

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
python check_setup.py
```

Expected output:

```text
RESULT: READY ✓
```

---

# Running the System

Open three terminals.

## Terminal 1 — Voice Agent

```powershell
python agent.py download-files
python agent.py start
```

`download-files` is only required the first time.

---

## Terminal 2 — SIP Dispatcher

```powershell
uvicorn dispatch:app --host 0.0.0.0 --port 8000
```

---

## Terminal 3 — Lead Collector (Optional)

```powershell
uvicorn leads.lead_collector:app --host 0.0.0.0 --port 8100
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

```powershell
curl -X POST http://localhost:8100/leads `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"Demo\",\"phone\":\"4155551234\",\"company\":\"Acme\",\"email\":\"demo@test.com\"}"
```

Flow A automatically places the outbound call.

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
