"""Fetch what the mapper needs from GitHub: the PR's changed files with their
diff hunks, and the full content of each at the head commit.

Contents API is used rather than the raw diff because we need whole files --
the failing element may live in an unchanged region of a changed file, and the
model needs the surrounding JSX to write a correct patch.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from app.github.client import GitHubClient
from app.logging_config import get_logger
from worker.mapper.diff import right_side_lines

log = get_logger(__name__)

SOURCE_EXTENSIONS = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".mdx", ".html")
MAX_FILE_BYTES = 200_000
MAX_FILES = 40
_PER_PAGE = 100


@dataclass
class ChangedFile:
    path: str
    status: str
    diff_lines: set[int] = field(default_factory=set)
    content: str | None = None


@dataclass
class PullRequestFiles:
    files: dict[str, ChangedFile] = field(default_factory=dict)

    @property
    def contents(self) -> dict[str, str]:
        return {p: f.content for p, f in self.files.items() if f.content is not None}


async def fetch_pr_files(
    client: GitHubClient, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
) -> PullRequestFiles:
    result = PullRequestFiles()

    page = 1
    while True:
        response = await client.request(
            "GET",
            f"/repos/{repo_full_name}/pulls/{pr_number}/files?per_page={_PER_PAGE}&page={page}",
            installation_id,
        )
        if response.status_code != 200:
            log.error("pr_files_failed", status=response.status_code, body=response.text[:300])
            return result
        batch = response.json()
        for entry in batch:
            path = entry["filename"]
            if entry.get("status") == "removed" or not path.endswith(SOURCE_EXTENSIONS):
                continue
            result.files[path] = ChangedFile(
                path=path, status=entry.get("status", ""), diff_lines=right_side_lines(entry.get("patch"))
            )
            if len(result.files) >= MAX_FILES:
                break
        if len(batch) < _PER_PAGE or len(result.files) >= MAX_FILES:
            break
        page += 1

    for path, changed in result.files.items():
        changed.content = await _fetch_content(client, installation_id, repo_full_name, path, head_sha)

    log.info(
        "pr_files_fetched",
        pr=pr_number,
        files=len(result.files),
        with_content=sum(1 for f in result.files.values() if f.content is not None),
    )
    return result


async def _fetch_content(
    client: GitHubClient, installation_id: int, repo_full_name: str, path: str, ref: str
) -> str | None:
    response = await client.request(
        "GET", f"/repos/{repo_full_name}/contents/{path}?ref={ref}", installation_id
    )
    if response.status_code != 200:
        log.warning("content_fetch_failed", path=path, status=response.status_code)
        return None
    body = response.json()
    if body.get("encoding") != "base64" or body.get("size", 0) > MAX_FILE_BYTES:
        log.info("content_skipped", path=path, size=body.get("size"), encoding=body.get("encoding"))
        return None
    try:
        return base64.b64decode(body["content"]).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        log.warning("content_not_utf8", path=path)
        return None
