"""
Runtime configuration — single source of truth is .env.

Importing this module loads .env and validates every required variable.
Missing or empty required values raise ConfigurationError immediately.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from core.exceptions import ConfigurationError  # noqa: F401 — re-export for backward compatibility

load_dotenv()


def _raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip()


def _require_str(name: str) -> str:
    value = _raw(name)
    if not value:
        raise ConfigurationError(f"Missing {name}")
    return value


def _require_bool(name: str) -> bool:
    value = _require_str(name).lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigurationError(
        f"Invalid {name}={value!r} — expected true/false (or 1/0, yes/no, on/off)"
    )


def _require_float(name: str) -> float:
    value = _require_str(name)
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid {name}={value!r} — expected a number") from exc


def _require_int(name: str) -> int:
    value = _require_str(name)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid {name}={value!r} — expected an integer") from exc


def _require_api_key(*names: str) -> str:
    for name in names:
        value = _raw(name)
        if value:
            return value
    raise ConfigurationError(f"Missing {' or '.join(names)}")


def _optional_str(name: str) -> str | None:
    value = _raw(name)
    return value if value else None


# --- Optional-with-default readers -----------------------------------------
# Used for latency/turn-taking knobs that were removed from .env to reduce
# configuration surface. If a var is absent, the default (= the previously
# tuned production value) is used, so behavior is unchanged. These defaults
# are deliberately NOT LiveKit's own defaults where LiveKit's are slower
# (e.g. max_endpointing_delay: we keep 1.0s, LiveKit's default is 3.0s).

def _opt_float(name: str, default: float) -> float:
    value = _raw(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid {name}={value!r} — expected a number") from exc


def _opt_int(name: str, default: int) -> int:
    value = _raw(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid {name}={value!r} — expected an integer") from exc


def _opt_bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if not value:
        return default
    v = value.lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigurationError(
        f"Invalid {name}={value!r} — expected true/false (or 1/0, yes/no, on/off)"
    )


def _opt_str(name: str, default: str) -> str:
    value = _raw(name)
    return value if value else default


@dataclass(frozen=True)
class Settings:
    # --- Agent metadata ---
    agent_build: str
    agent_name: str

    # --- Integrations (optional empty where noted) ---
    n8n_results_webhook: str
    calls_log_path: str
    max_call_duration_sec: int

    # --- SIP outbound ---
    sip_outbound_trunk_id: str
    sip_caller_id: str
    sip_ring_timeout_sec: int

    # --- LLM ---
    llm_backend: str
    llm_model_groq: str | None
    llm_model_openai: str
    llm_temperature: float
    llm_fallback_enabled: bool
    llm_fallback_attempt_timeout_sec: float
    llm_parallel_tool_calls: bool
    llm_fallback_max_retry_per_llm: int
    llm_fallback_retry_on_chunk_sent: bool
    groq_api_key: str | None
    openai_api_key: str
    groq_reasoning_effort: str | None

    # --- STT ---
    stt_backend: str
    stt_model: str
    stt_language: str       # BCP-47, e.g. "en" or "en-US"
    stt_interim_results: bool
    stt_no_delay: bool      # Deepgram-only
    stt_smart_format: bool  # Deepgram-only
    stt_filler_words: bool  # Deepgram-only
    stt_punctuate: bool     # Deepgram-only
    stt_endpointing_ms: int              # ms of silence before turn commit

    # --- TTS ---
    tts_backend: str
    tts_voice: str | None

    # --- Turn-taking / latency ---
    vad_min_silence_sec: float
    min_endpointing_delay_sec: float
    max_endpointing_delay_sec: float
    endpointing_mode: str
    preemptive_generation: bool
    preemptive_tts: bool
    preemptive_max_speech_duration_sec: float
    preemptive_max_retries: int
    false_interruption_timeout_sec: float
    min_interruption_duration_sec: float
    min_interruption_words: int
    interruption_enabled: bool
    resume_false_interruption: bool
    backchannel_boundary_start_sec: float
    backchannel_boundary_end_sec: float
    aec_warmup_duration_sec: float
    min_consecutive_speech_delay_sec: float
    user_away_timeout_sec: float

    # --- Audio / watchdogs ---
    thinking_sound_enabled: bool
    thinking_sound_volume: float
    dead_air_poll_sec: float
    dead_air_timeout_sec: float
    max_dead_air_recoveries: int
    goodbye_nudge_timeout_sec: float
    session_start_timeout_sec: float
    tool_slow_speak_delay_sec: float

    # --- Post-call classification ---
    outcome_classifier_model: str
    outcome_classifier_timeout_sec: float
    outcome_classifier_temperature: float

    n8n_results_max_retries: int
    n8n_results_timeout_sec: float

    # --- Follow-up email (optional subsystem; disabled by default) ---
    # All fields are optional with safe defaults so existing deployments run
    # unchanged without adding any .env variables. See email_service.py.
    email_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    from_email: str
    from_name: str
    email_timeout_sec: float
    email_max_retries: int

    watchdog_soft_limit_buffer_sec: int
    sip_dial_extra_timeout_sec: int

    # --- Prompts ---
    prospect_tz: str


def _load_settings() -> Settings:
    llm_backend = _require_str("LLM_BACKEND").lower()
    llm_fallback_enabled = _require_bool("LLM_FALLBACK_ENABLED")

    groq_api_key: str | None = None
    llm_model_groq: str | None = None
    if llm_backend == "groq":
        groq_api_key = _require_api_key("GROQ_API_KEY", "Groq_API_KEY", "groq_api_key")
        llm_model_groq = _require_str("LLM_MODEL_GROQ")
    elif llm_backend != "openai":
        raise ConfigurationError(
            f"Unsupported LLM_BACKEND={llm_backend!r} — expected 'groq' or 'openai'"
        )

    openai_api_key = _require_str("OPENAI_API_KEY")
    llm_model_openai = _require_str("LLM_MODEL_OPENAI")

    if llm_backend == "openai" and llm_fallback_enabled:
        raise ConfigurationError(
            "LLM_FALLBACK_ENABLED=true is only valid when LLM_BACKEND=groq"
        )

    stt_backend = _require_str("STT_BACKEND").lower()
    if stt_backend != "deepgram":
        raise ConfigurationError(
            f"Unsupported STT_BACKEND={stt_backend!r} — expected 'deepgram'"
        )
    _require_api_key("DEEPGRAM_API_KEY")

    tts_backend = _require_str("TTS_BACKEND").lower()
    if tts_backend != "deepgram":
        raise ConfigurationError(
            f"Unsupported TTS_BACKEND={tts_backend!r} — expected 'deepgram'"
        )
    tts_voice = _require_str("TTS_VOICE")

    return Settings(
        agent_build=_require_str("AGENT_BUILD"),
        agent_name=_require_str("AGENT_NAME"),
        n8n_results_webhook=_raw("N8N_RESULTS_WEBHOOK") or "",
        calls_log_path=_require_str("CALLS_LOG"),
        max_call_duration_sec=_require_int("MAX_CALL_DURATION_SEC"),
        sip_outbound_trunk_id=_require_str("SIP_OUTBOUND_TRUNK_ID"),
        sip_caller_id=_require_str("SIP_CALLER_ID"),
        sip_ring_timeout_sec=_require_int("SIP_RING_TIMEOUT_SEC"),
        llm_backend=llm_backend,
        llm_model_groq=llm_model_groq,
        llm_model_openai=llm_model_openai,
        llm_temperature=_require_float("LLM_TEMPERATURE"),
        llm_fallback_enabled=llm_fallback_enabled,
        llm_fallback_attempt_timeout_sec=_require_float("LLM_FALLBACK_ATTEMPT_TIMEOUT_SEC"),
        llm_parallel_tool_calls=_require_bool("LLM_PARALLEL_TOOL_CALLS"),
        llm_fallback_max_retry_per_llm=_require_int("LLM_FALLBACK_MAX_RETRY_PER_LLM"),
        llm_fallback_retry_on_chunk_sent=_require_bool("LLM_FALLBACK_RETRY_ON_CHUNK_SENT"),
        groq_api_key=groq_api_key,
        openai_api_key=openai_api_key,
        groq_reasoning_effort=_optional_str("GROQ_REASONING_EFFORT"),
        stt_backend=stt_backend,
        stt_model=_require_str("STT_MODEL"),
        stt_language=_require_str("STT_LANGUAGE"),
        stt_interim_results=_require_bool("STT_INTERIM_RESULTS"),
        stt_no_delay=_require_bool("STT_NO_DELAY"),
        stt_smart_format=_require_bool("STT_SMART_FORMAT"),
        stt_filler_words=_require_bool("STT_FILLER_WORDS"),
        stt_punctuate=_require_bool("STT_PUNCTUATE"),
        stt_endpointing_ms=_require_int("STT_ENDPOINTING_MS"),
        tts_backend=tts_backend,
        tts_voice=tts_voice,
        # --- Kept in .env: the knobs that materially move latency/UX ---
        vad_min_silence_sec=_opt_float("VAD_MIN_SILENCE_SEC", 0.35),
        endpointing_mode=_opt_str("ENDPOINTING_MODE", "fixed"),
        preemptive_generation=_opt_bool("PREEMPTIVE_GENERATION", True),
        interruption_enabled=_opt_bool("INTERRUPTION_ENABLED", True),
        min_interruption_duration_sec=_opt_float("MIN_INTERRUPTION_DURATION_SEC", 0.35),
        false_interruption_timeout_sec=_opt_float("FALSE_INTERRUPTION_TIMEOUT_SEC", 1.2),
        resume_false_interruption=_opt_bool("RESUME_FALSE_INTERRUPTION", True),
        dead_air_timeout_sec=_opt_float("DEAD_AIR_TIMEOUT_SEC", 8.0),
        max_dead_air_recoveries=_opt_int("MAX_DEAD_AIR_RECOVERIES", 1),
        goodbye_nudge_timeout_sec=_opt_float("GOODBYE_NUDGE_TIMEOUT_SEC", 3.0),
        # --- Removed from .env: default = last tuned production value ---
        # (kept as internal defaults so latency/behavior is unchanged; see
        #  the optimization report for why each is safe to drop from .env)
        min_endpointing_delay_sec=_opt_float("MIN_ENDPOINTING_DELAY_SEC", 0.20),
        max_endpointing_delay_sec=_opt_float("MAX_ENDPOINTING_DELAY_SEC", 1.0),
        preemptive_tts=_opt_bool("PREEMPTIVE_TTS", False),
        preemptive_max_speech_duration_sec=_opt_float("PREEMPTIVE_MAX_SPEECH_DURATION_SEC", 12.0),
        preemptive_max_retries=_opt_int("PREEMPTIVE_MAX_RETRIES", 2),
        min_interruption_words=_opt_int("MIN_INTERRUPTION_WORDS", 2),
        backchannel_boundary_start_sec=_opt_float("BACKCHANNEL_BOUNDARY_START_SEC", 0.5),
        backchannel_boundary_end_sec=_opt_float("BACKCHANNEL_BOUNDARY_END_SEC", 0.5),
        aec_warmup_duration_sec=_opt_float("AEC_WARMUP_DURATION_SEC", 0.0),
        min_consecutive_speech_delay_sec=_opt_float("MIN_CONSECUTIVE_SPEECH_DELAY_SEC", 0.05),
        user_away_timeout_sec=_opt_float("USER_AWAY_TIMEOUT_SEC", 20.0),
        thinking_sound_enabled=_opt_bool("THINKING_SOUND_ENABLED", False),
        thinking_sound_volume=_opt_float("THINKING_SOUND_VOLUME", 0.15),
        dead_air_poll_sec=_opt_float("DEAD_AIR_POLL_SEC", 1.0),
        session_start_timeout_sec=_opt_float("SESSION_START_TIMEOUT_SEC", 15.0),
        tool_slow_speak_delay_sec=_opt_float("TOOL_SLOW_SPEAK_DELAY_SEC", 2.0),
        outcome_classifier_model=_require_str("OUTCOME_CLASSIFIER_MODEL"),
        outcome_classifier_timeout_sec=_require_float("OUTCOME_CLASSIFIER_TIMEOUT_SEC"),
        outcome_classifier_temperature=_require_float("OUTCOME_CLASSIFIER_TEMPERATURE"),
        n8n_results_max_retries=_require_int("N8N_RESULTS_MAX_RETRIES"),
        n8n_results_timeout_sec=_require_float("N8N_RESULTS_TIMEOUT_SEC"),
        # Follow-up email — optional, defaults keep the feature OFF and boot
        # existing .env files unchanged (Gmail SMTP + App Password expected
        # when enabled; see email_service.py).
        email_enabled=_opt_bool("EMAIL_ENABLED", False),
        smtp_host=_opt_str("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_opt_int("SMTP_PORT", 587),
        smtp_username=_opt_str("SMTP_USERNAME", ""),
        smtp_password=_opt_str("SMTP_PASSWORD", ""),
        smtp_use_tls=_opt_bool("SMTP_USE_TLS", True),
        from_email=_opt_str("FROM_EMAIL", ""),
        from_name=_opt_str("FROM_NAME", "Technaptix"),
        email_timeout_sec=_opt_float("EMAIL_TIMEOUT_SEC", 15.0),
        email_max_retries=_opt_int("EMAIL_MAX_RETRIES", 2),
        watchdog_soft_limit_buffer_sec=_require_int("WATCHDOG_SOFT_LIMIT_BUFFER_SEC"),
        sip_dial_extra_timeout_sec=_require_int("SIP_DIAL_EXTRA_TIMEOUT_SEC"),
        prospect_tz=_require_str("PROSPECT_TZ"),
    )


settings = _load_settings()
