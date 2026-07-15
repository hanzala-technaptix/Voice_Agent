"""
Follow-up email templates — content only, zero transport logic.

Design contract (keep future migration cheap):
  - email_service.py is the ONLY consumer of this module. agent.py never
    imports it, so templates can change (or move into n8n / a RAG pipeline)
    without touching the voice agent.
  - Template selection is keyed by PRODUCT INTEREST, not by followup_type.
    followup_type ("busy" | "info_request") only tunes the opening line of
    whichever template was selected. Today product interest is never captured
    on calls, so every send resolves to "general" — the product templates
    below exist so a future product_interest field can activate them without
    any change to agent.py or email_service.py's public API.
  - Adding a product = write one renderer + register it in TEMPLATES.
  - Plain HTML + text only. No attachments, no external images, no tracking
    pixels, no scheduling links, no pricing (per sales-call policy).
"""

from __future__ import annotations

from dataclasses import dataclass

COMPANY = "Technaptix"
WEBSITE = "technaptix.com"
CONTACT_EMAIL = "info@technaptix.com"


@dataclass(frozen=True)
class RenderedEmail:
    template: str
    subject: str
    html: str
    text: str


def _opening(followup_type: str, lead_name: str) -> tuple[str, str]:
    """Return (subject, first paragraph) tuned by why we're writing."""
    name = lead_name.strip() or "there"
    if followup_type == "info_request":
        return (
            f"The information you asked for — {COMPANY}",
            f"Hi {name}, thanks for your interest on our call today. As promised, "
            "here's a quick introduction you can read in under a minute.",
        )
    # "busy" (default): they couldn't talk.
    return (
        f"Sorry we caught you at a busy moment — quick intro to {COMPANY}",
        f"Hi {name}, thanks for taking my call earlier — I know the timing "
        "wasn't great, so here's the 60-second version instead.",
    )


def _wrap_html(subject: str, paragraphs: list[str]) -> str:
    body = "\n".join(
        f'<p style="margin:0 0 14px 0;">{p}</p>' for p in paragraphs
    )
    return f"""\
<!DOCTYPE html>
<html>
  <body style="margin:0;padding:24px;background:#f6f7f9;">
    <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:8px;
                padding:28px;font-family:Arial,Helvetica,sans-serif;font-size:15px;
                line-height:1.55;color:#222;">
      <h2 style="margin:0 0 18px 0;font-size:18px;color:#111;">{subject}</h2>
      {body}
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:22px 0;" />
      <p style="margin:0;font-size:13px;color:#666;">
        {COMPANY} &middot; <a href="https://{WEBSITE}" style="color:#2563eb;">{WEBSITE}</a>
        &middot; <a href="mailto:{CONTACT_EMAIL}" style="color:#2563eb;">{CONTACT_EMAIL}</a><br />
        If you'd rather not hear from us, just reply and let us know.
      </p>
    </div>
  </body>
</html>
"""


def _render_general(lead_name: str, lead_company: str, followup_type: str) -> RenderedEmail:
    subject, opener = _opening(followup_type, lead_name)
    company_line = f" at {lead_company}" if lead_company.strip() else ""
    paragraphs = [
        opener,
        f"{COMPANY} builds AI assistants that plug into the systems your team"
        f"{company_line} already uses:",
        "<strong>Intellyca</strong> — ask your ERP or business data questions in "
        "plain English and get instant answers, instead of waiting on reports.",
        "<strong>Invoyser</strong> — an AI receivables agent that chases invoices "
        "automatically and shortens your collection cycle. Live in 2–3 days.",
        "<strong>Custom AI</strong> — tailored automation and document intelligence "
        "for high-volume manual workflows in finance, manufacturing, retail and logistics.",
        "If any of that sounds useful, just reply to this email and we'll take it "
        "from there — no pressure either way.",
        "Warm regards,<br />Maria — Sales, " + COMPANY,
    ]
    text = (
        f"{opener.replace('<br />', chr(10))}\n\n"
        f"{COMPANY} builds AI assistants that plug into the systems your team{company_line} already uses:\n\n"
        "  - Intellyca — ask your ERP or business data questions in plain English "
        "and get instant answers, instead of waiting on reports.\n"
        "  - Invoyser — an AI receivables agent that chases invoices automatically "
        "and shortens your collection cycle. Live in 2-3 days.\n"
        "  - Custom AI — tailored automation and document intelligence for "
        "high-volume manual workflows.\n\n"
        "If any of that sounds useful, just reply to this email and we'll take it "
        "from there — no pressure either way.\n\n"
        f"Warm regards,\nMaria — Sales, {COMPANY}\n"
        f"{WEBSITE} | {CONTACT_EMAIL}\n\n"
        "If you'd rather not hear from us, just reply and let us know.\n"
    )
    return RenderedEmail("general", subject, _wrap_html(subject, paragraphs), text)


def _render_intellyca(lead_name: str, lead_company: str, followup_type: str) -> RenderedEmail:
    subject, opener = _opening(followup_type, lead_name)
    subject = f"Intellyca — instant answers from your business data | {COMPANY}"
    paragraphs = [
        opener,
        "You mentioned reporting, so here's the short version of <strong>Intellyca</strong>: "
        "it sits on top of your ERP/BI stack (SAP-ready) and lets anyone ask questions "
        "in plain English — new KPIs and answers in seconds instead of days of "
        "developer back-and-forth.",
        "If that matches the pain you described, reply to this email and we'll show "
        "you a 15-minute walkthrough with your kind of data.",
        "Warm regards,<br />Maria — Sales, " + COMPANY,
    ]
    text = (
        f"{opener}\n\n"
        "You mentioned reporting, so here's the short version of Intellyca: it sits "
        "on top of your ERP/BI stack (SAP-ready) and lets anyone ask questions in "
        "plain English — new KPIs and answers in seconds instead of days of "
        "developer back-and-forth.\n\n"
        "If that matches the pain you described, reply to this email and we'll show "
        "you a 15-minute walkthrough with your kind of data.\n\n"
        f"Warm regards,\nMaria — Sales, {COMPANY}\n{WEBSITE} | {CONTACT_EMAIL}\n"
    )
    return RenderedEmail("intellyca", subject, _wrap_html(subject, paragraphs), text)


def _render_invoyser(lead_name: str, lead_company: str, followup_type: str) -> RenderedEmail:
    subject, opener = _opening(followup_type, lead_name)
    subject = f"Invoyser — collect receivables faster | {COMPANY}"
    paragraphs = [
        opener,
        "Since collections came up, here's <strong>Invoyser</strong> in one line: an AI "
        "receivables agent that follows up on invoices automatically, cuts DSO, and "
        "goes live in 2–3 days without changing your ERP.",
        "Reply to this email if you'd like to see how it would run on your ledger.",
        "Warm regards,<br />Maria — Sales, " + COMPANY,
    ]
    text = (
        f"{opener}\n\n"
        "Since collections came up, here's Invoyser in one line: an AI receivables "
        "agent that follows up on invoices automatically, cuts DSO, and goes live "
        "in 2-3 days without changing your ERP.\n\n"
        "Reply to this email if you'd like to see how it would run on your ledger.\n\n"
        f"Warm regards,\nMaria — Sales, {COMPANY}\n{WEBSITE} | {CONTACT_EMAIL}\n"
    )
    return RenderedEmail("invoyser", subject, _wrap_html(subject, paragraphs), text)


def _render_company(lead_name: str, lead_company: str, followup_type: str) -> RenderedEmail:
    # Company-overview variant — same body as general but framed as an intro
    # for prospects who asked "who are you?" rather than about a product.
    rendered = _render_general(lead_name, lead_company, followup_type)
    return RenderedEmail("company", rendered.subject, rendered.html, rendered.text)


# Registry — adding a future product template is one entry here, nothing else.
TEMPLATES = {
    "general": _render_general,
    "company": _render_company,
    "intellyca": _render_intellyca,
    "invoyser": _render_invoyser,
}


def select_template(product_interest: str | None) -> str:
    """Map a (future) product-interest signal to a template key.

    Deliberately NOT keyed on followup_type: busy-vs-info only changes the
    opening line, never which product story we tell. Unknown/absent interest
    falls back to the general company intro.
    """
    key = (product_interest or "").strip().lower()
    return key if key in TEMPLATES else "general"


def render_followup(
    *,
    product_interest: str | None,
    followup_type: str,
    lead_name: str,
    lead_company: str,
) -> RenderedEmail:
    """Render the follow-up email. Pure function: no I/O, no settings access."""
    key = select_template(product_interest)
    return TEMPLATES[key](lead_name or "", lead_company or "", followup_type or "busy")
