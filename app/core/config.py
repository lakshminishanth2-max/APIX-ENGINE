"""Typed application settings (Pydantic V2 / pydantic-settings).

Every tunable that differs between laptop, CI and the NSO production cluster
lives here and nowhere else.  Nothing in the codebase reads ``os.environ``
directly, so a misconfigured deployment fails loudly at import time rather than
silently halfway through a collection run.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Final, Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

Environment = Literal["local", "ci", "staging", "production"]

#: DGCA high-density route basket used for the headline APIx print.
DEFAULT_ROUTE_BASKET: Final[tuple[str, ...]] = (
    "DEL-BOM",
    "DEL-BLR",
    "BOM-BLR",
    "DEL-CCU",
    "BLR-HYD",
    "MAA-DEL",
)

#: Booking windows (days to departure) sampled every collection cycle.
DEFAULT_BOOKING_WINDOWS: Final[tuple[int, ...]] = (1, 7, 15, 30, 45)


class Settings(BaseSettings):
    """Process-wide configuration for the Python data and statistical core."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="APIX_",
        extra="ignore",
        frozen=True,
    )

    # -- identity ----------------------------------------------------------- #
    app_name: str = "APIx Data & Statistical Core"
    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # -- datastores --------------------------------------------------------- #
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://apix:apix@localhost:5432/apix",  # type: ignore[arg-type]
        description="asyncpg DSN; the sync driver is derived for Alembic.",
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_echo: bool = False
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")  # type: ignore[arg-type]
    celery_broker_url: RedisDsn = Field(default="redis://localhost:6379/1")  # type: ignore[arg-type]
    celery_result_backend: RedisDsn = Field(default="redis://localhost:6379/2")  # type: ignore[arg-type]

    # -- gateway trust boundary --------------------------------------------- #
    #: Shared secret the Express gateway presents on every internal call.  The
    #: core is never exposed to the public internet; the gateway is.
    internal_api_key: str = Field(default="dev-internal-key-change-me", min_length=8)
    internal_api_key_header: str = "X-APIx-Internal-Key"

    # -- compliance --------------------------------------------------------- #
    user_agent: str = "APIxBot/1.0 (+https://mospi.gov.in/apix; apix-ops@mospi.gov.in)"
    respect_robots: bool = True
    robots_fail_closed: bool = True
    robots_ttl_s: float = 3_600.0
    default_requests_per_second: float = Field(default=0.5, gt=0.0, le=50.0)
    default_burst: float = Field(default=2.0, ge=1.0, le=100.0)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_recovery_timeout_s: float = Field(default=300.0, gt=0.0)
    #: Hard stop.  When a bot challenge is seen the source is halted and only an
    #: operator can resume it - the platform never attempts evasion.
    halt_on_bot_challenge: bool = True

    # -- browser tier (Playwright) ------------------------------------------ #
    playwright_headless: bool = True
    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    playwright_nav_timeout_ms: int = Field(default=45_000, ge=1_000, le=180_000)
    playwright_max_contexts: int = Field(default=4, ge=1, le=32)
    playwright_locale: str = "en-IN"
    playwright_timezone: str = "Asia/Kolkata"

    # -- collection --------------------------------------------------------- #
    route_basket: tuple[str, ...] = DEFAULT_ROUTE_BASKET
    booking_windows: tuple[int, ...] = DEFAULT_BOOKING_WINDOWS
    collection_currency: str = "INR"
    collection_timezone: str = "Asia/Kolkata"
    max_quotes_per_request: int = Field(default=500, ge=1, le=5_000)

    # -- statistics --------------------------------------------------------- #
    methodology_version: str = "APIX-IDX-1.0.0"
    index_base_period: str = "2025-04"
    outlier_threshold: Decimal = Decimal("3.5")
    min_pairs_per_stratum: int = Field(default=5, ge=1)
    min_weight_coverage: Decimal = Decimal("0.60")

    # -- observability ------------------------------------------------------ #
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("route_basket", mode="before")
    @classmethod
    def _split_routes(cls, value: Any) -> Any:
        if isinstance(value, str):
            return tuple(part.strip().upper() for part in value.split(",") if part.strip())
        return value

    @field_validator("booking_windows", mode="before")
    @classmethod
    def _split_windows(cls, value: Any) -> Any:
        if isinstance(value, str):
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value

    @property
    def sync_database_url(self) -> str:
        """psycopg DSN for Alembic, derived from the async one."""
        return str(self.database_url).replace("+asyncpg", "")

    @property
    def route_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (route.split("-")[0], route.split("-")[1]) for route in self.route_basket if "-" in route
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton; also usable as a FastAPI dependency."""
    return Settings()


SettingsDep = Annotated[Settings, Field(description="Injected application settings")]
