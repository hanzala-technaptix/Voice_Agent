"""
Technaptix Voice Agent — SIP/PSTN outbound (direct phone dial)

LiveKit Agents worker. Waits for a dispatch (created by dispatch.py), dials the
prospect via SIP, runs the conversation when they answer, books meetings via
Cal.com mid-call, and reports the outcome + full transcript to n8n Flow B.

Run:
    python agent.py download-files
    python agent.py dev
    python agent.py start

All runtime configuration lives in .env — see config.py for the full variable list.
"""

import asyncio
import json
import logging
import os
import re as _re
import time
from datetime import datetime, timezone

import httpx

from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RunContext,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
    llm,
    metrics,
)
from livekit.agents.worker import ServerEnvOption
from livekit.plugins import openai, silero
from livekit.plugins import deepgram
from livekit.plugins import groq
from config import ConfigurationError, settings
from prompts import CALLER_NAME, COMPANY, build_instructions
from tools import calcom
from transcript_utils import classify_outcome, flatten_history

logger = logging.getLogger("outbound-agent")
logging.basicConfig(level=logging.INFO)
logger.info("agent.py build: %s", settings.agent_build)


def _build_turn_handling() -> TurnHandlingOptions:
    """Central turn-taking config — all values from .env via config.settings."""
    return TurnHandlingOptions(
        endpointing={
            "mode": settings.endpointing_mode,
            "min_delay": settings.min_endpointing_delay_sec,
            "max_delay": settings.max_endpointing_delay_sec,
        },
        interruption={
            "enabled": settings.interruption_enabled,
            "resume_false_interruption": settings.resume_false_interruption,
            "false_interruption_timeout": settings.false_interruption_timeout_sec,
            "min_duration": settings.min_interruption_duration_sec,
            "min_words": settings.min_interruption_words,
            "backchannel_boundary": (
                settings.backchannel_boundary_start_sec,
                settings.backchannel_boundary_end_sec,
            ),
        },
        preemptive_generation={
            "enabled": settings.preemptive_generation,
            "preemptive_tts": settings.preemptive_tts,
            "max_speech_duration": settings.preemptive_max_speech_duration_sec,
            "max_retries": settings.preemptive_max_retries,
        },
    )


def _build_llm():
    """Build LLM from .env — Groq or OpenAI primary, optional OpenAI fallback."""
    instances: list[llm.LLM] = []
    cfg = settings

    if cfg.llm_backend == "groq":
        assert cfg.llm_model_groq is not None
        groq_kwargs: dict = {
            "model": cfg.llm_model_groq,
            "temperature": cfg.llm_temperature,
            "parallel_tool_calls": cfg.llm_parallel_tool_calls,
        }
        if cfg.groq_reasoning_effort is not None:
            groq_kwargs["reasoning_effort"] = cfg.groq_reasoning_effort
        logger.info("LLM primary: Groq %s", cfg.llm_model_groq)
        instances.append(groq.LLM(**groq_kwargs))
    elif cfg.llm_backend == "openai":
        logger.info("LLM primary: OpenAI %s", cfg.llm_model_openai)
        instances.append(
            openai.LLM(
                model=cfg.llm_model_openai,
                temperature=cfg.llm_temperature,
            )
        )

    if cfg.llm_fallback_enabled and cfg.llm_backend == "groq":
        logger.info("LLM fallback: OpenAI %s", cfg.llm_model_openai)
        instances.append(
            openai.LLM(
                model=cfg.llm_model_openai,
                temperature=cfg.llm_temperature,
            )
        )

    if len(instances) >= 2:
        return llm.FallbackAdapter(
            instances,
            attempt_timeout=cfg.llm_fallback_attempt_timeout_sec,
            max_retry_per_llm=cfg.llm_fallback_max_retry_per_llm,
            retry_on_chunk_sent=cfg.llm_fallback_retry_on_chunk_sent,
        )
    if len(instances) == 1:
        return instances[0]

    raise ConfigurationError(
        "No LLM instance could be built — check LLM_BACKEND and API keys in .env"
    )


def _build_groq_warmup_client():
    """Build a standalone, throwaway Groq LLM client used ONLY to pre-warm
    the DNS/TCP/TLS connection to Groq before the first real completion
    request. Deliberately separate from _build_llm(): this helper never
    creates OpenAI, never creates a FallbackAdapter, and its return value is
    never passed to AgentSession — its only job is connection warmup, closed
    again right after. Returns None when LLM_BACKEND isn't "groq" (primary
    isn't Groq, so there's nothing to warm).
    """
    cfg = settings
    if cfg.llm_backend != "groq":
        return None
    assert cfg.llm_model_groq is not None
    kwargs: dict = {
        "model": cfg.llm_model_groq,
        "temperature": cfg.llm_temperature,
        "parallel_tool_calls": cfg.llm_parallel_tool_calls,
    }
    if cfg.groq_reasoning_effort is not None:
        kwargs["reasoning_effort"] = cfg.groq_reasoning_effort
    return groq.LLM(**kwargs)


def _build_stt():
    """Build Deepgram STT — all params env-driven via config.settings."""
    cfg = settings
    if cfg.stt_backend == "deepgram":
        logger.info("STT: Deepgram %s (%s)", cfg.stt_model, cfg.stt_language)
        return deepgram.STT(
            model=cfg.stt_model,
            language=cfg.stt_language,
            interim_results=cfg.stt_interim_results,
            no_delay=cfg.stt_no_delay,
            smart_format=cfg.stt_smart_format,
            endpointing_ms=cfg.stt_endpointing_ms,
            filler_words=cfg.stt_filler_words,
            punctuate=cfg.stt_punctuate,
        )
    raise ConfigurationError(f"Unsupported STT_BACKEND: {cfg.stt_backend}")


def _build_tts():
    """Build TTS from .env — no silent provider fallback."""
    cfg = settings
    if cfg.tts_backend == "deepgram":
        assert cfg.tts_voice is not None
        logger.info("TTS: Deepgram %s", cfg.tts_voice)
        return deepgram.TTS(model=cfg.tts_voice)
    raise ConfigurationError(f"Unsupported TTS_BACKEND: {cfg.tts_backend}")

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class OutboundSalesAgent(Agent):
    def __init__(self, lead: dict, *, opening_already_spoken: bool = True):
        super().__init__(
            instructions=build_instructions(
                lead, opening_already_spoken=opening_already_spoken
            )
        )
        self.lead = lead
        self.outcome: dict = {
            "status": "incomplete",
            "booking": None,
            "summary": "",
            "interested": None,
            "follow_up_required": False,
            # Follow-up intro email (separate from Cal.com booking invite).
            # Populated only by capture_followup_email() when the prospect
            # is busy / requests information. n8n Flow B (post-call) reads
            # these keys to decide whether to send the intro email. Booking
            # short-circuits: if status=="booked", these stay at defaults
            # and no intro email fires (Cal.com already emailed the invite).
            "followup_requested": False,
            "followup_email": None,
            "followup_type": None,  # "busy" | "info_request"
        }
        self.call_started_at: datetime | None = None
        self.spoke_with_human = False
        self.turn_latencies: list[dict] = []
        self._call_ended = False
        self.fake_toolcall_detected: str | None = None

    @staticmethod
    def _start_filler(context: RunContext, line: str) -> asyncio.Task:
        """Speak a short filler line IMMEDIATELY so the prospect never hears
        dead air while a tool runs. The tool executes concurrently with the
        filler playing, so the filler hides the tool's latency rather than
        adding to it. Returns the say-task; the caller awaits it (via
        _finish_filler) before the tool result is spoken so speech doesn't
        overlap."""
        return asyncio.create_task(
            context.session.say(line, allow_interruptions=True)
        )

    @staticmethod
    async def _finish_filler(task: asyncio.Task | None) -> None:
        """Wait for the filler to finish playing (swallowing any error) so the
        next spoken line doesn't collide with it."""
        if task is None:
            return
        try:
            await task
        except Exception:  # noqa: BLE001
            pass

    @function_tool()
    async def get_available_slots(self, context: RunContext) -> str:
        """Fetch open meeting slots for the next few business days."""
        filler = self._start_filler(
            context, "Let me check the calendar for you — one moment."
        )
        try:
            slots = await calcom.get_available_slots(days_ahead=5, max_slots=4)
        except Exception as e:  # noqa: BLE001
            await self._finish_filler(filler)
            logger.exception("slot fetch failed")
            return f"Calendar lookup failed ({e}). Apologise and offer to email times instead."
        await self._finish_filler(filler)
        if not slots:
            return (
                "[TOOL RESULT — do not read aloud] No open slots in the next 5 days. "
                "Say naturally: 'I don't have any open slots this week, but let me have "
                "someone from the team reach out to you directly to find a time that works.'"
            )
        # Keep the ISO available so the model can pass it to book_meeting, but
        # force it to SPEAK the human label verbatim — no relative-date rewrites.
        lines = [f"- {s['label']}   (book with slot_start_iso={s['start']})" for s in slots]
        return (
            "[TOOL RESULT — private, do not read this bracketed note aloud] "
            "Offer the prospect at most TWO of these options. When you say a time, "
            "read its label EXACTLY as written — do NOT convert it to 'tomorrow', "
            "'the day after', or any relative wording, and do NOT change the day, "
            "date, or time. When the prospect picks one, call book_meeting with that "
            "option's slot_start_iso value exactly.\n"
            + "\n".join(lines)
        )

    @function_tool()
    async def book_meeting(
        self,
        context: RunContext,
        slot_start_iso: str,
        attendee_email: str,
        attendee_name: str,
    ) -> str:
        """Book the meeting once the prospect AGREES to a specific slot.
        IMPORTANT: Do NOT speak the result of this function verbatim.
        The result is a private instruction for you only.
        Say a natural confirmation in your own words instead."""
        filler = self._start_filler(
            context, "Perfect — give me just a second to lock that in."
        )
        try:
            booking = await calcom.book_meeting(
                start_iso=slot_start_iso,
                name=attendee_name,
                email=attendee_email,
                phone=self.lead.get("phone", ""),
                company=self.lead.get("company", ""),
            )
        except Exception as e:  # noqa: BLE001
            await self._finish_filler(filler)
            logger.exception("booking failed")
            return (
                f"[TOOL RESULT — do not read aloud] Booking failed. "
                f"Error: {e}. "
            "Say naturally: 'I'm sorry, I had a small technical issue there. "
            "Our team will follow up with you directly by email to confirm the time.' "
            "Then invoke end_call."
            )
        await self._finish_filler(filler)
        self.outcome["status"] = "booked"
        self.outcome["booking"] = booking
        self.outcome["interested"] = True
        self.outcome["follow_up_required"] = False
        self.outcome["summary"] = f"Booked demo for {booking['start_label']}."
        return (
            f"[TOOL RESULT — do not read aloud] "
            f"Booking confirmed for {booking['start_label']}. "
            "Now say naturally to the prospect: "
            f"'Perfect, you're all set for {booking['start_label']}. "
            "You'll get a calendar invite with a Google Meet link in your inbox shortly. "
            "Really appreciate your time — have a great day!' "
            "Then invoke end_call."
        )

    # ------------------------------------------------------------------
    # Follow-up intro email (separate from Cal.com booking invite)
    # ------------------------------------------------------------------
    # This tool ONLY stores a confirmed email + intent on self.outcome.
    # No network I/O. No sending. n8n Flow B (post-call) is the sender —
    # it already receives the outcome payload via report_results, so the
    # send happens async, after end_call, off the voice path.
    #
    # Guardrails:
    #   - Refuses if the call already booked → Cal.com sent the invite.
    #   - Validates email format with a strict-enough regex before storing.
    #   - Returns a private [TOOL RESULT] telling the model what to say
    #     next in each branch (stored / bad-format / already-booked).
    _EMAIL_RE = _re.compile(
        r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
    )

    @function_tool()
    async def capture_followup_email(
        self,
        context: RunContext,
        email: str,
        reason: str,
    ) -> str:
        """Store a confirmed email address for a post-call follow-up intro
        (used ONLY when the prospect is busy or asks for information —
        NEVER for a meeting booking, which is handled by book_meeting).

        Args:
            email: The prospect's confirmed email address (must be validated
                and read back to them BEFORE calling this tool).
            reason: Why we're sending it — must be either "busy" (they
                couldn't talk) or "info_request" (they asked for information).
        """
        # Never send a follow-up if we already booked — Cal.com emailed them.
        if self.outcome.get("status") == "booked":
            logger.info("followup email: refused (already booked)")
            return (
                "[TOOL RESULT — do not read aloud] Follow-up email is not "
                "needed because a meeting was already booked (the calendar "
                "invite covers the introduction). Continue the current wrap-up "
                "and invoke end_call as planned."
            )

        clean = (email or "").strip().strip(".").strip()
        if not clean or not self._EMAIL_RE.match(clean):
            logger.info("followup email: invalid format %r", clean[:80])
            return (
                "[TOOL RESULT — do not read aloud] The email address didn't "
                "parse cleanly. Politely ask them to spell it once more, "
                "letter by letter, then re-invoke capture_followup_email "
                "with the corrected address. If it fails a second time, "
                "thank them warmly, say the team will follow up, and invoke "
                "end_call — do NOT promise an email in that case."
            )

        followup_type = "info_request" if reason == "info_request" else "busy"
        self.outcome["followup_requested"] = True
        self.outcome["followup_email"] = clean
        self.outcome["followup_type"] = followup_type
        # Non-destructive: don't overwrite an existing booked summary.
        if not self.outcome.get("summary"):
            self.outcome["summary"] = (
                f"Follow-up intro email requested ({followup_type})."
            )
        self.outcome["follow_up_required"] = True
        logger.info(
            "followup email: stored email=%s type=%s (will be sent by n8n "
            "Flow B after end_call)",
            clean, followup_type,
        )
        return (
            "[TOOL RESULT — do not read aloud] Follow-up email address "
            f"stored ({clean}). Say naturally to the prospect: "
            f"'Perfect — I'll send that brief intro to {clean} right after "
            "we hang up. Thanks so much for your time, have a great day!' "
            "Then invoke end_call."
        )

    @function_tool()
    async def end_call(self, context: RunContext) -> str:
        """Hang up. Use after saying goodbye."""
        if self._call_ended:
            return "Call already ended."
        self._call_ended = True
        if self._goodbye_watch_task and not self._goodbye_watch_task.done():
            self._goodbye_watch_task.cancel()
        await context.wait_for_playout()
        job_ctx = get_job_context()
        try:
            await job_ctx.api.room.delete_room(api.DeleteRoomRequest(room=job_ctx.room.name))
        except Exception:  # noqa: BLE001
            logger.exception("delete_room raised on end_call; ignoring")
        return "Call ended."

    # ------------------------------------------------------------------
    # Goodbye fast-path — cue pattern + task handle only.
    # ------------------------------------------------------------------
    # Problem this solves: the model is *supposed* to call end_call right
    # after saying goodbye, but it sometimes keeps the turn open for a few
    # extra seconds even after the prospect has clearly said "bye"/"thanks,
    # bye". The actual watch/nudge logic lives in _dead_air_watchdog below,
    # reusing its already-proven session.history polling loop rather than
    # adding a second, unverified event hook. This does NOT call end_call
    # early on its own — it only shortens how long the agent waits before
    # being nudged to actually invoke the tool once a goodbye cue is heard.
    _GOODBYE_CUE_PATTERN = _re.compile(
        r'\b(bye|goodbye|good\s*bye|thanks?,?\s*bye|talk\s*(to\s*you\s*)?later|'
        r'that\'?s\s*all|gotta\s*go|have\s*to\s*go)\b',
        flags=_re.IGNORECASE,
    )
    _goodbye_watch_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

async def _watchdog(
    agent: OutboundSalesAgent,
    session: AgentSession,
    ctx: JobContext,
) -> None:
    """Gracefully end calls that exceed the maximum duration."""

    soft_limit = max(settings.max_call_duration_sec - settings.watchdog_soft_limit_buffer_sec, 30)

    try:
        await asyncio.sleep(soft_limit)

        # If the worker is already shutting down, do nothing.
        if ctx.is_shutting_down:
            return

        # Don't interrupt a successfully booked call.
        if agent.outcome["status"] == "booked":
            return

        logger.warning(
            "watchdog: call exceeded %ss, prompting graceful close",
            soft_limit,
        )

        try:
            session.generate_reply(
                instructions=(
                    "We are running out of time on this call. "
                    "Politely thank the prospect, say you'll have someone "
                    "from the team follow up by email, then invoke end_call."
                )
            )
        except RuntimeError:
            # The AgentSession has already stopped.
            logger.info("Watchdog: AgentSession already stopped.")
            return

    except asyncio.CancelledError:
        logger.debug("Watchdog cancelled.")
        return

    except Exception:
        logger.exception("Watchdog failed.")
        try:
            ctx.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dead-air watchdog
# ---------------------------------------------------------------------------

async def _dead_air_watchdog(
    agent: OutboundSalesAgent,
    session: AgentSession,
    ctx: JobContext,
    *,
    opening_line: str,
) -> None:
    """Detect the agent going silent — including never speaking at all — and
    recover or end gracefully.

    Covers two distinct failure modes seen in production: (1) the opener
    itself silently fails to play right after the prospect answers, leaving
    a connected call with zero spoken turns; (2) the prospect speaks mid-call
    and the agent's reply pipeline hangs with no retry. Both look the same
    from here: "the agent currently owes a turn and hasn't taken it."

    Deliberately polls session.history.to_dict() rather than hooking
    internal session events, since that structure is already proven to work
    elsewhere in this file (report_results uses it) and we haven't verified
    the exact event names on this livekit-agents version."""

    last_item_count = -1
    last_change_at = time.monotonic()
    recovery_attempts = 0

    try:
        while True:
            await asyncio.sleep(settings.dead_air_poll_sec)

            if ctx.is_shutting_down or agent._call_ended:
                return

            try:
                items = session.history.to_dict().get("items", [])
            except Exception:
                continue

            if len(items) != last_item_count:
                last_item_count = len(items)
                last_change_at = time.monotonic()

                # Goodbye fast-path: if the prospect's most recent turn was a
                # clear goodbye cue and the agent hasn't ended the call within
                # GOODBYE_NUDGE_TIMEOUT_SEC, nudge it to wrap up. Reuses this
                # loop's already-proven history polling rather than guessing
                # at an unverified event hook or constructor.
                if not agent._call_ended and not (
                    agent._goodbye_watch_task and not agent._goodbye_watch_task.done()
                ):
                    last_msg = None
                    for item in reversed(items):
                        if item.get("type") == "message":
                            last_msg = item
                            break
                    if last_msg and last_msg.get("role") == "user":
                        content = last_msg.get("content") or []
                        text = (
                            " ".join(c for c in content if isinstance(c, str))
                            if isinstance(content, list) else str(content)
                        )
                        if agent._GOODBYE_CUE_PATTERN.search(text or ""):

                            async def _nudge_if_still_open(
                                _agent=agent, _session=session
                            ) -> None:
                                try:
                                    await asyncio.sleep(settings.goodbye_nudge_timeout_sec)
                                    if _agent._call_ended:
                                        return
                                    logger.info(
                                        "goodbye fast-path: nudging agent to "
                                        "wrap up after %.1fs",
                                        settings.goodbye_nudge_timeout_sec,
                                    )
                                    _session.generate_reply(
                                        instructions=(
                                            "The prospect just said goodbye. "
                                            "Say a brief warm goodbye of your "
                                            "own RIGHT NOW in one short "
                                            "sentence, then immediately "
                                            "invoke end_call. Do not ask any "
                                            "further questions."
                                        )
                                    )
                                except asyncio.CancelledError:
                                    pass
                                except RuntimeError:
                                    logger.info(
                                        "goodbye nudge: session already stopped"
                                    )
                                except Exception:
                                    logger.exception("goodbye nudge failed")

                            agent._goodbye_watch_task = asyncio.create_task(
                                _nudge_if_still_open()
                            )
                continue  # conversation is still moving — nothing to do

            last_msg = None
            for item in reversed(items):
                if item.get("type") == "message":
                    last_msg = item
                    break

            # Agent owes a turn either if nothing's been said yet (the
            # opener never played) or the prospect just spoke and got no
            # reply. Either way it's the same dead-air condition.
            never_spoken = last_msg is None
            agent_owes_turn = never_spoken or last_msg.get("role") == "user"
            if not agent_owes_turn:
                continue

            elapsed = time.monotonic() - last_change_at
            if elapsed < settings.dead_air_timeout_sec:
                continue

            logger.warning(
                "dead-air watchdog: %.1fs of silence, never_spoken=%s "
                "(recovery attempt %d/%d)",
                elapsed, never_spoken, recovery_attempts + 1,
                settings.max_dead_air_recoveries,
            )

            if recovery_attempts < settings.max_dead_air_recoveries:
                recovery_attempts += 1
                try:
                    if never_spoken:
                        # Retry the actual opener, not a generic line — the
                        # AI disclosure still has to be the first thing said.
                        await session.say(opening_line, allow_interruptions=True)
                    else:
                        await session.say(
                            "Sorry about that, I think we had a brief "
                            "connection hiccup there — could you say that "
                            "again?",
                            allow_interruptions=True,
                        )
                except Exception:
                    logger.exception("dead-air recovery speech failed")
                last_change_at = time.monotonic()
                continue

            logger.warning(
                "dead-air watchdog: recovery exhausted, ending call gracefully"
            )
            try:
                await session.say(
                    "I'm really sorry — I'm having some technical trouble on "
                    "my end. I'll have someone from our team follow up with "
                    "you directly by email. Thanks for your patience, have a "
                    "great day!",
                    allow_interruptions=True,
                )
            except Exception:
                logger.exception("dead-air graceful goodbye failed")

            agent.outcome["status"] = "incomplete"
            if not agent.outcome.get("summary"):
                agent.outcome["summary"] = (
                    "Call ended automatically after the agent stopped "
                    "responding (dead-air watchdog)."
                )
            agent.outcome["follow_up_required"] = True
            agent._call_ended = True
            try:
                job_ctx = get_job_context()
                await job_ctx.api.room.delete_room(
                    api.DeleteRoomRequest(room=job_ctx.room.name)
                )
            except Exception:
                logger.exception("dead-air watchdog: delete_room failed")
            return

    except asyncio.CancelledError:
        logger.debug("Dead-air watchdog cancelled.")
        return
    except Exception:
        logger.exception("Dead-air watchdog crashed.")

# ---------------------------------------------------------------------------
# Calls log
# ---------------------------------------------------------------------------

def _write_calls_log(payload: dict) -> None:
    transcript_text = _sanitize_transcript(payload.get("transcript_text") or "")
    try:
        with open(settings.calls_log_path, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "lead_id": payload.get("lead", {}).get("lead_id"),
                "phone": payload.get("lead", {}).get("phone"),
                "outcome": payload.get("outcome"),
                "interested": payload.get("interested"),
                "follow_up_required": payload.get("follow_up_required"),
                "duration_sec": payload.get("duration_sec"),
                "summary": payload.get("summary"),
                "transcript_len": len(transcript_text),
                "transcript": transcript_text,
                "transcript_turns": payload.get("transcript"),
                "turn_latencies": payload.get("turn_latencies"),
                "agent_build": payload.get("agent_build"),
                "fake_toolcall_detected": payload.get("fake_toolcall_detected"),
            }) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("calls.log write failed")


# ---------------------------------------------------------------------------
# Transcript sanitizer — strips leaked function call syntax
# ---------------------------------------------------------------------------

_FUNC_LEAK_PATTERN = _re.compile(
    r'<function=[^>]+>.*?</function>|'   # <function=name>{...}</function>
    r'\[TOOL RESULT[^\]]*\][^\n]*\n?|'   # [TOOL RESULT ...] lines
    r'`<function=[^`]+>`|'               # backtick-wrapped variants
    r'/?\s*end_call\b',                   # spoken or leaked tool name
    flags=_re.DOTALL | _re.IGNORECASE,
)

# Detects the SAME patterns as above, but used for fake-tool-call DETECTION,
# not transcript cleanup. The cleanup pass hides this text from the prospect
# and from the saved transcript — neither of those tells us whether the real
# tool was ever actually invoked. This pattern is checked against raw model
# output (before sanitizing) specifically to catch: model said
# "<function=book_meeting>...}</function>" as plain text instead of making
# a real structured tool call. If this fires and outcome never reaches
# "booked", we know for certain it was a fake call, not a slow/failed real one.
_FAKE_TOOLCALL_DETECT_PATTERN = _re.compile(
    r'<function\s*=\s*(book_meeting|get_available_slots|end_call)[^>]*>',
    flags=_re.IGNORECASE,
)

def _sanitize_transcript(text: str) -> str:
    """Remove any function call syntax that leaked into the spoken transcript."""
    if not text:
        return text
    cleaned = _FUNC_LEAK_PATTERN.sub("", text)
    # Collapse multiple blank lines left behind
    cleaned = _re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def _detect_fake_toolcall(raw_text: str) -> str | None:
    """Return the tool name if raw (pre-sanitized) text contains a fake
    <function=...> call that was never actually invoked, else None."""
    if not raw_text:
        return None
    m = _FAKE_TOOLCALL_DETECT_PATTERN.search(raw_text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Outbound dial — place the call and wait for a REAL answer
# ---------------------------------------------------------------------------

async def _dial_prospect(ctx: JobContext, *, lead: dict, sip_identity: str) -> bool:
    """Place the outbound PSTN call via the SIP trunk and block until the
    prospect actually answers. Returns True on a real human answer, False on
    no-answer / busy / reject / config error.

    Using wait_until_answered=True is the key fix: create_sip_participant only
    returns once the call is genuinely picked up, so we never start talking
    into a ringing phone (which previously caused the instant USER_REJECTED)."""
    if not settings.sip_outbound_trunk_id:
        logger.error("SIP_OUTBOUND_TRUNK_ID not set — cannot place call")
        return False

    phone = (lead.get("phone") or "").strip()
    if not phone:
        logger.error("lead has no phone number; nothing to dial")
        return False

    req = api.CreateSIPParticipantRequest(
        sip_trunk_id=settings.sip_outbound_trunk_id,
        sip_call_to=phone,
        room_name=ctx.room.name,
        participant_identity=sip_identity,
        participant_name=lead.get("name") or sip_identity,
        wait_until_answered=True,
    )
    req.ringing_timeout.FromNanoseconds(int(settings.sip_ring_timeout_sec * 1e9))
    if settings.sip_caller_id:
        req.sip_number = settings.sip_caller_id

    logger.info(
        "dialing %s via trunk %s (ring timeout %ss) ...",
        phone, settings.sip_outbound_trunk_id, settings.sip_ring_timeout_sec,
    )
    try:
        await ctx.api.sip.create_sip_participant(
            req, timeout=settings.sip_ring_timeout_sec + settings.sip_dial_extra_timeout_sec
        )
        logger.info("prospect answered: %s", phone)
        return True
    except api.TwirpError as e:
        # SIP status (486 busy, 603 declined, 480/487 no-answer, etc.) lands in metadata.
        sip_status = (e.metadata or {}).get("sip_status_code")
        sip_reason = (e.metadata or {}).get("sip_status")
        logger.warning(
            "dial not answered: twirp=%s msg=%s sip_status=%s reason=%s",
            e.code, e.message, sip_status, sip_reason,
        )
        return False
    except Exception:  # noqa: BLE001
        logger.exception("dial failed (unexpected)")
        return False


# ---------------------------------------------------------------------------
# Worker prewarm (Phase 3 = wiring, Phase 4 = VAD)
# ---------------------------------------------------------------------------
# LiveKit invokes this once per child worker process, right after the process
# starts and before it accepts a job. Phase 4 loads the Silero VAD model here
# (the SDK's own documented pattern — silero.VAD.load() explicitly recommends
# calling it inside prewarm()) and stashes it in proc.userdata so every job
# handled by this warm process reuses the SAME loaded model instead of paying
# the ONNX load cost again on every call. STT/TTS/LLM warmup are still out of
# scope here and land in later phases (Phase 5 = TTS, Phase 6 = LLM).
def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=settings.vad_min_silence_sec
    )
    logger.info(
        "prewarm: worker process ready (pid=%s), VAD loaded", os.getpid()
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: JobContext):
    await ctx.connect()

    lead = json.loads(ctx.job.metadata or "{}")
    lead_ref = lead.get("lead_id") or lead.get("row_id") or "unknown"
    sip_identity = lead.get("sip_identity") or f"lead-{lead_ref}"

    agent = OutboundSalesAgent(lead=lead, opening_already_spoken=True)

    # Build the TTS once and reuse the SAME instance for both the session and
    # the pre-warm below. deepgram.TTS keeps a persistent connection pool per
    # instance, so warming a throwaway instance would do nothing for the
    # session's real TTS — the opener would stay cold.
    tts_engine = _build_tts()
    # Build STT once and reuse the SAME instance for the session and the STT
    # pre-warm below — the persistent aiohttp session (DNS/TLS/WS to the STT
    # provider) is per-instance, so warming a throwaway instance wouldn't help
    # the session's real stream (same lesson as the TTS pre-warm).
    stt_engine = _build_stt()

    # Phase 4 — reuse the VAD model loaded once in prewarm() (proc.userdata)
    # instead of loading it again per job. Falls back to an inline load if the
    # cached instance is somehow unavailable (e.g. prewarm didn't run yet),
    # so behavior is never worse than before this change — it just loses the
    # warm-pool benefit for that one job.
    vad_engine = ctx.proc.userdata.get("vad")
    if vad_engine is None:
        logger.warning(
            "prewarm VAD missing from proc.userdata — loading inline as fallback"
        )
        vad_engine = silero.VAD.load(min_silence_duration=settings.vad_min_silence_sec)

    session = AgentSession(
        vad=vad_engine,

        stt=stt_engine,

        llm=_build_llm(),

        tts=tts_engine,

        turn_handling=_build_turn_handling(),
        min_consecutive_speech_delay=settings.min_consecutive_speech_delay_sec,
        aec_warmup_duration=settings.aec_warmup_duration_sec,
        user_away_timeout=settings.user_away_timeout_sec,
    )

    # --- Per-turn latency breakdown ----------------------------------------
    # EOU               = caller-silent → turn-committed (VAD + endpointing)
    # transcription     = STT finalization time
    # LLM ttft          = first token from LLM
    # TTS ttfb          = first audio byte from Deepgram
    # pipeline_total    = eou + llm_ttft + tts_ttfb (excludes SIP codec lag)
    _turn_metrics: dict[str, dict] = {}

    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)
        m = ev.metrics
        sid = getattr(m, "speech_id", None)
        if sid is None:
            return
        bucket = _turn_metrics.setdefault(sid, {})
        if isinstance(m, metrics.EOUMetrics):
            bucket["eou_delay"] = m.end_of_utterance_delay
            bucket["transcription_delay"] = m.transcription_delay
        elif isinstance(m, metrics.LLMMetrics):
            bucket["llm_ttft"] = m.ttft
        elif isinstance(m, metrics.TTSMetrics):
            bucket["tts_ttfb"] = m.ttfb
        if {"eou_delay", "llm_ttft", "tts_ttfb"} <= bucket.keys():
            total = bucket["eou_delay"] + bucket["llm_ttft"] + bucket["tts_ttfb"]
            logger.info(
                "turn latency: eou=%.2fs transcription=%.2fs llm_ttft=%.2fs "
                "tts_ttfb=%.2fs -> pipeline_total=%.2fs",
                bucket["eou_delay"],
                bucket.get("transcription_delay", 0.0),
                bucket["llm_ttft"],
                bucket["tts_ttfb"],
                total,
            )
            agent.turn_latencies.append({"speech_id": sid, "pipeline_total": total, **bucket})

    session.on("metrics_collected", _on_metrics_collected)

    # --- Results reporting -------------------------------------------------
    async def report_results():
        duration_sec = None
        if agent.call_started_at:
            duration_sec = (datetime.now(timezone.utc) - agent.call_started_at).total_seconds()

        transcript_dict = None
        transcript_text = ""
        raw_transcript_text = ""
        try:
            transcript_dict = session.history.to_dict()
            raw_transcript_text = flatten_history(session.history)
            transcript_text = _sanitize_transcript(raw_transcript_text)
        except Exception:  # noqa: BLE001
            logger.exception("transcript extraction failed")

        # Fake tool-call detection: the model said "<function=book_meeting>...}
        # </function>" as plain text instead of actually invoking the tool.
        # This is checked on raw (pre-sanitized) text — sanitizing happens
        # right above and would otherwise hide the exact evidence we need.
        # If the model claimed a booking this way but outcome never actually
        # reached "booked" via the real book_meeting() tool, the outcome is
        # corrected here so n8n/Sheets never show a phantom confirmed meeting.
        leaked_tool = _detect_fake_toolcall(raw_transcript_text)
        if leaked_tool and agent.outcome["status"] != "booked":
            agent.fake_toolcall_detected = leaked_tool
            logger.error(
                "FAKE TOOL CALL detected: model emitted '<function=%s>' as text "
                "without a real invocation — no booking/slots actually happened. "
                "Forcing outcome to 'incomplete' so this isn't silently lost.",
                leaked_tool,
            )
            agent.outcome["status"] = "incomplete"
            agent.outcome["follow_up_required"] = True
            agent.outcome["summary"] = (
                f"Agent appeared to claim a '{leaked_tool}' action verbally but the "
                "real tool was never invoked — likely told the prospect a meeting "
                "was booked when it was not. Needs human follow-up to confirm or "
                "rebook."
            )

        already_booked = agent.outcome["status"] == "booked"
        if not already_booked or not agent.outcome.get("summary"):
            outcome, summary, interested, follow_up = await classify_outcome(
                transcript_text,
                already_booked=already_booked,
                spoke_with_human=agent.spoke_with_human,
                duration_sec=duration_sec,
            )
            if not already_booked:
                agent.outcome["status"] = outcome
            if not agent.outcome.get("summary"):
                agent.outcome["summary"] = summary
            if agent.outcome.get("interested") is None:
                agent.outcome["interested"] = interested
            if not agent.outcome.get("follow_up_required"):
                agent.outcome["follow_up_required"] = follow_up

        payload = {
            "lead": lead,
            "outcome": agent.outcome["status"],
            "booking": agent.outcome["booking"],
            "summary": agent.outcome["summary"],
            "interested": agent.outcome["interested"],
            "follow_up_required": agent.outcome["follow_up_required"],
            "duration_sec": duration_sec,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "transcript": transcript_dict,
            "transcript_text": transcript_text,
            "turn_latencies": agent.turn_latencies,
            "agent_build": settings.agent_build,
            "fake_toolcall_detected": agent.fake_toolcall_detected,
        }
        _write_calls_log(payload)
        if settings.n8n_results_webhook:
            for attempt in range(settings.n8n_results_max_retries):
                try:
                    async with httpx.AsyncClient(timeout=settings.n8n_results_timeout_sec) as client:
                        r = await client.post(settings.n8n_results_webhook, json=payload)
                        if r.status_code < 500:
                            break
                except Exception:  # noqa: BLE001
                    logger.exception("attempt %d: failed to POST results to n8n", attempt + 1)
                await asyncio.sleep(2 ** attempt)
        logger.info("call result: %s (duration=%.1fs)", payload["outcome"], duration_sec or 0)

    ctx.add_shutdown_callback(report_results)

    # --- Pre-warm TTS before dialling ------------------------------------
    # deepgram.TTS.prewarm() is a real (non-no-op) SDK override: it opens the
    # WS connection in the background via its ConnectionPool, so calling it
    # here during the SIP ring/dial wait means the connection is already live
    # by the time session.say(opening_line) fires. It's synchronous but
    # non-blocking — it just schedules the connect as a background task on
    # the running loop, so no asyncio.create_task/try-except wrapper needed.
    # Runs once per session (this entrypoint runs once per job).
    logger.info("TTS prewarm requested")
    tts_engine.prewarm()

    # --- Pre-warm STT before dialling ------------------------------------
    # The STT websocket/model has a cold-start (~3s transcription delay) on the
    # very first user utterance. We warm the SAME stt_engine the AgentSession
    # uses by opening a stream and pushing a short burst of SILENCE during the
    # SIP ring/dial wait — this forces the DNS/TLS/WS connection to open and
    # primes the provider's audio pipeline before the prospect ever speaks.
    #
    # Notes:
    #   - <provider>.prewarm() is a no-op upstream, so we must open a stream.
    #   - Silence produces NO transcripts, so live transcription is unaffected.
    #   - We push audio to a throwaway warm-up stream that is closed before
    #     session.start(); it never touches the live recognition stream.
    #   - Endpointing/STT options are untouched (same instance, same config).
    #   - Runs once per session (this entrypoint runs once per job).
    async def _prewarm_stt() -> None:
        logger.info("STT prewarm started")
        stream = None
        try:
            stream = stt_engine.stream()

            # Drain any connection/metadata events (silence yields no transcript).
            async def _drain() -> None:
                try:
                    async for _ in stream:
                        pass
                except Exception:  # noqa: BLE001
                    pass

            drain_task = asyncio.create_task(_drain())

            # ~200ms of 16 kHz mono silence (20 ms frames) to open + prime.
            samples_per_frame = 320  # 20 ms @ 16 kHz
            silent = rtc.AudioFrame(
                data=b"\x00\x00" * samples_per_frame,
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=samples_per_frame,
            )
            for _ in range(10):
                stream.push_frame(silent)
                await asyncio.sleep(0.02)
            stream.end_input()

            try:
                await asyncio.wait_for(drain_task, timeout=5.0)
            except asyncio.TimeoutError:
                drain_task.cancel()
            logger.info("STT prewarm complete")
        except Exception:
            logger.warning("STT prewarm failed (non-fatal)")
        finally:
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:  # noqa: BLE001
                    pass

    asyncio.create_task(_prewarm_stt())

    # --- Pre-warm the primary Groq LLM connection before dialling ----------
    # Neither groq.LLM nor openai.LLM (which Groq subclasses) override the
    # base SDK's LLM.prewarm() — verified: 'prewarm' not in groq.LLM.__dict__
    # / openai.LLM.__dict__, both inherit the base no-op `pass`. The
    # framework never calls it automatically either. SDK v1.6.4 exposes no
    # public warmup API for LLM, so the only way to actually open the
    # DNS/TCP/TLS connection ahead of time is a real request. We use a
    # throwaway Groq-only client (_build_groq_warmup_client — never the
    # session's real LLM, never OpenAI, never the FallbackAdapter) and the
    # smallest public call available: models.list(), a metadata GET that
    # consumes no prompt tokens and touches no chat state. Accessing the
    # private _client attribute is unavoidable here since models.list() is
    # not exposed on the plugin's LLM wrapper itself, only on the underlying
    # openai.AsyncClient.
    async def _prewarm_llm() -> None:
        warmup_client = _build_groq_warmup_client()
        if warmup_client is None:
            return
        logger.info("LLM prewarm started")
        try:
            await warmup_client._client.models.list()
            logger.info("LLM prewarm finished")
        except Exception:
            logger.warning("LLM prewarm failed (non-fatal)")
        finally:
            await warmup_client.aclose()

    asyncio.create_task(_prewarm_llm())

    # --- Place the call and wait for a real answer ------------------------
    if not await _dial_prospect(ctx, lead=lead, sip_identity=sip_identity):
        agent.outcome["status"] = "no_answer"
        agent.outcome["summary"] = "Prospect did not answer / call was rejected."
        ctx.shutdown()
        return

    agent.spoke_with_human = True
    agent.call_started_at = datetime.now(timezone.utc)

    # Both watchdogs are created AFTER this returns — if session.start()
    # itself hangs (audio/track publishing issue), nothing is watching yet
    # and the call sits in total silence until the prospect gives up. This
    # timeout is what catches that specific window.
    try:
        await asyncio.wait_for(
            session.start(agent=agent, room=ctx.room),
            timeout=settings.session_start_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.error(
            "session.start() did not complete within %ss — aborting call",
            settings.session_start_timeout_sec,
        )
        agent.outcome["status"] = "incomplete"
        agent.outcome["summary"] = "Call aborted: session failed to start in time."
        agent.outcome["follow_up_required"] = True
        ctx.shutdown()
        return
    except Exception:
        logger.exception("session.start() raised — aborting call")
        agent.outcome["status"] = "incomplete"
        agent.outcome["summary"] = "Call aborted: session.start() raised an exception."
        agent.outcome["follow_up_required"] = True
        ctx.shutdown()
        return

    # --- Background thinking sound ----------------------------------------
    # NOTE: audio_player.start() is intentionally NOT awaited here. It isn't
    # needed until the first tool call (get_available_slots/book_meeting),
    # which is always several turns after the opener — awaiting it inline
    # used to block session.say(opening_line) below, adding pure serial
    # delay to time-to-first-audio for no benefit. Moved to a background
    # task so it initializes concurrently with the opener instead.
    audio_player: BackgroundAudioPlayer | None = None
    if settings.thinking_sound_enabled:
        audio_player = BackgroundAudioPlayer(
            thinking_sound=AudioConfig(
                source=BuiltinAudioClip.KEYBOARD_TYPING2,
                volume=settings.thinking_sound_volume,
            ),
        )

        async def _start_audio_player() -> None:
            try:
                await audio_player.start(room=ctx.room, agent_session=session)
            except Exception:  # noqa: BLE001
                logger.exception("audio_player start failed")

        asyncio.create_task(_start_audio_player())

        async def _close_audio_player() -> None:
            try:
                await audio_player.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("audio_player close failed")

        ctx.add_shutdown_callback(_close_audio_player)

    # --- Kick off the conversation ----------------------------------------
    
    watchdog_task = asyncio.create_task(_watchdog(agent, session, ctx))

    async def _cancel_watchdog() -> None:
        if not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

    ctx.add_shutdown_callback(_cancel_watchdog)

    # Scripted opener via TTS (no LLM TTFT). Must include AI disclosure per prompts.
    name = lead.get("name", "there")
    opening_line = lead.get("opening_line") or (
        f"Hi, is this {name}? "
        f"This is {CALLER_NAME} — I'm a sales agent calling from {COMPANY}. "
        "How are you doing today?"
    )

    dead_air_task = asyncio.create_task(
        _dead_air_watchdog(agent, session, ctx, opening_line=opening_line)
    )

    async def _cancel_dead_air_watchdog() -> None:
        if not dead_air_task.done():
            dead_air_task.cancel()
            try:
                await dead_air_task
            except asyncio.CancelledError:
                pass
        # The goodbye nudge task is spawned independently by the dead-air
        # loop above, not awaited by it, so it needs its own cleanup here.
        gw_task = agent._goodbye_watch_task
        if gw_task and not gw_task.done():
            gw_task.cancel()
            try:
                await gw_task
            except asyncio.CancelledError:
                pass

    ctx.add_shutdown_callback(_cancel_dead_air_watchdog)

    _t_say = time.monotonic()
    logger.info("session.say(opener) starting — %.2fs since call answered", _t_say - agent.call_started_at.timestamp())
    try:
        await session.say(opening_line, allow_interruptions=True)
    except Exception:
        # Don't let this crash the entrypoint silently — the dead-air
        # watchdog (already running, started above) will catch the
        # never-spoken state and retry/end gracefully regardless, but we
        # still want this in the logs for diagnosis.
        logger.exception("initial session.say(opening_line) raised")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            num_idle_processes=ServerEnvOption(dev_default=0, prod_default=2),
            agent_name=settings.agent_name,
        )
    )
