from worker.schema import compute_fingerprint, normalise_html, page_path_of
from worker.wcag import criteria_from_tags, en_clauses_from_tags


def test_normalise_strips_generated_ids_and_classes():
    raw = (
        '<div id="headlessui-menu-button-:r3:" class="css-1q2w3e sc-AxjAm Button_root__x8Kz1 flex"'
        ' style="color: red" data-reactroot="">  <span>Hi</span> </div>'
    )
    out = normalise_html(raw)
    assert ":r3:" not in out
    assert "css-1q2w3e" not in out
    assert "sc-AxjAm" not in out
    assert "Button_root__x8Kz1" not in out
    assert "style=" not in out
    assert "data-reactroot" not in out
    assert "flex" in out, "deterministic classes must survive"
    assert "<span>Hi</span>" in out


def test_fingerprint_ignores_preview_host():
    a = compute_fingerprint("image-alt", page_path_of("https://site-abc.vercel.app/pricing"), "<img src=x>")
    b = compute_fingerprint("image-alt", page_path_of("https://site-xyz.vercel.app/pricing"), "<img src=x>")
    assert a == b


def test_fingerprint_ignores_css_in_js_rehash():
    a = compute_fingerprint("image-alt", "/", '<img class="css-1a2b3c" src="/hero.png">')
    b = compute_fingerprint("image-alt", "/", '<img class="css-9z8y7x" src="/hero.png">')
    assert a == b


def test_fingerprint_changes_when_element_is_fixed():
    broken = compute_fingerprint("image-alt", "/", '<img src="/hero.png">')
    fixed = compute_fingerprint("image-alt", "/", '<img src="/hero.png" alt="Hero">')
    assert broken != fixed


def test_fingerprint_changes_across_rules_and_pages():
    base = compute_fingerprint("image-alt", "/", "<img>")
    assert base != compute_fingerprint("role-img-alt", "/", "<img>")
    assert base != compute_fingerprint("image-alt", "/about", "<img>")


def test_page_path_keeps_query_drops_host_and_fragment():
    assert page_path_of("https://x.vercel.app/a?b=1#frag") == "/a?b=1"
    assert page_path_of("https://x.vercel.app") == "/"


def test_wcag_tags_parse_including_two_digit_criteria():
    tags = ["cat.color", "wcag2aa", "wcag143", "wcag21aa", "wcag1410", "wcag22aa", "wcag258"]
    got = {c.id: c for c in criteria_from_tags(tags)}
    assert got["1.4.3"].name == "Contrast (Minimum)"
    assert got["1.4.3"].level == "AA"
    assert got["1.4.10"].name == "Reflow"
    assert got["1.4.10"].introduced_in == "2.1"
    assert got["2.5.8"].name == "Target Size (Minimum)"
    assert got["2.5.8"].introduced_in == "2.2"


def test_obsolete_parsing_criterion_is_flagged_not_dropped():
    [c] = criteria_from_tags(["wcag2a", "wcag411"])
    assert c.id == "4.1.1"
    assert c.obsolete_in_2_2 is True


def test_aaa_and_unknown_tags_are_ignored():
    # 1.4.6 is AAA; not in our table; must not raise.
    assert criteria_from_tags(["wcag2aaa", "wcag146", "best-practice", "ACT"]) == []


def test_en_301_549_clauses_extracted():
    tags = ["wcag111", "EN-301-549", "EN-9.1.1.1", "section508"]
    assert en_clauses_from_tags(tags) == ["9.1.1.1"]
