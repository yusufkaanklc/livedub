"""Language list shown in the UI. Codes are ISO 639-1."""

# (code, display name, English name used in prompts)
LANGUAGES: list[tuple[str, str, str]] = [
    ("tr", "Türkçe", "Turkish"),
    ("en", "English", "English"),
    ("de", "Deutsch", "German"),
    ("fr", "Français", "French"),
    ("es", "Español", "Spanish"),
    ("it", "Italiano", "Italian"),
    ("pt", "Português", "Portuguese"),
    ("ru", "Русский", "Russian"),
    ("uk", "Українська", "Ukrainian"),
    ("ar", "العربية", "Arabic"),
    ("fa", "فارسی", "Persian"),
    ("az", "Azərbaycanca", "Azerbaijani"),
    ("nl", "Nederlands", "Dutch"),
    ("pl", "Polski", "Polish"),
    ("ja", "日本語", "Japanese"),
    ("ko", "한국어", "Korean"),
    ("zh", "中文", "Chinese"),
    ("hi", "हिन्दी", "Hindi"),
    ("id", "Bahasa Indonesia", "Indonesian"),
    ("vi", "Tiếng Việt", "Vietnamese"),
]

AUTO = "auto"

# Output languages of gpt-realtime-translate (input side auto-detects 70+ languages, Turkish included).
TRANSLATE_MODEL_OUTPUTS = {"en", "es", "pt", "fr", "ja", "ru", "zh", "de", "ko", "hi", "id", "vi", "it"}


def english_name(code: str) -> str:
    for c, _, en in LANGUAGES:
        if c == code:
            return en
    return code


def display_name(code: str) -> str:
    if code == AUTO:
        return "Otomatik algıla"
    for c, name, _ in LANGUAGES:
        if c == code:
            return name
    return code
