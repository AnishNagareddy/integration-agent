"""Settings — one place for every knob, loaded from `.env`.

The app is LIVE-only: it talks to a real LLM (Claude) and does real web research,
so there is no offline/mock mode. We **fail fast** with a clear message if a
required key is missing. (Tests don't use this — they inject a small fake LLM.)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into the real environment so libraries that read os.environ themselves
# (the Anthropic SDK) can see the keys.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


class Settings(BaseSettings):
    # Field `llm_provider` reads env var LLM_PROVIDER, `data_dir` reads DATA_DIR, etc.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The REASONING llm stays model-agnostic — swap Claude↔GPT↔Gemini via these two.
    # (Web search is Claude-specific for now; see providers/websearch.py.)
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5"
    data_dir: Path = Path("./.data")

    # --- derived paths ---
    @property
    def catalog_db(self) -> Path:
        return self.data_dir / "catalog.sqlite"  # what integrations exist

    @property
    def checkpoints_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"  # LangGraph state + conversation

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"  # generated connector code lands here on register

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def require_keys(self) -> None:
        """Fail fast (clear message) if we can't run live. Called by the app entrypoint,
        NOT at import — so tests can import freely without a key."""
        missing = [k for k in ("ANTHROPIC_API_KEY",) if not os.getenv(k)]
        if missing:
            raise RuntimeError(
                f"Missing required env var(s): {', '.join(missing)}.\n"
                "Copy .env.example to .env and fill them in — Claude powers both the "
                "reasoning and the web-search tool, so ANTHROPIC_API_KEY is required."
            )


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it (a simple singleton)."""
    return Settings()
