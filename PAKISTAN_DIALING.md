# Pakistan (+92) outbound dialing

International calls to Pakistan are **carrier configuration**, not application code. The dialer already accepts `+92` E.164 numbers without forcing a US `+1` prefix.

## Code paths (already +92-safe)

- **`schemas/calls.py` `CallRequest._phone_e164`** — if the input starts with `+`, digits are kept as-is (e.g. `+923342092504`).
- **n8n Flow A — Prepare Lead** — same rule: numbers starting with `+` are normalized without adding `+1`. Timezone mapping includes `'92': 'Asia/Karachi'`.

Use full E.164 in the sheet, e.g. `+923342092504`, not `03342092504`.

## Required Telnyx setup

1. **Outbound Voice Profile**
   - Enable **International** calling.
   - Enable the **Pakistan** zone / destination.

2. **SIP Connection**
   - Create (or reuse) a SIP Connection with username/password credentials.
   - Attach that SIP Connection to the Outbound Voice Profile above.

3. **LiveKit outbound trunk**
   - Point the trunk at Telnyx: `address=sip.telnyx.com`
   - Set `auth_username` / `auth_password` to the **SIP Connection credentials**, not the Telnyx API key.
   - Do **not** put Telnyx API keys in `.env` — they are not read by this project.

Verify with the LiveKit CLI:

```powershell
.\lk.exe sip outbound list
```

Confirm for your trunk:

- `address` = `sip.telnyx.com`
- `auth_username` is non-empty (SIP credential username)

## Caller ID

`SIP_CALLER_ID` in `.env` must be a number Telnyx allows for international outbound (often a verified US DID). A US caller ID dialing `+92…` is normal for Telnyx international routes.

## Debugging failed dials

Set in `.env`:

```
SIP_WAIT_FOR_ANSWER=true
```

Restart the backend. Failed SIP attempts return `sip_error_code` and `sip_error_message` in the `/call` response instead of a generic `dispatched` status.
