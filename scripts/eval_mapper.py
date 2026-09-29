"""Run the source mapper against the evaluation set with the real model.

    GEMINI_API_KEY=... python scripts/eval_mapper.py [--model gemini-3.5-flash] [--json out.json]

Prints, per case, what happened and whether it matched expectations, then the
four numbers that matter:

  false-suggestion rate   wrong-location or wrong-file suggestions / suggestions   target < 2%
  location precision      correct location / (suggestions + annotations)          target >= 0.95
  recall                  mappable cases posted / mappable cases                  target >= 0.70
  calibration             accuracy per reported-confidence bucket

Re-run after every prompt or threshold change. A change that raises recall
and raises the false-suggestion rate is a regression, not an improvement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.worker.mapper_cases import Case, load_cases
from worker.mapper.gemini import GeminiMapper
from worker.mapper.mapper import SourceMapper
from worker.schema import Finding


def judge(case: Case, f: Finding) -> tuple[bool, str]:
    """(correct, note). 'Correct' means: what we would post is right, or we
    correctly posted nothing."""
    exp = case.expected
    posted = f.disposition in ("suggestion", "annotation")

    if exp.get("file") is None:
        return (not posted), (
            "correctly dropped"
            if not posted
            else f"FALSE POSITIVE: posted at {f.source.file}:{f.source.line_start}"
        )

    if f.disposition in exp.get("never", []):
        return False, f"forbidden disposition {f.disposition}"
    if not posted:
        return False, f"missed (dropped: {f.disposition_reason})"

    src = f.source
    file_ok = src.file == exp["file"]
    lines = case.pr_files.contents[src.file].splitlines()[src.line_start - 1 : src.line_end]
    span = "\n".join(lines)
    line_ok = exp.get("line_contains", "") in span and exp.get("line_not_contains", "\x00") not in span
    if not (file_ok and line_ok):
        return False, f"WRONG LOCATION {src.file}:{src.line_start}-{src.line_end}"

    if "patch_contains" in exp and f.patch and exp["patch_contains"] not in f.patch.replacement:
        return False, f"patch lacks {exp['patch_contains']!r}: {f.patch.replacement.strip()[:80]}"
    if exp.get("requires_human_content") is False and f.patch and f.patch.requires_human_content:
        return False, "flagged for human content but it was inferable"
    if f.disposition not in exp.get("disposition_in", ["suggestion", "annotation"]):
        return False, f"disposition {f.disposition} not in {exp['disposition_in']}"
    return (
        True,
        f"{f.disposition} @ {src.file}:{src.line_start} loc={src.confidence:.2f} patch={f.patch.confidence if f.patch else 0:.2f}",
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"))
    ap.add_argument("--json", help="write per-case results here")
    ap.add_argument("--only", help="run a single case by name")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is required")

    mapper = SourceMapper(GeminiMapper(key, args.model), max_findings=20, concurrency=2)
    cases = [c for c in load_cases() if not args.only or c.name == args.only]

    rows = []
    suggestions = wrong_suggestions = 0
    posted = posted_correct = 0
    mappable = mappable_posted = 0
    buckets: dict[str, list[bool]] = defaultdict(list)

    for case in cases:
        findings = [case.finding, *case.siblings]
        stats = await mapper.map_findings(findings, case.pr_files)
        f = findings[0]
        correct, note = judge(case, f)

        dup_ok = True
        if "duplicates" in case.expected:
            dups = sum(1 for x in findings if x.disposition == "duplicate")
            dup_ok = dups == case.expected["duplicates"]
            note += f" | duplicates {dups}/{case.expected['duplicates']}"
        correct = correct and dup_ok

        if f.disposition == "suggestion":
            suggestions += 1
            if not correct:
                wrong_suggestions += 1
        if f.disposition in ("suggestion", "annotation"):
            posted += 1
            posted_correct += correct
            if f.source:
                buckets[f"{int(f.source.confidence * 10) / 10:.1f}"].append(correct)
        if case.expected.get("file") is not None:
            mappable += 1
            mappable_posted += f.disposition in ("suggestion", "annotation") and correct

        print(f"{'PASS' if correct else 'FAIL'}  {case.name:22s} {note}")
        if f.patch:
            print(f"      - {f.patch.original.strip()[:100]}")
            print(f"      + {f.patch.replacement.strip()[:100]}")
        rows.append(
            {
                "case": case.name,
                "correct": correct,
                "note": note,
                "disposition": f.disposition,
                "reason": f.disposition_reason,
                "source": f.source.model_dump() if f.source else None,
                "patch": f.patch.model_dump() if f.patch else None,
                "stats": stats.__dict__,
            }
        )

    print()
    print(
        f"false-suggestion rate : {wrong_suggestions}/{suggestions} = {wrong_suggestions / suggestions if suggestions else 0:.1%}   (target < 2%)"
    )
    print(
        f"location precision    : {posted_correct}/{posted} = {posted_correct / posted if posted else 0:.1%}   (target >= 95%)"
    )
    print(
        f"recall                : {mappable_posted}/{mappable} = {mappable_posted / mappable if mappable else 0:.1%}   (target >= 70%)"
    )
    print(
        "calibration           : " + "  ".join(f"[{b}] {sum(v)}/{len(v)}" for b, v in sorted(buckets.items()))
    )

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return 0 if wrong_suggestions == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
