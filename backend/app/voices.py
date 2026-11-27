"""Parsing of an upstream's GET /audio/voices answer (shared by discovery and the gateway route), and the audio defaults of a gateway model."""
import re
from typing import Optional


def voice_id(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("id") or v.get("voice_id") or v.get("name") or "")
    return ""


def parse_voices(data) -> list:
    """Kokoro-FastAPI answers {"voices": [...]}; some servers answer a bare list. Items are strings or objects."""
    items = data.get("voices") if isinstance(data, dict) else data
    return [v for v in items if voice_id(v)][:500] if isinstance(items, list) else []


def clean_voices(voices) -> list[str]:
    """Trimmed, de-duplicated voice names chosen by an admin (at most 200 entries of 64 characters)."""
    out: list[str] = []
    for v in voices or []:
        v = str(v).strip()[:64]
        if v and v not in out:
            out.append(v)
    return out[:200]


SPEECH_FORMATS = ("mp3", "opus", "aac", "flac", "wav", "pcm")
TRANSCRIPTION_FORMATS = ("json", "text", "srt", "verbose_json", "vtt")


def clean_audio_defaults(d, voices=None) -> Optional[dict]:
    """Defaults for audio requests that leave a setting out: {"speech": {voice, response_format, speed}, "transcription": {language,
    response_format, temperature}}. Raises ValueError with a message for the admin; unknown keys are dropped; empty -> None."""
    if not isinstance(d, dict) or not d:
        return None
    out: dict = {}
    sp, tr = d.get("speech") or {}, d.get("transcription") or {}
    s: dict = {}
    voice = str(sp.get("voice") or "").strip()
    if voice:
        if len(voice) > 64:
            raise ValueError("The default voice is too long")
        if voices and voice not in voices:
            raise ValueError(f"The default voice '{voice}' is not in this model's voice list")
        s["voice"] = voice
    if sp.get("response_format") not in (None, ""):
        if sp["response_format"] not in SPEECH_FORMATS:
            raise ValueError(f"Speech format must be one of: {', '.join(SPEECH_FORMATS)}")
        s["response_format"] = sp["response_format"]
    if sp.get("speed") not in (None, ""):
        try:
            speed = float(sp["speed"])
        except (TypeError, ValueError):
            raise ValueError("Speech speed must be a number") from None
        if not 0.25 <= speed <= 4.0:
            raise ValueError("Speech speed must be between 0.25 and 4")
        s["speed"] = speed
    r: dict = {}
    lang = str(tr.get("language") or "").strip()
    if lang:
        if not re.fullmatch(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{2,8})?", lang):
            raise ValueError("Transcription language must be a language code such as en or pl")
        r["language"] = lang
    if tr.get("response_format") not in (None, ""):
        if tr["response_format"] not in TRANSCRIPTION_FORMATS:
            raise ValueError(f"Transcription format must be one of: {', '.join(TRANSCRIPTION_FORMATS)}")
        r["response_format"] = tr["response_format"]
    if tr.get("temperature") not in (None, ""):
        try:
            temp = float(tr["temperature"])
        except (TypeError, ValueError):
            raise ValueError("Transcription temperature must be a number") from None
        if not 0.0 <= temp <= 1.0:
            raise ValueError("Transcription temperature must be between 0 and 1")
        r["temperature"] = temp
    if s:
        out["speech"] = s
    if r:
        out["transcription"] = r
    return out or None


def abilities(member_caps) -> dict:
    """What a public model can do, from the capabilities of its upstream models: chat, text-to-speech, speech-to-text. Decided per upstream model:
    name-based detection tags a TTS model "audio" as well, so only an audio model that is not a TTS model counts as speech-to-text."""
    caps = [set(c or []) for c in member_caps]
    return {"chat": any("chat" in c for c in caps), "tts": any("tts" in c for c in caps),
            "stt": any("audio" in c and "tts" not in c for c in caps), "capabilities": sorted(set().union(*caps)) if caps else []}
