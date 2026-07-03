"""Transcript flattening and post-call outcome classification."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("outbound-agent.transcript")

ALLOWED_OUTCOMES = frozenset(
    {"booked", "declined", "callback", "voicemail", "no_answer", "incomplete"}
)


def flatten_history(history: Any) -> str:
    """Turn session.history into a readable Agent/Prospect dialogue string."""
    if history is None:
        return ""
    try:
        data = history.to_dict() if hasattr(history, "to_dict") else history
    except Exception:  # noqa: BLE001
        return ""

    items = data.get("items") or data.get("messages") or []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = (item.get("role") or item.get("type") or "").lower()
        content = item.get("content") or item.get("text") or ""
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("content") or ""))
                else:
                    parts.append(str(block))
            content = " ".join(p for p in parts if p)
        content = str(content).strip()
        if not content:
            continue
        label = "Prospect" if role in ("user", "human") else "Agent"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


async def classify_outcome(
    transcript_text: str,
    *,
    already_booked: bool,
    spoke_with_human: bool,
    duration_sec: float | None,
) -> tuple[str, str, bool, bool]:
    """
    Return (outcome, summary, interested, follow_up_required).
    Uses rules first, then a single lightweight LLM call when needed.
    """
    if already_booked:
        return "booked", "Meeting booked during the call.", True, False

    if not spoke_with_human:
        if duration_sec is not None and duration_sec < 3:
            return "no_answer", "Call not answered.", False, True
        return "no_answer", "Prospect did not answer or connect.", False, True

    text = (transcript_text or "").strip()
    if not text:
        return "incomplete", "Call ended with no recorded conversation.", False, False

    lower = text.lower()
    if "voicemail" in lower or "leave a message" in lower or "after the tone" in lower:
        return "voicemail", "Reached voicemail; left a brief message.", False, True
    if "do not call" in lower or "remove me" in lower or "stop calling" in lower:
        return "declined", "Prospect requested removal from call list.", False, False

    if not settings.openai_api_key:
        return "incomplete", text[:280], False, False

    prompt = (
        "Classify this outbound sales phone call. Reply with JSON only:\n"
        '{"outcome":"booked|declined|callback|voicemail|no_answer|incomplete",'
        '"summary":"1-2 sentences","interested":true|false,"follow_up_required":true|false}\n\n'
        f"TRANSCRIPT:\n{text[:12000]}"
    )
    try:
        async with httpx.AsyncClient(timeout=settings.outcome_classifier_timeout_sec) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.outcome_classifier_model,
                    "temperature": settings.outcome_classifier_temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)
            outcome = str(parsed.get("outcome", "incomplete")).lower()
            if outcome not in ALLOWED_OUTCOMES:
                outcome = "incomplete"
            return (
                outcome,
                str(parsed.get("summary") or "")[:500],
                bool(parsed.get("interested")),
                bool(parsed.get("follow_up_required")),
            )
    except Exception:  # noqa: BLE001
        logger.exception("outcome classification failed")
        return "incomplete", text[:280], False, False
