"""Settings read from environment. Everything has a sane default except the admin password."""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _persistent_secret(data_dir: Path, name: str, factory) -> str:
    """Generate a secret once and keep it in the data dir, so a fresh install needs zero crypto setup."""
    path = data_dir / name
    if path.exists():
        return path.read_text().strip()
    value = factory()
    path.write_text(value)
    path.chmod(0o600)
    return value


@dataclass
class Settings:
    data_dir: Path
    workspace: Path
    host: str = "0.0.0.0"
    port: int = 8080
    admin_password: str = ""
    secret_key: str = ""  # Fernet key for stored secrets
    session_key: str = ""
    public_url: str = "http://localhost:8080"
    computer_url: str = "http://localhost:7000"
    computer_token: str = ""
    screens: int = 4
    default_model: str = "openai/gpt-5-mini"
    reviewer_model: str = ""
    auto_review: bool = True
    max_steps: int = 40
    absence_days: int = 14
    telegram_token: str = ""
    telegram_allowed: set[int] = field(default_factory=set)
    stt_model: str = ""
    tts_model: str = ""
    tts_voice: str = "alloy"
    brave_api_key: str = ""
    searxng_url: str = ""
    timezone: str = "UTC"
    owner_name: str = "the owner"


def load() -> Settings:
    data_dir = Path(_env("DROK_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(_env("DROK_WORKSPACE", "./workspace")).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    allowed = {int(x) for x in _env("TELEGRAM_ALLOWED_USER_IDS").replace(" ", "").split(",") if x}
    port = int(_env("DROK_PORT", "8080"))
    return Settings(
        data_dir=data_dir,
        workspace=workspace,
        host=_env("DROK_HOST", "0.0.0.0"),
        port=port,
        admin_password=_env("DROK_ADMIN_PASSWORD"),
        secret_key=_env("DROK_SECRET_KEY")
        or _persistent_secret(data_dir, ".secret.key", lambda: Fernet.generate_key().decode()),
        session_key=_persistent_secret(data_dir, ".session.key", lambda: secrets.token_urlsafe(32)),
        public_url=_env("DROK_PUBLIC_URL", f"http://localhost:{port}").rstrip("/"),
        computer_url=_env("COMPUTER_URL", "http://localhost:7000").rstrip("/"),
        computer_token=_env("COMPUTER_TOKEN"),
        screens=int(_env("DROK_SCREENS", "4")),
        default_model=_env("DROK_DEFAULT_MODEL", "openai/gpt-5-mini"),
        reviewer_model=_env("DROK_REVIEWER_MODEL"),
        auto_review=_env("DROK_AUTO_REVIEW", "on").lower() not in ("off", "0", "false", "no"),
        max_steps=int(_env("DROK_MAX_STEPS", "40")),
        absence_days=int(_env("DROK_ABSENCE_DAYS", "14")),
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_allowed=allowed,
        stt_model=_env("DROK_STT_MODEL"),
        tts_model=_env("DROK_TTS_MODEL"),
        tts_voice=_env("DROK_TTS_VOICE", "alloy"),
        brave_api_key=_env("BRAVE_API_KEY"),
        searxng_url=_env("SEARXNG_URL").rstrip("/"),
        timezone=_env("TZ", "UTC") or "UTC",
        owner_name=_env("DROK_OWNER_NAME", "the owner"),
    )
