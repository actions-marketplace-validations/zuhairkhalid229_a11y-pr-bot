"""Runtime configuration, loaded from the environment (.env locally)."""

from __future__ import annotations

import base64
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitHub App
    github_app_id: str
    github_webhook_secret: str
    github_private_key: str
    github_api_url: str = "https://api.github.com"

    # Google Cloud
    gcp_project_id: str
    firestore_database: str = "(default)"

    # Cloud Tasks
    enable_cloud_tasks: bool = False
    tasks_location: str = "europe-west1"
    tasks_queue: str = "a11y-scans"
    # Base URL of the worker service; /tasks/scan and /tasks/fallback hang off it.
    worker_base_url: str = ""
    tasks_invoker_sa: str = ""
    # How long to wait for a preview deployment before giving up on the scan.
    preview_wait_minutes: int = 15

    # Fernet key (SecretBox.generate_key()) for per-repo secrets in Firestore.
    app_encryption_key: str = ""

    # Gemini (worker only). Empty key disables source mapping; scans still run.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    mapper_max_findings: int = 10

    # Comma-separated allowed browser origins for /api (the Vercel dashboard).
    dashboard_origins: str = "http://localhost:3000"

    # Behaviour
    check_run_name: str = "Accessibility (WCAG 2.2 AA)"
    dedupe_ttl_days: int = 7
    log_level: str = "INFO"

    @property
    def private_key_pem(self) -> str:
        """Accept either a raw PEM (with real or escaped newlines) or base64.

        Cloud Run env vars cannot hold real newlines, so the PEM arrives either
        base64-encoded or with literal backslash-n. Normalise both here.
        """
        raw = self.github_private_key.strip()
        if "BEGIN" not in raw:
            raw = base64.b64decode(raw).decode("utf-8")
        return raw.replace("\n", "\n")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
