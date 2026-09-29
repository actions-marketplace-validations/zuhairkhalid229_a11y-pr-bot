"""Cross-file pin consistency.

These are the mismatches that do not fail at build time and do not fail any
functional test -- they fail at container start-up in production, or produce a
confusing pip resolution error. Cheap to assert, expensive to debug.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ["requirements.txt", "worker/requirements.txt", "requirements-dev.txt"]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def pin(rel: str, package: str) -> str | None:
    """The pinned version of `package` in a requirements file, if present."""
    pattern = re.compile(rf"^{re.escape(package)}(?:\[[^\]]+\])?==([^\s;]+)", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(read(rel))
    return match.group(1) if match else None


def test_playwright_matches_the_docker_base_image():
    """A Dockerfile base tag that disagrees with the playwright pin fails at
    container start with "Executable doesn't exist", not at build -- so nothing
    catches it until deploy. Upgrade both together."""
    pinned = pin("worker/requirements.txt", "playwright")
    assert pinned, "playwright must stay pinned in worker/requirements.txt"

    dockerfile = read("worker/Dockerfile")
    match = re.search(r"^FROM\s+mcr\.microsoft\.com/playwright/python:v([\d.]+)", dockerfile, re.MULTILINE)
    assert match, "worker/Dockerfile must use the official Playwright Python image"

    assert match.group(1) == pinned, (
        f"worker/Dockerfile pins Playwright image v{match.group(1)} but "
        f"worker/requirements.txt pins playwright=={pinned}. They must match; "
        f"a mismatch only surfaces when the container starts."
    )


def test_shared_packages_agree_across_requirements_files():
    """The api and worker images share app/, so a package pinned differently in
    each produces two behaviours from one codebase."""
    shared = [
        "fastapi",
        "pydantic",
        "pydantic-settings",
        "httpx",
        "PyJWT",
        "cryptography",
        "google-cloud-firestore",
        "structlog",
        "uvicorn",
    ]
    mismatches = []
    for package in shared:
        api, worker = pin("requirements.txt", package), pin("worker/requirements.txt", package)
        if api and worker and api != worker:
            mismatches.append(f"{package}: api=={api} worker=={worker}")
    assert not mismatches, "pins disagree between the two images:\n  " + "\n  ".join(mismatches)


@pytest.mark.parametrize("rel", REQUIREMENTS)
def test_requirements_are_fully_pinned(rel: str):
    """An unpinned dependency makes a build unreproducible and lets a breaking
    release reach production without a commit."""
    loose = []
    for raw in read(rel).splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-r "):
            continue
        if "==" not in line:
            loose.append(line)
    assert not loose, f"{rel} has unpinned requirements: {loose}"


@pytest.mark.parametrize("rel", REQUIREMENTS)
def test_no_byte_order_mark(rel: str):
    """PowerShell's `Set-Content -Encoding utf8` writes UTF-8 *with* BOM on
    Windows. A BOM at the head of a requirements file confuses some parsers and
    shows up as a phantom diff for every contributor on another platform."""
    raw = (ROOT / rel).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), (
        f"{rel} starts with a UTF-8 BOM. Rewrite it as UTF-8 without BOM."
    )


def test_vendored_axe_version_matches_its_recorded_digest():
    """worker/axe.py verifies this at import; asserting it here means a bad
    vendoring shows up as a named test failure rather than a stack trace."""
    from worker.axe import load_axe

    source, version = load_axe()
    assert version and source.lstrip().startswith("/*! axe")
    assert f"axe v{version}" in source[:200], "vendored axe.min.js does not match axe.json"
