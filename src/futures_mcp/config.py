"""Runtime settings, read from the environment (and an optional ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Per-user settings, read wherever the server is started from (Claude Code and
#: Desktop launch it from arbitrary working directories). A ``.env`` in the working
#: directory overrides it, and real environment variables override both.
USER_ENV_FILE = Path.home() / ".futures-mcp" / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(USER_ENV_FILE, ".env"),
                                      env_file_encoding="utf-8", extra="ignore")

    #: TradingView ``sessionid`` cookie. Empty = anonymous capture on the default chart.
    #: Set it to capture on your own saved layout (indicators, colours, drawings).
    tradingview_session_id: SecretStr = SecretStr("")
    #: Chart URL used in session mode, e.g. ``https://www.tradingview.com/chart/<layout-id>/``.
    tradingview_url: str = "https://www.tradingview.com/chart/"

    #: Mode a capture uses when the caller doesn't pick one. Unset = session when a
    #: cookie is configured, otherwise anonymous.
    default_mode: Literal["anonymous", "session"] | None = Field(
        default=None, alias="FUTURES_MCP_DEFAULT_MODE")

    #: Where captures, marked charts and the bar cache are written.
    data_dir: Path = Field(default=Path("data"), alias="FUTURES_MCP_DATA_DIR")
    #: Folder holding the (private, un-versioned) range detector module.
    detector_dir: Path = Field(default=Path("private"), alias="FUTURES_MCP_DETECTOR_DIR")
    #: Path to the tesseract binary if it is not on PATH.
    tesseract_cmd: str | None = None

    #: Seconds a bar fetch for a window that is still open may be served from cache.
    live_cache_ttl: int = Field(default=300, alias="FUTURES_MCP_LIVE_CACHE_TTL")
    #: Run Chromium with a visible window (useful when debugging a capture).
    headful: bool = Field(default=False, alias="FUTURES_MCP_HEADFUL")

    @property
    def session_mode(self) -> bool:
        return bool(self.tradingview_session_id.get_secret_value())

    @property
    def capture_mode(self) -> str:
        return self.default_mode or ("session" if self.session_mode else "anonymous")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
