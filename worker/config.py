from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="WORKER_"
    )

    # How long we give the page to reach `load`. Preview deploys on cold
    # serverless functions can take a while.
    navigation_timeout_ms: int = 30_000
    # After `load`, how long to wait for network idle before giving up on it.
    # SPAs with polling never go idle; this is a bound, not a requirement.
    settle_timeout_ms: int = 3_000
    # axe.run on a large page.
    axe_timeout_ms: int = 60_000

    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: str = "a11y-pr-bot/0.1 (+https://github.com/apps/a11y-pr-bot)"

    # Scans in flight per process. Match Cloud Run --concurrency to this.
    max_concurrent_scans: int = 2
    # Cloud Run gives you no GPU and shared CPU; headless chromium is happiest
    # with these. Kept as config so local runs can turn --headless=new on/off.
    chromium_args: list[str] = [
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
    ]

    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
