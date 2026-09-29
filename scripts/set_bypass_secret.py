"""Store a Vercel Protection Bypass token for one repo.

    python scripts/set_bypass_secret.py <installation_id> <repo_id> <token>

Runs with your ADC credentials (gcloud auth application-default login) against
the project in .env. This is the Day 6 dashboard's write path, in CLI form, so
your own test repo can be scanned today.

Find the values:
  installation_id  GitHub -> Settings -> Applications -> the app -> URL ends in /installations/<id>
  repo_id          gh api repos/<owner>/<repo> --jq .id
  token            Vercel -> Project -> Settings -> Deployment Protection ->
                   Protection Bypass for Automation -> Generate
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.crypto import SecretBox
from app.store.firestore import Store


async def main(installation_id: int, repo_id: int, token: str) -> None:
    settings = get_settings()
    box = SecretBox(settings.app_encryption_key)
    if not box.enabled:
        sys.exit("APP_ENCRYPTION_KEY is not set in .env; generate one with SecretBox.generate_key()")

    store = Store(settings)
    try:
        await store.set_repo_config(installation_id, repo_id, {"vercel_bypass_secret": box.encrypt(token)})
    finally:
        await store.close()
    print(f"stored encrypted bypass token for installation {installation_id} repo {repo_id}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))
