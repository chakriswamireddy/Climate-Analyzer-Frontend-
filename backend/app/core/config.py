"""Application settings (env-driven; docs/02 §15).

Field names map to ``SCREAMING_SNAKE`` env vars (pydantic-settings is
case-insensitive), e.g. ``mongodb_uri`` ← ``MONGODB_URI``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- MongoDB (docs/02 §15) ---
    mongodb_uri: str = "mongodb://localhost:27017/?directConnection=true"
    mongodb_db: str = "climate_analyzer"
    mongodb_timeout_ms: int = 5000
    # 0 disables the TTL (dev keeps fixtures); production ≥ 1825 (5 years, docs/02 §6.2)
    daily_weather_ttl_days: int = 0

    # --- App (docs/02 §15) ---
    admin_api_token: str = ""
    frontend_origin: str = "http://localhost:5173"
    algo_version: str = "1.0.0"
    baseline_period: str = "1991-2020"
    log_level: str = "INFO"
    log_format: str = "json"

    # --- Provider (docs/02 §5.3) ---
    openmeteo_base_url: str = "https://archive-api.open-meteo.com"
    geocoding_base_url: str = "https://geocoding-api.open-meteo.com"
    provider_connect_timeout_s: float = 5.0
    provider_timeout_s: float = 30.0
    provider_chunk_budget_s: float = 60.0
    provider_max_retries: int = 3
    provider_backoff_base_s: float = 1.0
    provider_retry_after_cap_s: float = 60.0
    provider_concurrency: int = 4
    provider_min_interval_s: float = 0.1
    provider_429_wait_s: float = 60.0
    provider_minute_weight_budget: float = 550.0
    provider_hourly_weight_budget: float = 4500.0
    provider_daily_weight_budget: float = 9500.0
    provider_weight_days_ref: float = 30.0
    provider_weight_vars_ref: float = 10.0
    breaker_failure_threshold: int = 5
    breaker_recovery_s: float = 30.0

    # --- Ingest (docs/02 §5.3, §5.6) ---
    chunk_years: int = 10
    bad_row_ratio_limit: float = 0.05
    ondemand_wait_seconds: int = 8
    response_cache_ttl_s: int = 60

    # --- Rate limits (docs/02 §11 — enforced from P3) ---
    rate_limit_analysis_per_min: int = 60
    rate_limit_region_per_min: int = 10
    rate_limit_admin_per_min: int = 5


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests build ``Settings(...)`` directly instead."""
    return Settings()
