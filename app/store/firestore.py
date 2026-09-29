"""Firestore access: webhook idempotency, scan state, findings, repo config.

GitHub delivers webhooks *at least* once -- redeliveries after a timeout are
normal, and a duplicate here means a duplicate PR comment later. We claim each
X-GitHub-Delivery id with an atomic create(); losing the race means someone
else already owns this delivery and we drop it.

Scan state changes go through transactions so two instances handling the PR
event and the deployment event concurrently cannot both conclude "the other
half is missing". The merge logic itself lives in scan_state.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import firestore
from google.cloud.firestore import async_transactional

from app.config import Settings
from app.logging_config import get_logger
from app.store.scan_state import Merge, ScanState, apply_half, transition

log = get_logger(__name__)

DELIVERIES = "deliveries"
SCANS = "scans"
FINDINGS = "findings"
INSTALLATIONS = "installations"
REPOS = "repos"
PRS = "prs"
POSTED = "posted"
USAGE = "usage"
MEMBERS = "members"


class Store:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db = firestore.AsyncClient(
            project=settings.gcp_project_id,
            database=settings.firestore_database,
        )

    @property
    def db(self) -> firestore.AsyncClient:
        return self._db

    # -- webhook idempotency -------------------------------------------------

    async def claim_delivery(self, delivery_id: str, *, event: str, action: str | None) -> bool:
        """Atomically claim a webhook delivery. False means already processed.

        `expires_at` is there for a Firestore TTL policy -- set one on this
        field or the collection grows forever:
          gcloud firestore fields ttls update expires_at \
            --collection-group=deliveries --enable-ttl
        """
        doc = self._db.collection(DELIVERIES).document(delivery_id)
        try:
            await doc.create(
                {
                    "event": event,
                    "action": action,
                    "received_at": firestore.SERVER_TIMESTAMP,
                    "expires_at": datetime.now(UTC) + timedelta(days=self._settings.dedupe_ttl_days),
                }
            )
            return True
        except gcloud_exceptions.AlreadyExists:
            log.info("duplicate_delivery_dropped", delivery_id=delivery_id, gh_event=event)
            return False

    # -- scan state machine --------------------------------------------------

    async def register_half(
        self,
        scan_id: str,
        *,
        base: dict[str, Any],
        pr: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
    ) -> Merge:
        """Merge one half (PR or preview) into the scan doc, transactionally.

        `base` carries the identity fields (installation_id, repo, sha) so a
        doc created by either half is complete. Returns the merge outcome;
        `should_enqueue` is True for exactly one caller per scan.
        """
        ref = self._db.collection(SCANS).document(scan_id)

        @async_transactional
        async def _txn(txn: firestore.AsyncTransaction) -> Merge:
            snapshot = await ref.get(transaction=txn)
            existing = snapshot.to_dict() if snapshot.exists else None
            merged = apply_half(existing, pr=pr, preview=preview)
            data = {**base, **merged.doc, "updated_at": firestore.SERVER_TIMESTAMP}
            if existing is None:
                data["created_at"] = firestore.SERVER_TIMESTAMP
            txn.set(ref, data, merge=True)
            return merged

        return await _txn(self._db.transaction())

    async def transition_scan(
        self,
        scan_id: str,
        *,
        allowed_from: set[ScanState],
        to: ScanState,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Move a scan between states if legal. False means no-op."""
        ref = self._db.collection(SCANS).document(scan_id)

        @async_transactional
        async def _txn(txn: firestore.AsyncTransaction) -> bool:
            snapshot = await ref.get(transaction=txn)
            existing = snapshot.to_dict() if snapshot.exists else None
            updated = transition(existing, allowed_from=allowed_from, to=to)
            if updated is None:
                return False
            txn.set(
                ref,
                {**(extra or {}), "state": to.value, "updated_at": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
            return True

        moved = await _txn(self._db.transaction())
        log.info("scan_transition", scan_id=scan_id, to=to.value, applied=moved)
        return moved

    async def update_scan(self, scan_id: str, data: dict[str, Any]) -> None:
        await (
            self._db.collection(SCANS)
            .document(scan_id)
            .set({**data, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        )

    async def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        snapshot = await self._db.collection(SCANS).document(scan_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def write_findings(
        self, scan_id: str, findings: list[dict[str, Any]], *, installation_id: int | None = None
    ) -> None:
        """One doc per finding keyed by fingerprint. Batched: 500 writes max
        per batch, and a page with more findings than that has bigger problems.

        installation_id is denormalised onto each finding so firestore.rules can
        authorise a findings query without a get() on the parent scan."""
        collection = self._db.collection(SCANS).document(scan_id).collection(FINDINGS)
        for start in range(0, len(findings), 450):
            batch = self._db.batch()
            for finding in findings[start : start + 450]:
                doc = finding if installation_id is None else {**finding, "installation_id": installation_id}
                batch.set(collection.document(finding["fingerprint"]), doc)
            await batch.commit()

    # -- quota ---------------------------------------------------------------

    async def get_usage(self, usage_id: str) -> dict[str, Any] | None:
        snapshot = await self._db.collection(USAGE).document(usage_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def get_plan(self, installation_id: int) -> str | None:
        snapshot = await self._db.collection(INSTALLATIONS).document(str(installation_id)).get()
        return (snapshot.to_dict() or {}).get("plan") if snapshot.exists else None

    async def count_private_scan(self, usage_id: str, repo_id: int) -> None:
        """Atomic meter bump. Increment/ArrayUnion are server-side transforms,
        so concurrent scans cannot lose a count."""
        await (
            self._db.collection(USAGE)
            .document(usage_id)
            .set(
                {
                    "private_scans_run": firestore.Increment(1),
                    "private_repo_ids": firestore.ArrayUnion([repo_id]),
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        )

    # -- dashboard membership (written by POST /api/link) --------------------

    async def set_members(self, installation_id: int, uid: str, data: dict[str, Any]) -> None:
        await (
            self._db.collection(INSTALLATIONS)
            .document(str(installation_id))
            .collection(MEMBERS)
            .document(uid)
            .set({**data, "uid": uid, "linked_at": firestore.SERVER_TIMESTAMP}, merge=True)
        )

    async def is_member(self, installation_id: int, uid: str) -> bool:
        snapshot = await (
            self._db.collection(INSTALLATIONS)
            .document(str(installation_id))
            .collection(MEMBERS)
            .document(uid)
            .get()
        )
        return snapshot.exists

    async def list_repos(self, installation_id: int) -> list[dict[str, Any]]:
        collection = self._db.collection(INSTALLATIONS).document(str(installation_id)).collection(REPOS)
        return [snap.to_dict() | {"repo_id": snap.id} async for snap in collection.stream()]

    # -- per-PR posting history (Day 5) -------------------------------------

    async def list_posted(self, pr_key: str) -> list[dict[str, Any]]:
        collection = self._db.collection(PRS).document(pr_key).collection(POSTED)
        return [snap.to_dict() async for snap in collection.stream()]

    async def record_posted(self, pr_key: str, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        collection = self._db.collection(PRS).document(pr_key).collection(POSTED)
        batch = self._db.batch()
        for e in entries:
            batch.set(
                collection.document(e["fingerprint"]),
                {**e, "posted_at": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
        await batch.commit()

    async def mark_resolved(self, pr_key: str, fingerprints: list[str], *, head_sha: str) -> None:
        if not fingerprints:
            return
        collection = self._db.collection(PRS).document(pr_key).collection(POSTED)
        batch = self._db.batch()
        for fp in fingerprints:
            batch.set(
                collection.document(fp),
                {"resolved_at": firestore.SERVER_TIMESTAMP, "resolved_sha": head_sha},
                merge=True,
            )
        await batch.commit()

    # -- installations & repo config ----------------------------------------

    async def record_installation(self, installation_id: int, data: dict[str, Any]) -> None:
        await (
            self._db.collection(INSTALLATIONS)
            .document(str(installation_id))
            .set({**data, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        )

    async def get_repo_config(self, installation_id: int, repo_id: int) -> dict[str, Any] | None:
        snapshot = await (
            self._db.collection(INSTALLATIONS)
            .document(str(installation_id))
            .collection(REPOS)
            .document(str(repo_id))
            .get()
        )
        return snapshot.to_dict() if snapshot.exists else None

    async def set_repo_config(self, installation_id: int, repo_id: int, data: dict[str, Any]) -> None:
        await (
            self._db.collection(INSTALLATIONS)
            .document(str(installation_id))
            .collection(REPOS)
            .document(str(repo_id))
            .set({**data, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        )

    async def close(self) -> None:
        self._db.close()


def scan_id_for(installation_id: int, repo_full_name: str, head_sha: str) -> str:
    """Stable document id. Re-pushing the same SHA must not create a new scan."""
    return f"{installation_id}_{repo_full_name.replace('/', '__')}_{head_sha}"
