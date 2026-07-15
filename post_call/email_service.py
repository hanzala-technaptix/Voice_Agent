"""
Post-call follow-up email service — fully isolated subsystem.

Contract with the voice agent (agent.py):
  - agent.py calls exactly ONE public coroutine: process_followup_email().
  - It is invoked ONLY from report_results(), i.e. inside the LiveKit shutdown
    callback, strictly AFTER the conversation has ended and AFTER calls.log +
    the n8n webhook have completed. Nothing here can run during a live call.
  - This function NEVER raises: every failure path logs a warning and returns.
    A total email outage must not affect the worker, shutdown, reporting,
    booking, or the voice pipeline in any way.
  - Feature is config-gated (EMAIL_ENABLED, default false). With the default
    configuration this module is a no-op and existing deployments behave
    byte-identically.

Future n8n migration path: replace the body of process_followup_email() with a
webhook POST (or simply flip EMAIL_ENABLED=false and let n8n consume the
existing results webhook). agent.py needs no changes either way.

SMTP notes:
  - Designed for Gmail SMTP (smtp.gmail.com:587 + STARTTLS + App Password),
    but nothing Gmail-specific is hardcoded — any STARTTLS SMTP server works.
  - smtplib is blocking, so the send runs in a worker thread via
    asyncio.to_thread(); the event loop is never blocked.
  - The `with smtplib.SMTP(...)` context manager guarantees QUIT + socket
    close on success and on every failure path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from core.config import settings
from post_call.email_templates import RenderedEmail, render_followup

logger = logging.getLogger("outbound-agent.email")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Retry ONLY transient transport problems. Auth failures, refused recipients,
# malformed messages and other permanent errors are never retried.
_TRANSIENT_ERRORS = (
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPHeloError,
    TimeoutError,        # covers socket.timeout (alias since 3.10)
    ConnectionError,     # refused / reset / aborted
)
_PERMANENT_SMTP_ERRORS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPDataError,
    smtplib.SMTPNotSupportedError,
)


@dataclass(frozen=True)
class _SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    from_email: str
    from_name: str
    timeout_sec: float
    max_retries: int


def _smtp_config() -> _SmtpConfig:
    return _SmtpConfig(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_use_tls,
        from_email=settings.from_email,
        from_name=settings.from_name,
        timeout_sec=settings.email_timeout_sec,
        max_retries=settings.email_max_retries,
    )


def is_configured() -> bool:
    """True only when the feature is enabled AND every credential is present."""
    cfg = _smtp_config()
    return bool(
        settings.email_enabled
        and cfg.host
        and cfg.port
        and cfg.username
        and cfg.password
        and cfg.from_email
    )


def validate_startup_config() -> None:
    """Log ONE clear status line for the follow-up email subsystem at process
    startup (called once from agent.py at import time — never per-call).

    Silent when EMAIL_ENABLED=false (nothing to warn about, no log spam).
    When EMAIL_ENABLED=true, logs a single INFO line if fully configured, or
    a single WARNING block naming exactly which credential is missing.
    """
    if not settings.email_enabled:
        return
    cfg = _smtp_config()
    checks = [
        ("SMTP HOST", bool(cfg.host)),
        ("USERNAME", bool(cfg.username)),
        ("PASSWORD", bool(cfg.password)),
        ("FROM_EMAIL", bool(cfg.from_email)),
    ]
    if all(ok for _, ok in checks):
        logger.info(
            "EMAIL ENABLED — SMTP fully configured (host=%s, from=%s). "
            "Follow-up emails are ENABLED.",
            cfg.host, cfg.from_email,
        )
        return
    lines = ["EMAIL ENABLED"] + [
        f"{name}: {'OK' if ok else 'MISSING'}" for name, ok in checks
    ] + ["", "Follow-up emails are DISABLED."]
    logger.warning("\n".join(lines))


def validate_email(address: str | None) -> bool:
    """Reject None/empty/malformed addresses before any SMTP work happens."""
    if not address or not isinstance(address, str):
        return False
    return bool(_EMAIL_RE.match(address.strip()))


def _build_message(rendered: RenderedEmail, to_email: str, cfg: _SmtpConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name or cfg.from_email, cfg.from_email))
    msg["To"] = to_email
    msg["Subject"] = rendered.subject
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    return msg


def _send_sync(msg: EmailMessage, cfg: _SmtpConfig) -> None:
    """Blocking SMTP delivery — always executed via asyncio.to_thread()."""
    with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_sec) as smtp:
        smtp.ehlo()
        if cfg.use_tls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(cfg.username, cfg.password)
        smtp.send_message(msg)
    # context manager guarantees QUIT + close on all paths


async def _send_with_retry(msg: EmailMessage, to_email: str, cfg: _SmtpConfig) -> bool:
    """Deliver with exponential backoff on transient errors only. Never raises."""
    attempts = max(1, cfg.max_retries)
    for attempt in range(1, attempts + 1):
        logger.info("followup email: attempt %d/%d to=%s", attempt, attempts, to_email)
        try:
            await asyncio.to_thread(_send_sync, msg, cfg)
            return True
        except _PERMANENT_SMTP_ERRORS as e:
            # Auth/recipient/sender/data problems will not heal on retry.
            logger.warning(
                "followup email: permanent SMTP failure (%s) — not retrying",
                type(e).__name__,
            )
            return False
        except _TRANSIENT_ERRORS as e:
            logger.warning(
                "followup email: transient failure on attempt %d/%d (%s: %s)",
                attempt, attempts, type(e).__name__, e,
            )
            if attempt < attempts:
                await asyncio.sleep(2 ** (attempt - 1))
        except Exception as e:  # noqa: BLE001 — unknown ⇒ treat as permanent
            logger.warning(
                "followup email: unexpected failure (%s: %s) — not retrying",
                type(e).__name__, e,
            )
            return False
    return False


async def process_followup_email(*, outcome: dict, lead: dict) -> None:
    """Single public entry point, called from report_results() after the call.

    Gates internally on followup_requested + followup_email + EMAIL_ENABLED and
    returns immediately when any gate fails. Never raises.
    """
    try:
        if not outcome.get("followup_requested"):
            return  # nothing was promised on this call — stay silent

        to_email = outcome.get("followup_email")

        if not settings.email_enabled:
            # A follow-up WAS promised on the call but the sender is off —
            # surface it loudly so the promise isn't silently dropped.
            logger.warning(
                "followup email: requested for %s but EMAIL_ENABLED=false — NOT sent",
                to_email,
            )
            return
        if not is_configured():
            logger.warning(
                "followup email: requested for %s but SMTP is not fully configured "
                "(need SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD/FROM_EMAIL) — NOT sent",
                to_email,
            )
            return
        if not validate_email(to_email):
            logger.warning(
                "followup email: invalid recipient %r — rejected before SMTP", to_email
            )
            return

        assert isinstance(to_email, str)
        to_email = to_email.strip()
        followup_type = outcome.get("followup_type") or "busy"
        # product_interest is a future field — absent today, so this resolves
        # to the "general" template. Template choice is never keyed on
        # followup_type (see email_templates.select_template).
        rendered = render_followup(
            product_interest=outcome.get("product_interest"),
            followup_type=followup_type,
            lead_name=str(lead.get("name") or ""),
            lead_company=str(lead.get("company") or ""),
        )
        logger.info(
            "EMAIL_SEND_STARTED to=%s type=%s template=%s subject=%r",
            to_email, followup_type, rendered.template, rendered.subject,
        )

        cfg = _smtp_config()
        sent = await _send_with_retry(_build_message(rendered, to_email, cfg), to_email, cfg)
        if sent:
            logger.info(
                "EMAIL_SEND_SUCCESS to=%s template=%s", to_email, rendered.template
            )
        else:
            logger.warning(
                "EMAIL_SEND_FAILED to=%s template=%s — no retries left "
                "or permanent error (see warnings above)",
                to_email, rendered.template,
            )
    except Exception:  # noqa: BLE001 — absolute isolation: never propagate
        logger.warning("followup email: unexpected internal error", exc_info=True)


# NOTE: validate_startup_config() is intentionally NOT called here at module
# import time. It fires only once, from agent.py, AFTER the logging system
# (including the logs/email.log FileHandler) has been fully configured — see
# the comment above that call site. Calling it here as well would run before
# any handler exists, silently losing the message and logging it twice.