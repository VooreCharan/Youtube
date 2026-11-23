"""
Language & voice catalog shared by the script, TTS, trends and bot layers.

Each language knows:
  • how to name itself to the LLM (``name``) and to the user (``native`` + ``flag``)
  • which gTTS / Whisper code to use
  • which Google News edition to read trending headlines from
  • which Edge-TTS neural voices are offered to the user
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

GTTS_VOICE_PREFIX = "gtts:"


@dataclass(frozen=True)
class Voice:
    id: str        # Edge-TTS voice name, or "gtts:<lang>" for the Google fallback voice
    label: str     # Human label shown in Telegram

    @property
    def is_gtts(self) -> bool:
        return self.id.startswith(GTTS_VOICE_PREFIX)


@dataclass(frozen=True)
class Language:
    code: str
    name: str                      # English name, used in LLM prompts
    native: str                    # Name in its own script, shown to the user
    flag: str
    news: Tuple[str, str, str]     # Google News (hl, gl, ceid)
    voices: Tuple[Voice, ...] = field(default_factory=tuple)

    @property
    def button_label(self) -> str:
        if self.native == self.name:
            return f"{self.flag} {self.name}"
        return f"{self.flag} {self.native} ({self.name})"

    def all_voices(self) -> List[Voice]:
        """Neural voices plus the always-available Google TTS fallback voice."""
        return list(self.voices) + [Voice(f"{GTTS_VOICE_PREFIX}{self.code}", "Google voice (basic)")]


def _v(voice_id: str, label: str) -> Voice:
    return Voice(voice_id, label)


LANGUAGES: Dict[str, Language] = {
    lang.code: lang
    for lang in [
        Language("en", "English", "English", "🇺🇸", ("en-US", "US", "US:en"), (
            _v("en-US-AvaNeural", "Ava · US · female"),
            _v("en-US-AndrewNeural", "Andrew · US · male"),
            _v("en-US-EmmaNeural", "Emma · US · female"),
            _v("en-US-BrianNeural", "Brian · US · male"),
            _v("en-GB-SoniaNeural", "Sonia · UK · female"),
            _v("en-GB-RyanNeural", "Ryan · UK · male"),
            _v("en-IN-NeerjaNeural", "Neerja · India · female"),
            _v("en-IN-PrabhatNeural", "Prabhat · India · male"),
        )),
        Language("hi", "Hindi", "हिन्दी", "🇮🇳", ("hi", "IN", "IN:hi"), (
            _v("hi-IN-SwaraNeural", "Swara · female"),
            _v("hi-IN-MadhurNeural", "Madhur · male"),
        )),
        Language("te", "Telugu", "తెలుగు", "🇮🇳", ("te", "IN", "IN:te"), (
            _v("te-IN-ShrutiNeural", "Shruti · female"),
            _v("te-IN-MohanNeural", "Mohan · male"),
        )),
        Language("ta", "Tamil", "தமிழ்", "🇮🇳", ("ta", "IN", "IN:ta"), (
            _v("ta-IN-PallaviNeural", "Pallavi · female"),
            _v("ta-IN-ValluvarNeural", "Valluvar · male"),
        )),
        Language("kn", "Kannada", "ಕನ್ನಡ", "🇮🇳", ("kn", "IN", "IN:kn"), (
            _v("kn-IN-SapnaNeural", "Sapna · female"),
            _v("kn-IN-GaganNeural", "Gagan · male"),
        )),
        Language("ml", "Malayalam", "മലയാളം", "🇮🇳", ("ml", "IN", "IN:ml"), (
            _v("ml-IN-SobhanaNeural", "Sobhana · female"),
            _v("ml-IN-MidhunNeural", "Midhun · male"),
        )),
        Language("bn", "Bengali", "বাংলা", "🇮🇳", ("bn", "IN", "IN:bn"), (
            _v("bn-IN-TanishaaNeural", "Tanishaa · female"),
            _v("bn-IN-BashkarNeural", "Bashkar · male"),
        )),
        Language("mr", "Marathi", "मराठी", "🇮🇳", ("mr", "IN", "IN:mr"), (
            _v("mr-IN-AarohiNeural", "Aarohi · female"),
            _v("mr-IN-ManoharNeural", "Manohar · male"),
        )),
        Language("gu", "Gujarati", "ગુજરાતી", "🇮🇳", ("gu", "IN", "IN:gu"), (
            _v("gu-IN-DhwaniNeural", "Dhwani · female"),
            _v("gu-IN-NiranjanNeural", "Niranjan · male"),
        )),
        Language("es", "Spanish", "Español", "🇪🇸", ("es", "ES", "ES:es"), (
            _v("es-ES-ElviraNeural", "Elvira · Spain · female"),
            _v("es-ES-AlvaroNeural", "Álvaro · Spain · male"),
            _v("es-MX-DaliaNeural", "Dalia · Mexico · female"),
            _v("es-MX-JorgeNeural", "Jorge · Mexico · male"),
        )),
        Language("fr", "French", "Français", "🇫🇷", ("fr", "FR", "FR:fr"), (
            _v("fr-FR-DeniseNeural", "Denise · female"),
            _v("fr-FR-HenriNeural", "Henri · male"),
        )),
        Language("de", "German", "Deutsch", "🇩🇪", ("de", "DE", "DE:de"), (
            _v("de-DE-TanjaNeural", "Tanja · female"),
            _v("de-DE-ChristophNeural", "Christoph · male"),
        )),
        Language("pt", "Portuguese", "Português", "🇧🇷", ("pt-BR", "BR", "BR:pt-419"), (
            _v("pt-BR-FranciscaNeural", "Francisca · Brazil · female"),
            _v("pt-BR-AntonioNeural", "Antônio · Brazil · male"),
        )),
        Language("ar", "Arabic", "العربية", "🇸🇦", ("ar", "EG", "EG:ar"), (
            _v("ar-SA-ZariyahNeural", "Zariyah · female"),
            _v("ar-SA-HamedNeural", "Hamed · male"),
        )),
        Language("ja", "Japanese", "日本語", "🇯🇵", ("ja", "JP", "JP:ja"), (
            _v("ja-JP-NanamiNeural", "Nanami · female"),
            _v("ja-JP-KeitaNeural", "Keita · male"),
        )),
    ]
}

DEFAULT_LANGUAGE = "en"


def get_language(code: Optional[str]) -> Language:
    """Return the language for *code*, falling back to English."""
    if code and code in LANGUAGES:
        return LANGUAGES[code]
    if code:
        prefix = code.split("-")[0].lower()
        if prefix in LANGUAGES:
            return LANGUAGES[prefix]
    return LANGUAGES[DEFAULT_LANGUAGE]


def language_from_voice(voice: Optional[str]) -> Language:
    """Infer the language from an Edge voice name ('te-IN-ShrutiNeural') or 'gtts:te'."""
    if not voice:
        return get_language(None)
    if voice.startswith(GTTS_VOICE_PREFIX):
        return get_language(voice[len(GTTS_VOICE_PREFIX):])
    return get_language(voice.split("-")[0])
