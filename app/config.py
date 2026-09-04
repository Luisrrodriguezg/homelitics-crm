"""Settings, loaded from environment / .env."""
import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- database ---
    # Runtime uses the TRANSACTION pooler (6543). See db.py for why that forces
    # statement_cache_size=0.
    database_url: str = Field(alias="DATABASE_URL")
    database_url_migrate: str | None = Field(default=None, alias="DATABASE_URL_MIGRATE")

    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    # --- auth ---
    supabase_project_ref: str = Field(alias="SUPABASE_PROJECT_REF")
    # Blank on modern projects (asymmetric keys -> JWKS). Only set for legacy HS256.
    supabase_jwt_secret: str | None = Field(default=None, alias="SUPABASE_JWT_SECRET")
    supabase_jwt_audience: str = Field(default="authenticated", alias="SUPABASE_JWT_AUDIENCE")

    # --- app ---
    enable_scheduler: bool = Field(default=False, alias="ENABLE_SCHEDULER")
    inactivity_hours: int = Field(default=72, alias="INACTIVITY_HOURS")
    scheduler_interval_minutes: int = Field(default=60, alias="SCHEDULER_INTERVAL_MINUTES")
    # The domain-event outbox relay (jobs.relay_events). Runs on the same
    # ENABLE_SCHEDULER gate as the inactivity sweep.
    event_relay_seconds: int = Field(default=30, alias="EVENT_RELAY_SECONDS")
    # Agents publish availability in local time; slot maths happens in this zone.
    app_timezone: str = Field(default="America/Bogota", alias="APP_TIMEZONE")
    # When true, request_visit rejects a slot the agent has not published.
    # Default false so the existing overlap tests keep their meaning.
    enforce_availability: bool = Field(default=False, alias="ENFORCE_AVAILABILITY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")

    # --- local dev only ---
    # Skip Supabase JWT entirely and take the acting agent from X-Dev-Agent-Id.
    # Guarded in __init__: refused unless DATABASE_URL points at localhost/db.
    dev_auth_bypass: bool = Field(default=False, alias="DEV_AUTH_BYPASS")

    @field_validator("supabase_jwt_secret", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        # An empty SUPABASE_JWT_SECRET= line in .env must mean "use JWKS",
        # not "the secret is the empty string".
        return v or None

    @field_validator("database_url")
    @classmethod
    def _must_be_async(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the asyncpg driver, e.g. "
                "postgresql+asyncpg://... — on Supabase it must be the TRANSACTION "
                "pooler (port 6543), not the session pooler (5432) or the direct "
                "connection. A local Postgres (localhost/db) is also accepted for "
                "the docker-compose 'local' profile."
            )
        return v

    @property
    def _db_host_is_local(self) -> bool:
        host = self.database_url.split("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return host in {"localhost", "127.0.0.1", "db"}

    def model_post_init(self, __context) -> None:
        if self.dev_auth_bypass and not self._db_host_is_local:
            raise ValueError(
                "DEV_AUTH_BYPASS is on but DATABASE_URL does not point at a local "
                "database (localhost/127.0.0.1/db). Refusing to start — this would "
                "disable authentication against a real database."
            )
        if self.enable_scheduler and not self._db_host_is_local:
            # Not fatal: an EC2 box without pg_cron is a legitimate setup. But on
            # Supabase, migrations/005 hands the sweep and relay to pg_cron, and
            # running the in-process scheduler as well double-runs them.
            logging.getLogger(__name__).warning(
                "ENABLE_SCHEDULER=true against a non-local database. If this is "
                "Supabase, pg_cron already runs the sweep and the relay "
                "(migrations/005_cron_jobs.sql) — set ENABLE_SCHEDULER=false or "
                "follow-ups will be raised twice."
            )

    @property
    def supabase_url(self) -> str:
        return f"https://{self.supabase_project_ref}.supabase.co"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.jwt_issuer}/.well-known/jwks.json"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
