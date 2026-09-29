"""Stage 1 (literals + candidates), diff parsing, verification and the decision
table -- all pure, all offline."""

import pytest

from tests.worker.mapper_cases import load_cases
from worker.mapper import literals
from worker.mapper.candidates import find_candidates
from worker.mapper.decide import decide
from worker.mapper.diff import right_side_lines, span_in_diff
from worker.mapper.verify import locate_original, looks_like_placeholder, structurally_sound

CASES = {c.name: c for c in load_cases()}


# ---- literals ------------------------------------------------------------


def test_literals_keep_stable_drop_generated():
    lits = literals.extract(
        '<img id="headlessui-:r3:" data-testid="hero-img" src="/img/hero-mountains.8f3a2c1d.jpg" '
        'class="css-1q2w3e h-8 flex rounded-lg" style="opacity:1" data-reactid="5">'
    )
    values = {l.value: l for l in lits.literals}
    assert lits.tag == "img"
    assert values["hero-img"].weight == 6
    assert "/img/hero-mountains.8f3a2c1d.jpg" in values
    assert "hero-mountains.8f3a2c1d.jpg" in values
    assert "hero-mountains" in values, "bundler hash stripped"
    assert " src=" in values, "prop-name literal for call-site matching"
    assert "h-8" in values and "rounded-lg" in values
    assert "css-1q2w3e" not in values and "flex" not in values
    assert not any(":r3:" in v for v in values)
    assert not any("opacity" in v for v in values)


def test_literals_text_and_data_urls():
    lits = literals.extract('<a href="/pricing" class="nav">See pricing</a>')
    values = {l.value for l in lits.literals}
    assert "See pricing" in values and "/pricing" in values
    lits = literals.extract('<img src="data:image/gif;base64,R0lGOD">')
    assert not any(l.kind == "src-basename" for l in lits.literals)


# ---- candidates on the eval set --------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_stage1_finds_expected_file(name):
    case = CASES[name]
    cands = find_candidates(literals.extract(case.finding.html), case.pr_files.contents)
    expected = case.expected.get("file")
    if expected is None:
        assert cands == [], f"{name}: must produce no candidates, got {[c.file for c in cands]}"
        return
    assert len(cands) <= 3
    hit = [c for c in cands if c.file == expected and case.expected["line_contains"] in c.excerpt]
    assert hit, (
        f"{name}: expected {expected} containing {case.expected['line_contains']!r}; got {[(c.file, c.line_start, c.line_end) for c in cands]}"
    )


def test_tag_only_fallback_gives_up_when_tag_is_everywhere():
    files = {"a.tsx": "\n".join(f"<img src={{x{i}}} />" for i in range(20))}
    lits = literals.extract('<img src="/nope.png" class="css-abc">')
    # src basename 'nope' hits nothing; tag fallback sees 20 imgs -> nothing.
    assert find_candidates(lits, files) == []


# ---- diff parsing ----------------------------------------------------------

PATCH = """@@ -1,4 +1,5 @@
 import x
-old line
+new line
+another new
 context
@@ -20,2 +21,3 @@
 ctx
+added
 ctx2
\\ No newline at end of file"""


def test_right_side_lines():
    lines = right_side_lines(PATCH)
    assert lines == {1, 2, 3, 4, 21, 22, 23}
    assert span_in_diff(lines, 2, 4)
    assert not span_in_diff(lines, 4, 6)
    assert right_side_lines(None) == set()


# ---- verify ---------------------------------------------------------------

CONTENT = "a\nb\n  <img src={x} />\nc\nd\n  <img src={x} />\ne\n"


def test_locate_exact_claim():
    loc = locate_original(CONTENT, "  <img src={x} />", 3, 3)
    assert (loc.line_start, loc.line_end, loc.relocated) == (3, 3, False)


def test_locate_relocates_off_by_n():
    loc = locate_original("a\nb\nc\n  <img src={y} />\nd\n", "  <img src={y} />", 2, 2)
    assert (loc.line_start, loc.relocated) == (4, True)


def test_locate_refuses_ambiguous_whole_file_match():
    # Two identical blocks, claim points nowhere near either.
    assert locate_original(CONTENT, "  <img src={x} />", 40, 40) is None


def test_locate_ignores_trailing_whitespace_only():
    assert locate_original("x\n  <img />   \n", "  <img />", 2, 2) is not None
    assert locate_original(CONTENT, "", 1, 1) is None


def test_structural_check():
    assert structurally_sound("<img src={x} />", '<img src={x} alt="Logo" />')
    assert structurally_sound(
        "<button onClick={f}>\n  <X />\n</button>",
        '<button onClick={f} aria-label="Close">\n  <X />\n</button>',
    )
    assert not structurally_sound("<img src={x} />", '<img src={x} alt="Logo">')  # lost self-close
    assert not structurally_sound(
        "{items.map(i => (\n<li>{i}</li>\n))}", "{items.map(i => (\n<li>{i}</li>\n)}"
    )
    assert not structurally_sound("x", "")


def test_placeholder_detection():
    assert looks_like_placeholder('alt="TODO: describe the image"')
    assert looks_like_placeholder('aria-label="Describe this button"')
    assert not looks_like_placeholder('alt="Acme company logo"')


# ---- decide ---------------------------------------------------------------


def _d(**over):
    base = {
        "location_confidence": 0.95,
        "patch_confidence": 0.9,
        "requires_human_content": False,
        "original_verified": True,
        "in_diff": True,
        "structurally_sound": True,
        "placeholder": False,
        "has_patch": True,
    }
    base.update(over)
    return decide(**base)


def test_decision_table():
    assert _d().disposition == "suggestion"
    assert _d(original_verified=False).disposition == "drop"
    assert _d(location_confidence=0.5).disposition == "drop"
    assert _d(location_confidence=0.7).disposition == "annotation"
    assert _d(patch_confidence=0.7).disposition == "annotation"
    assert _d(requires_human_content=True).disposition == "annotation"
    assert _d(placeholder=True).disposition == "annotation"
    assert _d(structurally_sound=False).disposition == "annotation"
    assert _d(in_diff=False).disposition == "annotation"
    assert _d(has_patch=False).disposition == "annotation"
