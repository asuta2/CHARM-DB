from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    control_dsn: str = Field(alias="CHARMDB_CONTROL_DSN")
    target_dsn: str = Field(alias="CHARMDB_TARGET_DSN")
    target_host: str = Field(default="127.0.0.1", alias="CHARMDB_TARGET_HOST")
    target_port: int = Field(default=55432, alias="CHARMDB_TARGET_PORT")
    target_db: str = Field(default="charm_target", alias="CHARMDB_TARGET_DB")
    target_user: str = Field(default="charm_target", alias="CHARMDB_TARGET_USER")
    target_password: str = Field(alias="CHARMDB_TARGET_PASSWORD")
    target_allowlist: str = Field(default="localhost,127.0.0.1", alias="CHARMDB_TARGET_ALLOWLIST")
    artifact_dir: Path = Field(default=Path("artifacts"), alias="CHARMDB_ARTIFACT_DIR")
    benchmark_warmup_seconds: int = Field(default=5, ge=0, alias="CHARMDB_BENCHMARK_WARMUP_SECONDS")
    benchmark_duration_seconds: int = Field(
        default=30, ge=1, alias="CHARMDB_BENCHMARK_DURATION_SECONDS"
    )
    benchmark_concurrency: int = Field(
        default=4, ge=1, le=128, alias="CHARMDB_BENCHMARK_CONCURRENCY"
    )
    benchmark_seed: int = Field(default=20260712, alias="CHARMDB_BENCHMARK_SEED")
    min_host_free_bytes: int = Field(
        default=5 * 1024**3, ge=1024**3, alias="CHARMDB_MIN_HOST_FREE_BYTES"
    )
    min_target_free_bytes: int = Field(
        default=5 * 1024**3, ge=1024**3, alias="CHARMDB_MIN_TARGET_FREE_BYTES"
    )
    max_container_memory_fraction: float = Field(
        default=0.95,
        gt=0.0,
        le=1.0,
        alias="CHARMDB_MAX_CONTAINER_MEMORY_FRACTION",
    )

    @field_validator("target_host")
    @classmethod
    def target_must_be_allowlisted(cls, value: str, info: object) -> str:
        # Full validation follows construction because Pydantic field order is not an API.
        if not value.strip():
            raise ValueError("target host cannot be empty")
        return value

    def assert_target_allowed(self) -> None:
        allowed = {
            item.strip().lower() for item in self.target_allowlist.split(",") if item.strip()
        }
        if self.target_host.lower() not in allowed:
            raise ValueError(f"target host {self.target_host!r} is not in CHARMDB_TARGET_ALLOWLIST")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.assert_target_allowed()
    return settings
