"""Text-to-Speech mit austauschbaren Engines (OpenAI, espeak-ng; später Piper/ElevenLabs)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Protocol

from .config import ChannelConfig, Settings

log = logging.getLogger(__name__)


class TTSEngine(Protocol):
    name: str

    def synthesize(self, text: str, out_dir: Path) -> Path:
        """Erzeugt eine Audiodatei in out_dir und gibt den Pfad zurück."""
        ...


class OpenAITTS:
    name = "openai"

    def __init__(self, settings: Settings, channel: ChannelConfig) -> None:
        from openai import OpenAI

        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY fehlt in .env")
        self._client = OpenAI(api_key=settings.openai_api_key, max_retries=3)
        self._model = settings.openai_tts_model
        self._voice = channel.tts_voice or settings.openai_tts_voice
        self._instructions = channel.tts_instructions

    def synthesize(self, text: str, out_dir: Path) -> Path:
        out = out_dir / "voice.mp3"
        kwargs: dict = dict(model=self._model, voice=self._voice, input=text, response_format="mp3")
        if self._instructions and not self._model.startswith("tts-1"):
            kwargs["instructions"] = self._instructions
        with self._client.audio.speech.with_streaming_response.create(**kwargs) as resp:
            resp.stream_to_file(out)
        log.info("OpenAI-TTS: %s (%s, %s)", out, self._model, self._voice)
        return out


class EspeakTTS:
    """Offline, robotisch – nur für Dry-Run/Tests."""
    name = "espeak"

    def __init__(self, voice: str = "de", speed_wpm: int = 165) -> None:
        self._voice = voice
        self._speed = speed_wpm

    def synthesize(self, text: str, out_dir: Path) -> Path:
        out = out_dir / "voice.wav"
        subprocess.run(["espeak-ng", "-v", self._voice, "-s", str(self._speed), "-w", str(out), text],
                       check=True, capture_output=True)
        return out


def get_tts(settings: Settings, channel: ChannelConfig, dry_run: bool = False) -> TTSEngine:
    provider = "espeak" if dry_run else settings.tts_provider.lower()
    if provider == "openai":
        return OpenAITTS(settings, channel)
    if provider == "espeak":
        return EspeakTTS(channel.espeak_voice)
    if provider in ("piper", "elevenlabs"):
        raise NotImplementedError(f"TTS '{provider}' ist vorgesehen, aber noch nicht implementiert "
                                  "(neue Klasse mit synthesize() in tts.py anlegen).")
    raise ValueError(f"Unbekannter TTS_PROVIDER '{provider}'")
