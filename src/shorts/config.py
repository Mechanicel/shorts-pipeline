"""Zentrale Konfiguration: .env (Geheimnisse/Provider) + config/channels.yaml (pro Kanal)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

APP_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parents[2]))
load_dotenv(APP_ROOT / ".env", override=False)


@dataclass(frozen=True)
class SourceConfig:
    name: str
    type: str  # "rss" | "html"
    url: str
    link_pattern: str | None = None
    max_items: int = 20


@dataclass(frozen=True)
class ChannelConfig:
    key: str
    name: str
    language: str
    locale_hint: str
    youtube_token: Path
    youtube_category_id: str
    contains_synthetic_media: bool
    tts_voice: str | None
    tts_instructions: str
    espeak_voice: str
    default_hashtags: list[str]
    outro_text: str
    max_new_topics_per_run: int
    max_videos_per_run: int
    publish_slots: list[str]
    sources: list[SourceConfig]
    background_queries: list[str] = field(default_factory=list)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-opus-5-5") or "")
    anthropic_effort: str = field(default_factory=lambda: _env("ANTHROPIC_EFFORT", "high") or "high")
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "openai") or "openai")
    openai_api_key: str | None = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_tts_model: str = field(default_factory=lambda: _env("OPENAI_TTS_MODEL", "gpt-4o-mini-tts") or "")
    openai_tts_voice: str = field(default_factory=lambda: _env("OPENAI_TTS_VOICE", "nova") or "nova")
    tts_speed: float = field(default_factory=lambda: float(_env("TTS_SPEED", "1.15") or 1.15))
    pixabay_api_key: str | None = field(default_factory=lambda: _env("PIXABAY_API_KEY"))
    subtitle_timing: str = field(default_factory=lambda: _env("SUBTITLE_TIMING", "whisper") or "whisper")
    telegram_bot_token: str | None = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str | None = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    youtube_max_uploads_per_day: int = field(
        default_factory=lambda: int(_env("YOUTUBE_MAX_UPLOADS_PER_DAY", "5") or 5))
    youtube_daily_quota: int = field(default_factory=lambda: int(_env("YOUTUBE_DAILY_QUOTA", "10000") or 10000))
    upload_mode: str = field(default_factory=lambda: (_env("UPLOAD_MODE", "manual") or "manual").lower())
    tts_usd_per_min: float = field(default_factory=lambda: float(_env("TTS_USD_PER_MIN", "0.015") or 0.015))
    whisper_usd_per_min: float = field(default_factory=lambda: float(_env("WHISPER_USD_PER_MIN", "0.006") or 0.006))
    eur_per_usd: float = field(default_factory=lambda: float(_env("EUR_PER_USD", "0.90") or 0.90))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO") or "INFO")
    default_channel: str = field(default_factory=lambda: _env("DEFAULT_CHANNEL", "de") or "de")

    root: Path = APP_ROOT
    data_dir: Path = APP_ROOT / "data"
    output_dir: Path = APP_ROOT / "output"
    assets_dir: Path = APP_ROOT / "assets"
    channels_file: Path = APP_ROOT / "config" / "channels.yaml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pipeline.sqlite3"


def load_settings() -> Settings:
    return Settings()


def load_channels(path: Path | None = None) -> dict[str, ChannelConfig]:
    path = path or Settings().channels_file
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    channels: dict[str, ChannelConfig] = {}
    for key, c in raw["channels"].items():
        channels[key] = ChannelConfig(
            key=key,
            name=c["name"],
            language=c.get("language", key),
            locale_hint=c.get("locale_hint", ""),
            youtube_token=APP_ROOT / c.get("youtube_token", f"credentials/token_{key}.json"),
            youtube_category_id=str(c.get("youtube_category_id", "25")),
            contains_synthetic_media=bool(c.get("contains_synthetic_media", False)),
            tts_voice=c.get("tts_voice"),
            tts_instructions=c.get("tts_instructions", ""),
            espeak_voice=c.get("espeak_voice", c.get("language", "de")),
            default_hashtags=list(c.get("default_hashtags", [])),
            outro_text=c.get("outro_text", ""),
            max_new_topics_per_run=int(c.get("max_new_topics_per_run", 10)),
            max_videos_per_run=int(c.get("max_videos_per_run", 7)),
            publish_slots=list(c.get("publish_slots", [])),
            sources=[SourceConfig(**s) for s in c.get("sources", [])],
            background_queries=list(c.get("background_queries", [])),
        )
    return channels


def get_channel(key: str) -> ChannelConfig:
    channels = load_channels()
    if key not in channels:
        raise KeyError(f"Unbekannter Kanal '{key}'. Verfügbar: {', '.join(channels)}")
    return channels[key]
