"""Generates the mapper evaluation set. Run once; commit the output.

    python tests/worker/mapper_cases/build_cases.py

Each case directory holds:
  files/<path>    the PR's changed files, full content
  case.json       finding (as the scanner emits it), which lines are in the
                  diff, and the expected outcome
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent

IMAGE_ALT = {
    "rule_id": "image-alt",
    "impact": "critical",
    "wcag": [{"id": "1.1.1", "name": "Non-text Content", "level": "A", "introduced_in": "2.0"}],
    "help": "Images must have alternative text",
    "failure_summary": "Fix any of the following:\n  Element does not have an alt attribute",
}
BUTTON_NAME = {
    "rule_id": "button-name",
    "impact": "critical",
    "wcag": [{"id": "4.1.2", "name": "Name, Role, Value", "level": "A", "introduced_in": "2.0"}],
    "help": "Buttons must have discernible text",
    "failure_summary": "Fix any of the following:\n  Element does not have inner text that is visible to screen readers\n  aria-label attribute does not exist or is empty",
}
LABEL = {
    "rule_id": "label",
    "impact": "critical",
    "wcag": [{"id": "4.1.2", "name": "Name, Role, Value", "level": "A", "introduced_in": "2.0"}],
    "help": "Form elements must have labels",
    "failure_summary": "Fix any of the following:\n  Form element does not have an implicit (wrapped) <label>\n  aria-label attribute does not exist or is empty",
}
LINK_NAME = {
    "rule_id": "link-name",
    "impact": "serious",
    "wcag": [{"id": "2.4.4", "name": "Link Purpose (In Context)", "level": "A", "introduced_in": "2.0"}],
    "help": "Links must have discernible text",
    "failure_summary": "Fix all of the following:\n  Element does not have text that is visible to screen readers",
}
HTML_LANG = {
    "rule_id": "html-has-lang",
    "impact": "serious",
    "wcag": [{"id": "3.1.1", "name": "Language of Page", "level": "A", "introduced_in": "2.0"}],
    "help": "<html> element must have a lang attribute",
    "failure_summary": "Fix any of the following:\n  The <html> element does not have a lang attribute",
}


def finding(base: dict, html: str, selector: str) -> dict:
    return {
        **base,
        "html": html,
        "selector": selector,
        "en_301_549": ["9.1.1.1"],
        "page_url": "https://p.vercel.app/",
        "page_path": "/",
        "help_url": "https://dequeuniversity.com/rules/axe/4.13/x",
    }


CASES: dict[str, dict] = {}

# 1. direct literal -----------------------------------------------------------
CASES["direct-literal"] = {
    "files": {
        "src/components/Header.tsx": """import Link from 'next/link'

export function Header() {
  return (
    <header className="flex items-center gap-4 px-6 py-3">
      <Link href="/">
        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />
      </Link>
      <nav className="ml-auto">
        <Link href="/pricing">Pricing</Link>
      </nav>
    </header>
  )
}
"""
    },
    "diff": {"src/components/Header.tsx": "all"},
    "finding": finding(
        IMAGE_ALT, '<img src="/logo.svg" class="h-8 w-auto" width="120" height="32">', "header > a > img"
    ),
    "expected": {
        "file": "src/components/Header.tsx",
        "line_contains": 'src="/logo.svg"',
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "alt=",
    },
    "notes": "Everything survives the projection. Baseline.",
}

# 2. css-in-js: only src survives -----------------------------------------------
CASES["css-in-js"] = {
    "files": {
        "src/Hero.tsx": """/** @jsxImportSource @emotion/react */
import { css } from '@emotion/react'
import hero from '../assets/hero-mountains.jpg'

const wrap = css`
  display: grid;
  place-items: center;
`

export default function Hero() {
  return (
    <section css={wrap}>
      <img src={hero} css={css`max-width: 100%`} />
      <h1>Climb higher</h1>
    </section>
  )
}
"""
    },
    "diff": {"src/Hero.tsx": "all"},
    "finding": finding(
        IMAGE_ALT,
        '<img src="/_next/static/media/hero-mountains.8f3a2c1d.jpg" class="css-1q2w3e">',
        "section > img",
    ),
    "expected": {
        "file": "src/Hero.tsx",
        "line_contains": "src={hero}",
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "alt=",
    },
    "notes": "Class is hashed; src is rewritten by the bundler but the basename stem survives in the import.",
}

# 3. i18n text ----------------------------------------------------------------
CASES["i18n-text"] = {
    "files": {
        "src/checkout/SubmitButton.tsx": """import { useTranslation } from 'react-i18next'
import { SpinnerIcon } from '../icons'

export function SubmitButton({ busy }: { busy: boolean }) {
  const { t } = useTranslation()
  return (
    <button type="submit" className="btn-primary" disabled={busy}>
      {busy ? <SpinnerIcon /> : null}
    </button>
  )
}
"""
    },
    "diff": {"src/checkout/SubmitButton.tsx": "all"},
    "finding": finding(
        BUTTON_NAME,
        '<button type="submit" class="btn-primary"><svg class="spinner"></svg></button>',
        "form > button",
    ),
    "expected": {
        "file": "src/checkout/SubmitButton.tsx",
        "line_contains": '<button type="submit"',
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "aria-label",
    },
    "notes": "Text literal is empty; must locate by tag + type + class token.",
}

# 4. .map render: three findings, one source line ------------------------------
_MAP_FILE = """const socials = [
  { href: 'https://twitter.com/acme', icon: TwitterIcon },
  { href: 'https://github.com/acme', icon: GithubIcon },
  { href: 'https://linkedin.com/company/acme', icon: LinkedinIcon },
]

export function SocialLinks() {
  return (
    <ul className="social-links">
      {socials.map(({ href, icon: Icon }) => (
        <li key={href}>
          <a href={href} target="_blank" rel="noreferrer">
            <Icon />
          </a>
        </li>
      ))}
    </ul>
  )
}
"""
CASES["map-render"] = {
    "files": {"src/Footer/SocialLinks.tsx": _MAP_FILE},
    "diff": {"src/Footer/SocialLinks.tsx": "all"},
    "finding": finding(
        LINK_NAME,
        '<a href="https://github.com/acme" target="_blank" rel="noreferrer"><svg></svg></a>',
        "ul.social-links > li:nth-child(2) > a",
    ),
    "siblings": [
        '<a href="https://twitter.com/acme" target="_blank" rel="noreferrer"><svg></svg></a>',
        '<a href="https://linkedin.com/company/acme" target="_blank" rel="noreferrer"><svg></svg></a>',
    ],
    "expected": {
        "file": "src/Footer/SocialLinks.tsx",
        "line_contains": "<a href={href}",
        "disposition_in": ["annotation"],
        "never": ["suggestion"],
        "duplicates": 2,
    },
    "notes": "href literal hits the data array, not the JSX. Three <a> from one .map() must collapse to ONE comment, and it must not be one-click: a literal aria-label would be wrong for two of the three.",
}

# 5. wrapper component: <img> lives in an unchanged file -----------------------
CASES["wrapper-component"] = {
    "files": {
        "src/pages/Team.tsx": """import { Avatar } from '../components/Avatar'
import { team } from '../data/team'

export default function Team() {
  return (
    <ul className="team-grid">
      {team.map((person) => (
        <li key={person.id}>
          <Avatar src={person.photo} size={64} />
          <span>{person.name}</span>
        </li>
      ))}
    </ul>
  )
}
"""
    },
    "diff": {"src/pages/Team.tsx": "all"},
    "finding": finding(
        IMAGE_ALT,
        '<img src="/team/jane-doe.jpg" width="64" height="64" class="avatar rounded-full">',
        "ul.team-grid > li:nth-child(1) > img",
    ),
    "expected": {
        "file": "src/pages/Team.tsx",
        "line_contains": "<Avatar src=",
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "alt",
    },
    "notes": "No <img> in any changed file. Correct answer is the call site, passing an alt prop.",
}

# 6. decoy: two images, one already fine ---------------------------------------
CASES["decoy"] = {
    "files": {
        "src/Gallery.tsx": """export function Gallery() {
  return (
    <div className="gallery">
      <img src="/photos/beach.jpg" alt="A sandy beach at sunset" />
      <img src="/photos/forest.jpg" />
    </div>
  )
}
"""
    },
    "diff": {"src/Gallery.tsx": "all"},
    "finding": finding(IMAGE_ALT, '<img src="/photos/forest.jpg">', "div.gallery > img:nth-child(2)"),
    "expected": {
        "file": "src/Gallery.tsx",
        "line_contains": "forest.jpg",
        "line_not_contains": "beach",
        "disposition_in": ["suggestion", "annotation"],
    },
    "notes": "Picking the beach line would produce a patch that changes nothing wrong.",
}

# 7. not in PR: the false-positive case ----------------------------------------
CASES["not-in-pr"] = {
    "files": {
        "src/utils/format.ts": """export function formatPrice(cents: number, currency = 'EUR') {
  return new Intl.NumberFormat('de-DE', { style: 'currency', currency }).format(cents / 100)
}
""",
        "src/components/PriceTag.tsx": """import { formatPrice } from '../utils/format'

export function PriceTag({ cents }: { cents: number }) {
  return <span className="price">{formatPrice(cents)}</span>
}
""",
    },
    "diff": {"src/utils/format.ts": "all", "src/components/PriceTag.tsx": "all"},
    "finding": finding(
        IMAGE_ALT, '<img src="/banner/summer-sale.png" class="banner-img">', "main > img.banner-img"
    ),
    "expected": {"file": None, "disposition_in": ["drop"]},
    "notes": "The banner is rendered by an untouched file. Any location here is wrong.",
}

# 8. document level ------------------------------------------------------------
CASES["document-level"] = {
    "files": {
        "app/layout.tsx": """import './globals.css'

export const metadata = { title: 'Acme' }

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html>
      <body className="antialiased">{children}</body>
    </html>
  )
}
"""
    },
    "diff": {"app/layout.tsx": "all"},
    "finding": finding(HTML_LANG, '<html><head></head><body class="antialiased">...</body></html>', "html"),
    "expected": {
        "file": "app/layout.tsx",
        "line_contains": "<html>",
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "lang=",
    },
    "notes": "The html literal is enormous; only the tag matters.",
}

# 9. inferable content ---------------------------------------------------------
CASES["inferable-content"] = {
    "files": {
        "src/Newsletter.tsx": """export function Newsletter() {
  return (
    <form className="newsletter" action="/subscribe" method="post">
      <input type="email" name="email" placeholder="you@example.com" required />
      <button type="submit">Subscribe</button>
    </form>
  )
}
"""
    },
    "diff": {"src/Newsletter.tsx": "all"},
    "finding": finding(
        LABEL,
        '<input type="email" name="email" placeholder="you@example.com" required="">',
        "form.newsletter > input",
    ),
    "expected": {
        "file": "src/Newsletter.tsx",
        "line_contains": 'name="email"',
        "disposition_in": ["suggestion", "annotation"],
        "requires_human_content": False,
    },
    "notes": "name=email + type=email: the label text is knowable. Should not be flagged for a human.",
}

# 10. needs human content --------------------------------------------------------
CASES["needs-human-content"] = {
    "files": {
        "src/Modal.tsx": """import { XIcon } from './icons'

export function Modal({ children, onDismiss }: Props) {
  return (
    <div role="dialog" className="modal">
      <button className="modal-close" onClick={onDismiss}>
        <XIcon />
      </button>
      {children}
    </div>
  )
}
"""
    },
    "diff": {"src/Modal.tsx": "all"},
    "finding": finding(BUTTON_NAME, '<button class="modal-close"><svg></svg></button>', "div.modal > button"),
    "expected": {
        "file": "src/Modal.tsx",
        "line_contains": "modal-close",
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "aria-label",
    },
    "notes": "Handler is onDismiss, so 'Dismiss'/'Close' is inferable; either a confident suggestion or a flagged annotation is acceptable. A silent wrong label is not.",
}

# 11. twin components, distinguished by data-testid ------------------------------
CASES["twin-components"] = {
    "files": {
        "src/Toolbar.tsx": """import { IconButton } from './IconButton'
import { BoldIcon, ItalicIcon } from './icons'

export function Toolbar() {
  return (
    <div className="toolbar">
      <IconButton data-testid="fmt-bold" icon={<BoldIcon />} label="Bold" />
      <IconButton data-testid="fmt-italic" icon={<ItalicIcon />} />
    </div>
  )
}
"""
    },
    "diff": {"src/Toolbar.tsx": "all"},
    "finding": finding(
        BUTTON_NAME,
        '<button data-testid="fmt-italic" class="icon-btn"><svg></svg></button>',
        "div.toolbar > button:nth-child(2)",
    ),
    "expected": {
        "file": "src/Toolbar.tsx",
        "line_contains": "fmt-italic",
        "line_not_contains": "fmt-bold",
        "disposition_in": ["suggestion", "annotation"],
        "patch_contains": "label=",
    },
    "notes": "The bold one already passes a label; the fix is the same prop on the italic one.",
}

# 12. located but outside the diff hunks ----------------------------------------
CASES["out-of-hunk"] = {
    "files": {
        "src/Card.tsx": """import { Badge } from './Badge'

export function Card({ title, image, isNew }: Props) {
  return (
    <article className="card">
      <img src={image} className="card-img" />
      <h3>{title}</h3>
      {isNew && <Badge tone="info">New</Badge>}
    </article>
  )
}
"""
    },
    "diff": {"src/Card.tsx": [8, 8]},  # only the Badge line changed
    "finding": finding(IMAGE_ALT, '<img src="/cards/widget.png" class="card-img">', "article.card > img"),
    "expected": {
        "file": "src/Card.tsx",
        "line_contains": "card-img",
        "disposition_in": ["annotation"],
        "never": ["suggestion"],
    },
    "notes": "The img line was not touched by this PR. GitHub would 422 a suggestion there; we must not try.",
}


def main() -> None:
    for name, case in CASES.items():
        d = ROOT / name
        (d / "files").mkdir(parents=True, exist_ok=True)
        for path, content in case["files"].items():
            f = d / "files" / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        meta = {k: v for k, v in case.items() if k != "files"}
        (d / "case.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(CASES)} cases to {ROOT}")


if __name__ == "__main__":
    main()
