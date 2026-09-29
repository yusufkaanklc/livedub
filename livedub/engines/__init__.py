from ..config import ENGINE_CASCADE, ENGINE_GEMINI, ENGINE_REALTIME, ENGINE_TRANSLATE
from .base import Engine, FatalEngineError
from .cascade import CascadeEngine
from .gemini_translate import GeminiTranslateEngine
from .openai_realtime import OpenAIRealtimeEngine
from .openai_translate import OpenAITranslateEngine

ENGINES: dict[str, type[Engine]] = {
    ENGINE_GEMINI: GeminiTranslateEngine,
    ENGINE_TRANSLATE: OpenAITranslateEngine,
    ENGINE_REALTIME: OpenAIRealtimeEngine,
    ENGINE_CASCADE: CascadeEngine,
}

ENGINE_LABELS = {
    ENGINE_GEMINI: "Gemini Live Translate — eşzamanlı, Türkçe dahil 70+ dil (en ucuz)",
    ENGINE_TRANSLATE: "OpenAI Realtime Translate — eşzamanlı (13 çıkış dili, Türkçe yok)",
    ENGINE_REALTIME: "OpenAI Realtime GPT — tercüman modu, tüm diller",
    ENGINE_CASCADE: "Kaskad: Deepgram → çeviri → TTS (ElevenLabs/OpenAI ses)",
}

__all__ = ["ENGINES", "ENGINE_LABELS", "Engine", "FatalEngineError"]
