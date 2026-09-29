"""User settings, persisted as JSON in ~/.livedub/settings.json.

API keys can also come from the environment (GEMINI_API_KEY, OPENAI_API_KEY, DEEPGRAM_API_KEY,
ELEVENLABS_API_KEY, DEEPL_API_KEY); a key typed in the app takes precedence.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path.home() / ".livedub"
CONFIG_FILE = CONFIG_DIR / "settings.json"

ENGINE_GEMINI = "gemini"  # Gemini Live Translate: simultaneous speech-to-speech, 70+ output languages
ENGINE_TRANSLATE = "translate"  # gpt-realtime-translate: simultaneous speech-to-speech
ENGINE_REALTIME = "realtime"  # gpt-realtime-*: prompted interpreter, any target language
ENGINE_CASCADE = "cascade"  # Deepgram STT -> text translation -> streaming TTS


@dataclass
class Settings:
    engine: str = ENGINE_GEMINI
    source_lang: str = "auto"
    target_lang: str = "tr"

    source_device: str = ""
    output_device: str = ""
    dub_volume: float = 1.0
    original_volume: float = 0.0
    duck_level: float = 0.3
    feedback_guard: str = "auto"  # auto | on | off

    gemini_api_key: str = ""
    openai_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    deepl_api_key: str = ""

    # Shared by both OpenAI speech engines
    show_source_text: bool = True
    noise_reduction: str = ""  # "" | near_field | far_field

    # ENGINE_GEMINI
    gemini_translate_model: str = "gemini-3.5-live-translate-preview"

    # ENGINE_TRANSLATE
    translate_model: str = "gpt-realtime-translate"
    translate_transcribe_model: str = "gpt-realtime-whisper"

    # ENGINE_REALTIME
    realtime_model: str = "gpt-realtime-1.5"
    realtime_voice: str = "marin"
    realtime_speed: float = 1.1
    realtime_reasoning: str = ""  # "" (model default) | minimal | low
    realtime_transcribe_model: str = "gpt-4o-mini-transcribe"
    vad_type: str = "server_vad"  # server_vad | semantic_vad
    vad_threshold: float = 0.5
    vad_silence_ms: int = 350
    max_turn_s: float = 7.0

    # ENGINE_CASCADE
    deepgram_model: str = "nova-3"
    deepgram_endpointing_ms: int = 300
    translator: str = "openai"  # openai | deepl
    translate_text_model: str = "gpt-4.1-mini"
    tts_provider: str = "openai"  # openai | elevenlabs
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "coral"
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    adaptive_speed: bool = True

    def key(self, name: str) -> str:
        value = getattr(self, f"{name}_api_key", "")
        return value.strip() or os.environ.get(f"{name.upper()}_API_KEY", "").strip()

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
