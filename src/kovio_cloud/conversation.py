"""Push-to-talk conversation — the robot's spoken reply to what a person said.

The robot does speech-to-text locally (faster-whisper) and POSTs the recognized
*text*; this module turns a running conversation into the next spoken reply:

  OpenRouter (Claude Haiku 4.5)  ->  short reply text, given the history
  ElevenLabs (reused from greeting.synthesize_wav)  ->  the reply WAV

Same posture as greeting.py: plain ``requests``, total (logs + returns None on
any error), so a failed turn never breaks the session. History is bounded so a
long chat can't grow the prompt without limit.
"""
from __future__ import annotations

import requests

from .config import Settings
from .db import get_logger
from .greeting import synthesize_wav  # reuse the exact ElevenLabs path

log = get_logger("kovio_cloud.conversation")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_HTTP_TIMEOUT = 20

# Keep at most this many prior messages (user+assistant) in the prompt so a long
# conversation stays cheap and fast. The system prompt is always prepended.
_MAX_HISTORY_MESSAGES = 16

_SYSTEM_PROMPT = (
    "You are a friendly humanoid robot chatting out loud with a person standing "
    "at your live advertising display. Keep every reply to one or two short "
    "spoken sentences — natural, warm, a little playful, genuinely helpful. "
    "You can answer questions and make small talk. Never use emoji, stage "
    "directions, markdown, or asterisks; return only the words to be spoken."
)


def generate_reply(history: list[dict], settings: Settings) -> str | None:
    """Given the conversation ``history`` (list of {role, content}, ending with
    the person's latest turn), return the robot's next spoken reply. None on any
    error/misconfig."""
    if not settings.openrouter_api_key:
        log.warning("conversation.skip", reason="no OPENROUTER_API_KEY")
        return None
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(history[-_MAX_HISTORY_MESSAGES:])
    try:
        resp = requests.post(
            _OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.web_app_url,
                "X-Title": "Kovio robot conversation",
            },
            json={
                "model": settings.greeting_model,
                "messages": messages,
                "max_tokens": 150,
                "temperature": 0.8,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        text = (
            resp.json()["choices"][0]["message"]["content"] or ""
        ).strip().strip('"').strip()
        if not text:
            log.warning("conversation.empty_reply")
            return None
        return text[:500]
    except Exception:
        log.exception("conversation.openrouter_failed")
        return None


def reply_wav(history: list[dict], settings: Settings) -> tuple[str, bytes] | None:
    """Full turn: reply text -> voice. Returns (reply_text, wav) or None."""
    text = generate_reply(history, settings)
    if not text:
        return None
    wav = synthesize_wav(text, settings)
    if not wav:
        return None
    log.info("conversation.replied", chars=len(text), wav_bytes=len(wav))
    return text, wav
