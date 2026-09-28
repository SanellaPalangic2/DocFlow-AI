"""Configuration loaded from environment variables (.env).

Secrets are only ever read from the environment - never hard-coded.
Nothing in this module is ever rendered to the browser.
"""
import os
import secrets


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self) -> None:
        # Flask session signing key. A random one is generated if unset so the
        # app still runs locally, but sessions won't survive a restart.
        self.SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
        self.SECRET_KEY_IS_EPHEMERAL = not os.getenv("SECRET_KEY")

        # LLM
        self.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5").strip()
        self.LLM_API_URL = os.getenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
        self.LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 60)

        # Features
        self.DEMO_MODE = _bool("DEMO_MODE", False)

        # Deployment
        # If set, every page requires this password (HTTP Basic Auth, any username).
        # Use it on any public deployment so strangers can't spend your API credits.
        self.APP_PASSWORD = os.getenv("APP_PASSWORD", "")
        # Cookies only over HTTPS. On by default on Render (which sets RENDER=true).
        self.SESSION_COOKIE_SECURE = _bool("SECURE_COOKIES", bool(os.getenv("RENDER")))

        # Limits
        self.MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 5)
        self.MAX_CONTENT_LENGTH = self.MAX_UPLOAD_MB * 1024 * 1024  # enforced by Flask
        self.MAX_PDF_PAGES = _int("MAX_PDF_PAGES", 30)
        self.MAX_LLM_CHARS = _int("MAX_LLM_CHARS", 30000)

        # Storage
        self.DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join("instance", "docflow.sqlite3"))

    def to_flask(self) -> dict:
        return {k: v for k, v in vars(self).items() if k.isupper()}
