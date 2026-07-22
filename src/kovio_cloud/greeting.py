"""Greeting-on-Go — the robot's natural spoken welcome when a session starts.

Two hops, both plain ``requests`` (the cloud already ships requests; no SDKs):

  OpenRouter (Claude Haiku 4.5)  ->  a fresh, short spoken line
  ElevenLabs (TTS)               ->  a WAV the robot plays out its JBL

``render_greeting_wav`` is the single entry point the session route calls from a
background task. It is intentionally total: any misconfig or upstream error logs
and returns ``None`` (feature simply stays silent) — a greeting must never break
session start. The WAV is a mono 16-bit PCM container wrapped around ElevenLabs'
raw ``pcm_*`` output so the robot can play it with ``paplay`` without needing an
mp3 decoder installed.
"""
from __future__ import annotations

import io
import wave

import requests

from .config import Settings
from .db import get_logger

log = get_logger("kovio_cloud.greeting")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_HTTP_TIMEOUT = 20  # seconds; runs off-request in a background task

_SYSTEM_PROMPT = (
    "You are the voice of a friendly humanoid robot greeting a person who has "
    "just walked up to a live advertising display you are showing. Write ONE "
    "warm, natural spoken greeting, at most two short sentences, that a robot "
    "would say out loud. Be human and inviting, never salesy or robotic. Do "
    "not use emoji, stage directions, quotation marks, or the person's name. "
    "Return only the words to be spoken."
)


def build_context(
    *,
    campaign_name: str | None,
    advertiser: str | None,
    category: str | None,
    is_blended: bool,
) -> dict:
    """Shape the campaign facts the greeting prompt is allowed to lean on."""
    return {
        "campaign_name": campaign_name,
        "advertiser": advertiser,
        "category": category,
        "is_blended": is_blended,
    }


def _user_prompt(ctx: dict) -> str:
    if ctx.get("is_blended") or not ctx.get("advertiser"):
        # Blended playlist (or no bound campaign): no single advertiser to name,
        # so keep it a generic, brand-neutral welcome.
        return (
            "The display is looping several ads, so do not name any brand. "
            "Give a warm general welcome inviting the person to take a look."
        )
    bits = [f"advertiser: {ctx['advertiser']}"]
    if ctx.get("campaign_name"):
        bits.append(f"campaign: {ctx['campaign_name']}")
    if ctx.get("category"):
        bits.append(f"category: {ctx['category']}")
    return (
        "The display is showing this single advertiser — you may naturally "
        "reference the brand once if it fits.\n" + "\n".join(bits)
    )


def generate_greeting_text(ctx: dict, settings: Settings) -> str | None:
    """Ask OpenRouter for the spoken line. None on any error/misconfig."""
    if not settings.openrouter_api_key:
        log.warning("greeting.skip", reason="no OPENROUTER_API_KEY")
        return None
    try:
        resp = requests.post(
            _OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers (optional but recommended).
                "HTTP-Referer": settings.web_app_url,
                "X-Title": "Kovio robot greeting",
            },
            json={
                "model": settings.greeting_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _user_prompt(ctx)},
                ],
                "max_tokens": 120,
                "temperature": 0.9,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        text = (
            resp.json()["choices"][0]["message"]["content"] or ""
        ).strip().strip('"').strip()
        if not text:
            log.warning("greeting.empty_text")
            return None
        # Speak-path guardrail: the /current schema and TTS both dislike very
        # long lines; a greeting is a sentence or two.
        return text[:500]
    except Exception:
        log.exception("greeting.openrouter_failed")
        return None


def synthesize_wav(text: str, settings: Settings) -> bytes | None:
    """Render ``text`` to a WAV via ElevenLabs. None on any error/misconfig."""
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        log.warning(
            "greeting.skip",
            reason="missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID",
        )
        return None
    rate = int(settings.elevenlabs_sample_rate)
    try:
        resp = requests.post(
            _ELEVENLABS_URL.format(voice_id=settings.elevenlabs_voice_id),
            params={"output_format": f"pcm_{rate}"},
            headers={
                "xi-api-key": settings.elevenlabs_api_key,
                "Content-Type": "application/json",
                "Accept": "audio/pcm",
            },
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        pcm = resp.content
        if not pcm:
            log.warning("greeting.empty_audio")
            return None
        return _wrap_pcm_as_wav(pcm, rate)
    except Exception:
        log.exception("greeting.elevenlabs_failed")
        return None


def _wrap_pcm_as_wav(pcm: bytes, rate: int) -> bytes:
    """Wrap raw 16-bit little-endian mono PCM in a minimal WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def render_greeting_wav(ctx: dict, settings: Settings) -> tuple[str, bytes] | None:
    """Full greeting pipeline: text -> voice. Returns (text, wav) or None.

    Total by design — logs and returns None on any failure so the caller's
    background task can no-op silently.
    """
    text = generate_greeting_text(ctx, settings)
    if not text:
        return None
    wav = synthesize_wav(text, settings)
    if not wav:
        return None
    log.info("greeting.rendered", chars=len(text), wav_bytes=len(wav))
    return text, wav
