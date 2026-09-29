"""Loader shared by the offline tests and the live eval harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from worker.mapper.github_files import ChangedFile, PullRequestFiles
from worker.schema import Finding, compute_fingerprint

ROOT = Path(__file__).parent


@dataclass
class Case:
    name: str
    finding: Finding
    siblings: list[Finding]
    pr_files: PullRequestFiles
    expected: dict
    notes: str


def load_cases() -> list[Case]:
    cases = []
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / "case.json").exists()):
        meta = json.loads((d / "case.json").read_text(encoding="utf-8"))
        pr = PullRequestFiles()
        for f in sorted((d / "files").rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(d / "files").as_posix()
            content = f.read_text(encoding="utf-8")
            spec = meta["diff"].get(rel, "all")
            n = len(content.splitlines())
            lines = set(range(1, n + 1)) if spec == "all" else set(range(spec[0], spec[1] + 1))
            pr.files[rel] = ChangedFile(path=rel, status="modified", diff_lines=lines, content=content)

        def mk(html: str, meta: dict = meta) -> Finding:
            # `meta` is bound as a default so the closure cannot capture a later
            # iteration's value (ruff B023).
            payload = dict(meta["finding"])
            payload["html"] = html
            payload["fingerprint"] = compute_fingerprint(payload["rule_id"], "/", html)
            return Finding.model_validate(payload)

        cases.append(
            Case(
                name=d.name,
                finding=mk(meta["finding"]["html"]),
                siblings=[mk(h) for h in meta.get("siblings", [])],
                pr_files=pr,
                expected=meta["expected"],
                notes=meta.get("notes", ""),
            )
        )
    return cases
