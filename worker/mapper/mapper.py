"""Orchestrates the four stages for a batch of findings.

    literals -> candidates -> model -> verify -> decide -> Finding.source/patch

Cost controls: findings are mapped worst-impact first up to `max_findings`;
findings with no candidates never reach the model; identical
(rule, normalised html) pairs share one model call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.logging_config import get_logger
from worker.mapper import literals as lit
from worker.mapper.candidates import Candidate, find_candidates
from worker.mapper.decide import decide
from worker.mapper.diff import span_in_diff
from worker.mapper.gemini import MapperModel, MapperResponse, build_prompt
from worker.mapper.github_files import PullRequestFiles
from worker.mapper.verify import locate_original, looks_like_placeholder, structurally_sound
from worker.schema import Finding, Patch, SourceLocation, normalise_html

log = get_logger(__name__)

_IMPACT_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
# Stage 1 score at/above which the top candidate counts as a grep-level match.
GREP_STRONG_SCORE = 8


@dataclass
class MapStats:
    considered: int = 0
    no_candidates: int = 0
    model_calls: int = 0
    suggestions: int = 0
    annotations: int = 0
    drops: int = 0
    duplicates: int = 0
    errors: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)


class SourceMapper:
    def __init__(self, model: MapperModel, *, max_findings: int = 10, concurrency: int = 3) -> None:
        self._model = model
        self._max = max_findings
        self._sem = asyncio.Semaphore(concurrency)

    async def map_findings(self, findings: list[Finding], pr_files: PullRequestFiles) -> MapStats:
        stats = MapStats()
        contents = pr_files.contents
        if not contents or not findings:
            for f in findings:
                _drop(f, "no source files in PR" if not contents else "nothing to map")
            stats.drops = len(findings)
            return stats

        ordered = sorted(findings, key=lambda f: _IMPACT_RANK.get(f.impact.value, 9))
        selected, overflow = ordered[: self._max], ordered[self._max :]
        for f in overflow:
            _drop(f, f"beyond per-scan mapping cap of {self._max}")
            stats.drops += 1

        # One model call per distinct (rule, normalised element).
        groups: dict[str, list[Finding]] = {}
        for f in selected:
            groups.setdefault(f"{f.rule_id}|{normalise_html(f.html)}", []).append(f)

        async def _run(group: list[Finding]) -> None:
            async with self._sem:
                await self._map_group(group, pr_files, contents, stats)

        await asyncio.gather(*(_run(g) for g in groups.values()))

        _mark_duplicates(selected, stats)
        for f in selected:
            stats.by_reason[f.disposition_reason or "?"] = (
                stats.by_reason.get(f.disposition_reason or "?", 0) + 1
            )
        log.info("mapping_complete", **{k: v for k, v in stats.__dict__.items() if k != "by_reason"})
        return stats

    async def _map_group(
        self, group: list[Finding], pr_files: PullRequestFiles, contents: dict[str, str], stats: MapStats
    ) -> None:
        primary = group[0]
        stats.considered += len(group)

        candidates = find_candidates(lit.extract(primary.html), contents)
        if not candidates:
            stats.no_candidates += len(group)
            for f in group:
                _drop(f, "no candidate source lines")
            stats.drops += len(group)
            return

        try:
            stats.model_calls += 1
            response = await self._model.map(build_prompt(primary, candidates))
        except Exception:
            log.exception("mapper_model_failed", rule=primary.rule_id)
            stats.errors += 1
            for f in group:
                _drop(f, "model error")
            stats.drops += len(group)
            return

        outcome = self._resolve(primary, response, candidates, pr_files, contents)
        for f in group:
            f.source, f.patch, f.disposition, f.disposition_reason = outcome
            _count(stats, f.disposition)

    def _resolve(
        self,
        finding: Finding,
        response: MapperResponse | None,
        candidates: list[Candidate],
        pr_files: PullRequestFiles,
        contents: dict[str, str],
    ) -> tuple[SourceLocation | None, Patch | None, str, str]:
        if response is None or not response.matched or response.candidate_index is None:
            return None, None, "drop", "model reported no match"
        if not 0 <= response.candidate_index < len(candidates):
            return None, None, "drop", "model returned candidate index out of range"

        cand = candidates[response.candidate_index]
        content = contents[cand.file]
        diff_lines = pr_files.files[cand.file].diff_lines

        located = None
        if response.original:
            located = locate_original(content, response.original, response.line_start, response.line_end)

        if located is None:
            # Could not verify the model's lines. Fall back to the candidate window
            # itself for an annotation if the model was confident about *which* candidate.
            if response.location_confidence >= 0.6:
                source = SourceLocation(
                    file=cand.file,
                    line_start=cand.line_start,
                    line_end=cand.line_end,
                    confidence=min(response.location_confidence, 0.79),
                    method="llm",
                    in_diff=span_in_diff(diff_lines, cand.line_start, cand.line_end),
                )
                return source, None, "annotation", "model lines unverified; annotating candidate window"
            return None, None, "drop", "original text not found in file"

        method = "grep" if (cand is candidates[0] and cand.score >= GREP_STRONG_SCORE) else "llm"
        in_diff = span_in_diff(diff_lines, located.line_start, located.line_end)
        source = SourceLocation(
            file=cand.file,
            line_start=located.line_start,
            line_end=located.line_end,
            confidence=response.location_confidence,
            method=method,
            in_diff=in_diff,
        )

        patch = None
        sound = placeholder = False
        if response.replacement and response.original:
            sound = structurally_sound(response.original, response.replacement)
            placeholder = looks_like_placeholder(response.replacement)
            patch = Patch(
                original=response.original,
                replacement=response.replacement,
                rationale=response.rationale,
                generated_by=self._model.model_id,
                confidence=response.patch_confidence,
                requires_human_content=response.requires_human_content,
            )

        decision = decide(
            location_confidence=response.location_confidence,
            patch_confidence=response.patch_confidence,
            requires_human_content=response.requires_human_content,
            original_verified=True,
            in_diff=in_diff,
            structurally_sound=sound,
            placeholder=placeholder,
            has_patch=patch is not None,
        )
        return source, patch, decision.disposition, decision.reason


def _drop(f: Finding, reason: str) -> None:
    f.disposition, f.disposition_reason = "drop", reason


def _count(stats: MapStats, disposition: str | None) -> None:
    if disposition == "suggestion":
        stats.suggestions += 1
    elif disposition == "annotation":
        stats.annotations += 1
    else:
        stats.drops += 1


def _mark_duplicates(findings: list[Finding], stats: MapStats) -> None:
    """Several rendered elements from one JSX construct -> one comment.

    Matched on *overlapping* line ranges, not exact equality: three <a> from one
    .map() often come back with slightly different spans (12-12 vs 12-14), and
    exact matching would post two comments about one line.

    A span that collects more than one finding is then forced to `annotation`.
    One JSX line rendering N elements means a literal fix -- aria-label="Visit
    our GitHub profile" for the line that also renders Twitter and LinkedIn --
    is wrong for N-1 of them. Only a parameterised patch would be right, and we
    cannot tell whether the model wrote one, so we refuse the one-click path and
    let a human read it.
    """
    groups: list[tuple[Finding, list[Finding]]] = []
    for f in findings:
        if f.source is None or f.disposition in ("drop", None):
            continue
        primary = next(
            (
                p
                for p, _ in groups
                if p.source is not None
                and p.source.file == f.source.file
                and not (f.source.line_end < p.source.line_start or f.source.line_start > p.source.line_end)
            ),
            None,
        )
        if primary is None:
            groups.append((f, []))
            continue
        _discount(stats, f.disposition)
        f.disposition, f.duplicate_of = "duplicate", primary.fingerprint
        f.disposition_reason = f"same source lines as {primary.fingerprint}"
        stats.duplicates += 1
        next(g for p, g in groups if p is primary).append(f)

    for primary, siblings in groups:
        if siblings and primary.disposition == "suggestion":
            _discount(stats, "suggestion")
            primary.disposition = "annotation"
            primary.disposition_reason = (
                f"this line renders {len(siblings) + 1} elements; a literal fix would be "
                "wrong for the others -- review before committing"
            )
            stats.annotations += 1


def _discount(stats: MapStats, disposition: str | None) -> None:
    if disposition == "suggestion":
        stats.suggestions -= 1
    elif disposition == "annotation":
        stats.annotations -= 1
