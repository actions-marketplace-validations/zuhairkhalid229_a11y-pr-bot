"""SourceMapper end to end with a scripted model. Every branch of verify ->
decide -> Finding is driven from here; the live model is exercised only by
scripts/eval_mapper.py."""

from tests.worker.mapper_cases import load_cases
from worker.mapper.gemini import MapperResponse
from worker.mapper.mapper import SourceMapper
from worker.schema import Impact

CASES = {c.name: c for c in load_cases()}


class ScriptedModel:
    model_id = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def map(self, prompt):
        self.prompts.append(prompt)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _resp(**over):
    base: dict = {
        "matched": True,
        "candidate_index": 0,
        "line_start": None,
        "line_end": None,
        "location_confidence": 0.95,
        "original": None,
        "replacement": None,
        "patch_confidence": 0.9,
        "requires_human_content": False,
        "rationale": "test",
    }
    base.update(over)
    return MapperResponse(**base)


async def _run(case, *responses, **kw):
    model = ScriptedModel(responses)
    mapper = SourceMapper(model, **kw)
    findings = [case.finding, *case.siblings]
    stats = await mapper.map_findings(findings, case.pr_files)
    return findings, stats, model


async def test_suggestion_happy_path():
    case = CASES["direct-literal"]
    original = '        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />'
    replacement = (
        '        <img src="/logo.svg" alt="Acme logo" className="h-8 w-auto" width={120} height={32} />'
    )
    [f], stats, model = await _run(
        case, _resp(line_start=7, line_end=7, original=original, replacement=replacement)
    )

    assert f.disposition == "suggestion", f.disposition_reason
    assert f.source.file == "src/components/Header.tsx"
    assert (f.source.line_start, f.source.line_end) == (7, 7)
    assert f.source.method == "grep"  # top candidate, strong score
    assert f.source.in_diff is True
    assert f.patch.replacement == replacement
    assert f.patch.generated_by == "scripted"
    assert stats.suggestions == 1 and stats.model_calls == 1
    assert "Candidate 0: src/components/Header.tsx" in model.prompts[0]
    assert "    7 |" in model.prompts[0]


async def test_model_off_by_two_is_relocated():
    case = CASES["direct-literal"]
    original = '        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />'
    [f], _, _ = await _run(
        case,
        _resp(
            line_start=5,
            line_end=5,
            original=original,
            replacement=original.replace("<img", '<img alt="Acme logo"'),
        ),
    )
    assert f.disposition == "suggestion"
    assert f.source.line_start == 7


async def test_out_of_hunk_is_annotation_never_suggestion():
    case = CASES["out-of-hunk"]
    original = '      <img src={image} className="card-img" />'
    [f], _stats, _ = await _run(
        case,
        _resp(
            line_start=6,
            line_end=6,
            original=original,
            replacement=original.replace("<img", "<img alt={title}"),
        ),
    )
    assert f.disposition == "annotation"
    assert "diff hunks" in f.disposition_reason
    assert f.source.in_diff is False
    assert f.patch is not None, "the patch is still shown, just not as a one-click fence"


async def test_requires_human_content_is_annotation():
    case = CASES["needs-human-content"]
    original = '      <button className="modal-close" onClick={onDismiss}>'
    [f], _, _ = await _run(
        case,
        _resp(
            line_start=6,
            line_end=6,
            original=original,
            replacement=original.replace(">", ' aria-label="TODO: describe action">'),
            requires_human_content=True,
        ),
    )
    assert f.disposition == "annotation"
    assert f.patch.requires_human_content is True


async def test_placeholder_text_is_annotation_even_if_model_is_confident():
    case = CASES["direct-literal"]
    original = '        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />'
    [f], _, _ = await _run(
        case,
        _resp(
            line_start=7,
            line_end=7,
            original=original,
            replacement=original.replace("<img", '<img alt="TODO: describe the image"'),
        ),
    )
    assert f.disposition == "annotation" and "placeholder" in f.disposition_reason


async def test_structurally_broken_patch_is_annotation():
    case = CASES["direct-literal"]
    original = '        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />'
    [f], _, _ = await _run(
        case,
        _resp(
            line_start=7,
            line_end=7,
            original=original,
            replacement='        <img src="/logo.svg" alt="Logo" className="h-8 w-auto" width={120} height={32}>',
        ),
    )
    assert f.disposition == "annotation" and "structural" in f.disposition_reason


async def test_original_not_in_file_with_confident_model_annotates_window():
    case = CASES["direct-literal"]
    [f], _, _ = await _run(
        case, _resp(line_start=7, line_end=7, original="<img src='nope' />", replacement="<img alt='x' />")
    )
    assert f.disposition == "annotation" and "unverified" in f.disposition_reason
    assert f.patch is None
    assert f.source.confidence <= 0.79


async def test_original_not_in_file_with_unsure_model_drops():
    case = CASES["direct-literal"]
    [f], _, _ = await _run(
        case, _resp(location_confidence=0.5, original="<img src='nope' />", replacement="x")
    )
    assert f.disposition == "drop"


async def test_no_match_and_bad_index_drop():
    case = CASES["direct-literal"]
    [f], _, _ = await _run(case, _resp(matched=False, candidate_index=None))
    assert f.disposition == "drop" and "no match" in f.disposition_reason
    [f], _, _ = await _run(case, _resp(candidate_index=7))
    assert f.disposition == "drop" and "out of range" in f.disposition_reason


async def test_not_in_pr_never_calls_model():
    case = CASES["not-in-pr"]
    [f], stats, model = await _run(case)  # no responses scripted: a call would IndexError
    assert f.disposition == "drop" and "no candidate" in f.disposition_reason
    assert stats.model_calls == 0 and model.prompts == []


async def test_map_render_siblings_collapse_to_one_patch():
    case = CASES["map-render"]
    original = '          <a href={href} target="_blank" rel="noreferrer">'
    replacement = '          <a href={href} target="_blank" rel="noreferrer" aria-label={`Acme on ${href}`}>'
    resp = _resp(line_start=12, line_end=12, original=original, replacement=replacement)
    # Three findings with different hrefs are three groups -> three calls.
    findings, stats, _model = await _run(case, resp, resp, resp)
    assert stats.model_calls == 3
    dispositions = sorted(f.disposition for f in findings)
    # One comment, and NOT a one-click one: this line renders three links, so a
    # literal aria-label would be wrong for two of them.
    assert dispositions == ["annotation", "duplicate", "duplicate"]
    primary = next(f for f in findings if f.disposition == "annotation")
    assert "renders 3 elements" in primary.disposition_reason
    for f in findings:
        if f.disposition == "duplicate":
            assert f.duplicate_of == primary.fingerprint
            assert f.source.line_start == 12
    assert stats.suggestions == 0 and stats.annotations == 1 and stats.duplicates == 2


async def test_overlapping_spans_collapse_even_when_not_identical():
    """Sibling elements often come back with slightly different line ranges;
    exact-match dedupe would post two comments about one JSX line."""
    case = CASES["map-render"]
    original = '          <a href={href} target="_blank" rel="noreferrer">'
    wide = """          <a href={href} target="_blank" rel="noreferrer">
            <Icon />
          </a>"""
    narrow = _resp(line_start=12, line_end=12, original=original, replacement=original + " x")
    broad = _resp(line_start=12, line_end=14, original=wide, replacement=wide + " x")
    findings, stats, _ = await _run(case, narrow, broad, broad)
    assert stats.duplicates == 2, "12-12 and 12-14 overlap, so they are one construct"
    assert sum(1 for f in findings if f.disposition in ("suggestion", "annotation")) == 1


async def test_single_finding_on_a_line_still_gets_one_click():
    """The downgrade must apply only when a span really has siblings."""
    case = CASES["direct-literal"]
    original = '        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />'
    [f], stats, _ = await _run(
        case,
        _resp(
            line_start=7,
            line_end=7,
            original=original,
            replacement=original.replace("<img", '<img alt="Acme logo"'),
        ),
    )
    assert f.disposition == "suggestion" and stats.suggestions == 1


async def test_identical_elements_share_one_model_call():
    case = CASES["direct-literal"]
    twin = case.finding.model_copy(update={"fingerprint": "other"})
    model = ScriptedModel([_resp(matched=False, candidate_index=None)])
    stats = await SourceMapper(model).map_findings([case.finding, twin], case.pr_files)
    assert stats.model_calls == 1 and stats.considered == 2


async def test_cap_drops_overflow_worst_first():
    case = CASES["direct-literal"]
    minor = case.finding.model_copy(
        update={"fingerprint": "m", "impact": Impact.minor, "html": "<img src='/other.png'>"}
    )
    model = ScriptedModel([_resp(matched=False, candidate_index=None)])
    findings = [minor, case.finding]
    stats = await SourceMapper(model, max_findings=1).map_findings(findings, case.pr_files)
    assert minor.disposition == "drop" and "cap" in minor.disposition_reason
    assert stats.model_calls == 1


async def test_model_exception_drops_and_counts():
    case = CASES["direct-literal"]
    [f], stats, _ = await _run(case, RuntimeError("quota"))
    assert f.disposition == "drop" and f.disposition_reason == "model error"
    assert stats.errors == 1


async def test_no_source_files_drops_everything():
    from worker.mapper.github_files import PullRequestFiles

    case = CASES["direct-literal"]
    stats = await SourceMapper(ScriptedModel([])).map_findings([case.finding], PullRequestFiles())
    assert case.finding.disposition == "drop" and stats.drops == 1
