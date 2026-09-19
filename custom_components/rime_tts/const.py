"""Constants for Rime TTS."""

DOMAIN = "rime_tts"
MODELS = ("coda", "mistv3")
DEFAULT_MODEL = "coda"
CONF_VOICE = "voice"
LANGUAGES = {
    "eng": "en",
    "spa": "es",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "por": "pt",
    "jpn": "ja",
    "ara": "ar",
    "hin": "hi",
    "ita": "it",
}
CONF_REGION = "region"
DEFAULT_REGION = "us-west"
HTTP_URLS = {
    "us-east": "https://users-east.rime.ai",
    "us-west": "https://users-west.rime.ai",
}
WS_URLS = {
    "us-east": "wss://users-east-ws.rime.ai/ws3",
    "us-west": "wss://users-ws.rime.ai/ws3",
}
