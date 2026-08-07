"""Central runtime configuration with backward-compatible environment names."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _value(env: Mapping[str, str], name: str, default: str = "") -> str:
    raw = env.get(name)
    return default if raw is None else str(raw).strip()


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _value(env, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {value}")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _value(env, name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {value}")
    return value


def _url(env: Mapping[str, str], name: str, default: str, *, optional: bool = False) -> str:
    value = _value(env, name, default)
    if optional and not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an http(s) URL, got {value!r}")
    return value.rstrip("/")


def _recipients(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class LLMSettings:
    nim_base_url: str
    nim_api_key: str
    tier2_model: str
    tier3_model: str
    insight_model: str
    ollama_base_url: str
    ollama_api_key: str
    ollama_model: str
    timeout: float
    tier2_timeout: float
    blog_timeout: float
    narrative_max_tokens: int
    cerebras_api_key: str
    groq_api_key_1: str
    groq_api_key_2: str


@dataclass(frozen=True)
class EmbeddingSettings:
    api_url: str
    api_key: str
    model: str
    dimension: int
    local_model: str


@dataclass(frozen=True)
class YouTubeSettings:
    proxy: str
    cookies_file: str


@dataclass(frozen=True)
class EmailSettings:
    user: str
    password: str
    recipients: tuple[str, ...]
    smtp_host: str
    smtp_port: int


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    chat_id: str
    ollama_base_url: str
    ollama_api_key: str
    ollama_model: str
    mcp_transport: str
    max_tool_iterations: int
    timeout: float
    message_limit: int
    log_level: str


@dataclass(frozen=True)
class StorageSettings:
    project_root: Path
    sqlite_path: Path
    lancedb_dir: Path
    obsidian_vault: Path


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    embedding: EmbeddingSettings
    youtube: YouTubeSettings
    email: EmailSettings
    telegram: TelegramSettings
    storage: StorageSettings


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build and validate settings from a mapping; defaults preserve legacy behavior."""
    env = os.environ if environ is None else environ
    tier3_model = _value(env, "TIER3_MODEL", "deepseek-ai/deepseek-v4-flash")
    gmail_user = _value(env, "GMAIL_USER") or _value(env, "SMTP_USER")
    gmail_password = _value(env, "GMAIL_APP_PASSWORD") or _value(env, "SMTP_PASS")
    recipient_raw = _value(env, "EMAIL_TO") or gmail_user
    ollama_url = _url(env, "OLLAMA_PRO_BASE_URL", "https://ollama.com")
    telegram_ollama_url = _url(env, "OLLAMA_PRO_BASE_URL", "https://ollama.com/v1")
    ollama_key = _value(env, "OLLAMA_PRO_API_KEY")

    llm = LLMSettings(
        nim_base_url=_url(env, "NIM_BASE_URL", "http://localhost:8000"),
        nim_api_key=_value(env, "NIM_API_KEY", "proxy-rotates-keys"),
        tier2_model=_value(env, "TIER2_MODEL", "deepseek-ai/deepseek-v4-flash"),
        tier3_model=tier3_model,
        insight_model=_value(env, "INSIGHT_MODEL", tier3_model),
        ollama_base_url=ollama_url,
        ollama_api_key=ollama_key,
        ollama_model=_value(env, "OLLAMA_PRO_MODEL", "deepseek-v4-flash:0731-cloud"),
        timeout=_positive_float(env, "LLM_TIMEOUT", 300.0),
        tier2_timeout=_positive_float(env, "TIER2_TIMEOUT", 300.0),
        blog_timeout=_positive_float(env, "BLOG_TIMEOUT", 180.0),
        narrative_max_tokens=_positive_int(env, "NARRATIVE_MAX_TOKENS", 8192),
        cerebras_api_key=_value(env, "CEREBRAS_API_KEY"),
        groq_api_key_1=_value(env, "GROQ_API_KEY_1"),
        groq_api_key_2=_value(env, "GROQ_API_KEY_2"),
    )
    embedding = EmbeddingSettings(
        api_url=_url(env, "EMBEDDING_API_URL", "", optional=True),
        api_key=_value(env, "EMBEDDING_API_KEY"),
        model=_value(env, "EMBEDDING_MODEL", "nomic-embed-text"),
        dimension=_positive_int(env, "EMBEDDING_DIM", 256),
        local_model=_value(env, "EMBEDDING_LOCAL_MODEL"),
    )
    email = EmailSettings(
        user=gmail_user,
        password=gmail_password,
        recipients=_recipients(recipient_raw),
        smtp_host=_value(env, "SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_positive_int(env, "SMTP_PORT", 465),
    )
    transport = _value(env, "MCP_TRANSPORT", "inproc").lower()
    if transport not in {"inproc", "stdio"}:
        raise ValueError(f"MCP_TRANSPORT must be 'inproc' or 'stdio', got {transport!r}")
    telegram = TelegramSettings(
        bot_token=_value(env, "TELEGRAM_BOT_TOKEN"),
        chat_id=_value(env, "TELEGRAM_CHAT_ID"),
        ollama_base_url=telegram_ollama_url,
        ollama_api_key=ollama_key,
        # Telegram historically used a different default when the shared env was unset.
        ollama_model=_value(env, "OLLAMA_PRO_MODEL", "llama3.1:70b"),
        mcp_transport=transport,
        max_tool_iterations=_positive_int(env, "MAX_TOOL_ITER", 6),
        timeout=_positive_float(env, "OLLAMA_TIMEOUT_S", 120.0),
        message_limit=4000,
        log_level=_value(env, "LOG_LEVEL", "INFO").upper(),
    )
    storage = StorageSettings(
        project_root=PROJECT_ROOT,
        sqlite_path=PROJECT_ROOT / "data" / "macro_knowledge.db",
        lancedb_dir=PROJECT_ROOT / "data" / "lancedb_store",
        obsidian_vault=PROJECT_ROOT / "obsidian_vault",
    )
    return Settings(
        llm=llm,
        embedding=embedding,
        youtube=YouTubeSettings(
            proxy=_value(env, "YOUTUBE_PROXY"),
            cookies_file=_value(env, "YOUTUBE_COOKIES_FILE", "cookies.txt"),
        ),
        email=email,
        telegram=telegram,
        storage=storage,
    )


settings = load_settings()
