Written for: you, to paste into the GitHub Marketplace listing form and the OSS repo README. Each section maps to one field in that form.

# Marketplace listing copy

## Name

**a11y-pr-bot**

Check availability before committing — GitHub App names are globally unique, and this one is plausible enough that someone may have taken it. Ranked alternatives, all of which read as a tool rather than a brand:

1. `axe-pr` — closest to what it is; leans on axe-core's recognition
2. `wcag-guard`
3. `preview-a11y`

Whatever you pick, the OSS Action repo should share the name — the two halves of the funnel must look like one product.

## Tagline (short description, ~80 chars)

> WCAG 2.2 and EN 301 549 checks on every PR, with one-click fixes for your JSX.

Three things in one line: the standards (credibility with whoever asked for this), *on every PR* (where it runs), *one-click fixes* (why it beats a linter).

## Categories

- **Primary:** Code quality
- **Secondary:** Continuous integration

Not "Monitoring" or "Project management" — developers browsing those are not looking for this. Code quality is where Codecov, CodeRabbit and Snyk live, and that is the shelf this belongs on.

## Description (listing body)

> ### Accessibility review on the pull request, not in a quarterly audit
>
> Every PR gets its preview deployment scanned with axe-core. Each violation is traced back to the JSX that rendered it and posted as a review comment — most of them as a `suggestion` block you commit with one click.
>
> Reported against **WCAG 2.2 Level AA**, with every finding mapped to its **EN 301 549** clause, the harmonised standard behind the European Accessibility Act.
>
> **What makes it different**
>
> - **Fixes, not findings.** Other tools hand you a rule id and a CSS selector from the rendered DOM. This one gives you the file, the line, and the patch — because it reads the JSX in your diff.
> - **It does not guess.** A suggestion is posted only when the patch is verified against your actual file, sits inside the diff, and is structurally balanced. Anything less confident becomes a comment you review, or is left in the check run. A wrong one-click fix is worse than no fix.
> - **It knows what it cannot know.** Whether alt text is *meaningful*, whether focus order makes sense — these are flagged for a human, not silently guessed. Automated checks cover roughly a third of WCAG 2.2, and this app says so instead of implying otherwise.
> - **Quiet by default.** One review per push. Fixed issues get a ✅ and their thread resolved. Open issues are not re-posted. Nothing new means no comment.
> - **Not an overlay.** No widget, no script tag. Changes land in your source, reviewed by you.
>
> **Free for every public repository**, unmetered. Private repos start at one repository and 50 scans a month.
>
> Requires a preview deployment that reports to GitHub — Vercel, Netlify, Cloudflare Pages, Render.

## Screenshots to capture

In this order; the first is the one that sells it. Capture on a real public repo with real findings, in light mode at 1280px, and redact nothing (a real repo name is credibility).

1. **A `suggestion` comment on a PR diff** with the *Commit suggestion* button visible. This is the product. Nothing else in the listing matters as much.
2. **The review summary comment** — the counts line and the rule table with WCAG and EN 301 549 columns side by side.
3. **A second push showing the delta** — a ✅ "Fixed in `abc1234`" reply on a resolved thread, with "2 still open · 3 fixed" in the new summary. This is what proves it is not a spam bot.
4. **The check run page** — impact table, rule table, and the "needs manual review" line. The honesty is a feature; show it.
5. **An annotation comment** — a fix flagged as needing human judgement, showing the tool declining to guess at alt text.
6. **The dashboard** — installation page with scan history and the monthly usage row.

Skip: the sign-in screen, the empty state, anything with a fake repo name.

## Support and privacy fields

- **Support URL:** the OSS repo's Issues tab, not an email address. Developers trust a public issue tracker and it doubles as social proof.
- **Privacy policy:** must state plainly what `Contents: read` means in practice — source files are read only from the diff of an open PR, held in memory to generate the patch, and not retained. That permission is the single biggest install-time hesitation; answer it before it is asked.
- **Pricing on the listing:** list the **free plan only.** Paid Marketplace plans require verified publisher status (organisation, enforced 2FA, verified domain) *and* a minimum of 100 installations, so paid tiers cannot launch here. Billing runs through Paddle on your own domain; the listing is a discovery channel.

## Launch-day checklist

- [ ] App name matches the OSS Action repo name
- [ ] Free plan only; paid tiers on your own site
- [ ] Support URL points at public Issues
- [ ] Privacy policy addresses `Contents: read` explicitly
- [ ] Screenshot 1 is a real one-click suggestion on a real repo
- [ ] `INSTALL.md` is linked from the listing and is the repo README's first link
- [ ] Your own docs site scores 100 — you will be audited by strangers within a day
