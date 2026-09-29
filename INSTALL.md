Written for: a developer who just found the app and is deciding whether to install it. This is the page the Marketplace listing and the repo README both link to.

# Accessibility checks on every pull request

This GitHub App scans your preview deployment with [axe-core](https://github.com/dequelabs/axe-core), maps each violation back to the JSX that produced it, and leaves a review comment you can commit with one click.

```
open a PR  →  Vercel/Netlify deploys a preview  →  we scan it  →  review comments on the diff
```

It reports against **WCAG 2.2 Level AA** and maps every finding to its **EN 301 549** clause — the harmonised standard behind the European Accessibility Act.

## What it does not do

Being straight about this up front, because the accessibility tooling market has earned the scepticism:

- **Automated checks catch roughly a third of WCAG 2.2 success criteria.** Nothing that runs without a human can tell you whether your alt text is *meaningful*, whether your focus order makes sense, or whether your error messages are understandable. Findings this app cannot decide automatically are reported separately as "needs manual review" rather than quietly dropped.
- **This is not an overlay.** No widget, no script tag, no runtime patching of your site. Every fix is a change in your own source, reviewed by you.
- **Passing this check is not a legal compliance certificate.** It is evidence of conformance testing against a documented standard. Anyone selling you a guarantee is selling you something they cannot deliver.

## Install

1. **Install the app** on the repositories you want scanned. It asks for five permissions:

   | Permission | Why |
   |---|---|
   | Checks · read & write | Create the check run on your PR |
   | Pull requests · read & write | Post the review with the suggested fixes |
   | Contents · read-only | Read the JSX in your diff to locate the element and write the patch |
   | Deployments · read-only | Notice when your preview deployment is ready |
   | Metadata · read-only | Mandatory for all GitHub Apps |

   Source files are read only from the diff of an open PR, used in-memory to generate the patch, and not retained after the scan.

2. **Sign in to the dashboard** at your instance URL. Sign-in requests `read:user` only — repository access comes from the installation above, not from the OAuth grant.

3. **Connect a preview deployment** if you have not already. Anything that reports deployment status to GitHub works: Vercel, Netlify, Cloudflare Pages, Render. Without a preview there is nothing to scan, and the check run will tell you so rather than failing silently.

4. **If your previews are protected** — Vercel Authentication is on by default for new projects — generate a *Protection Bypass for Automation* token (Vercel → Project → Settings → Deployment Protection) and paste it on the repository page in the dashboard. It is encrypted before storage and never displayed again.

5. **Open a pull request.** A check appears within seconds of the preview going live.

## What you will see

A single review per push, never one comment per finding:

- **Fixable in one click** — a ` ```suggestion ` block you commit from the GitHub UI. Only posted when the patch is verified against your actual file, sits inside the diff, and is structurally balanced.
- **Needs your judgement** — the same information with the patch as a plain code block, because the correct fix needs something only you know (what the image depicts, where the link goes).
- **On later pushes** — fixed issues get a ✅ reply and their thread resolved; issues still open are listed, not re-posted. Nothing new and nothing fixed means no comment at all.

Findings that cannot be confidently located in your diff are listed in the check run rather than guessed at. A wrong one-click fix is worse than no fix.

## Pricing

| | Free | Pro · $29/mo | Team · $99/mo |
|---|---|---|---|
| Public repositories | Unlimited | Unlimited | Unlimited |
| Private repositories | 1 | 5 | Unlimited |
| Private-repo scans | 50 / month | 500 / month | Unlimited |
| EN 301 549 conformance report export | — | — | ✓ |

Public repositories are never metered. If you go over a limit the check run says so and no scan runs — nothing breaks, nothing is charged silently.

## Troubleshooting

**"No preview deployment found"** — no provider reported a successful deployment for that commit within 15 minutes. Check that your deploy succeeded, that it was not superseded by a newer push, and that the app has the *Deployments* permission.

**"Could not scan the preview — HTTP 401"** — deployment protection. Add the bypass token (step 4).

**The check ran but left no comments** — either nothing was found, or the findings could not be located in the files this PR changed. Open the check run; everything found is listed there.

**Comments stopped appearing on new pushes** — that is deliberate. An issue already commented on is not re-posted while its thread is open.

## Questions worth asking us

- Scanned pages: currently the preview root. Multi-page crawling is next.
- Data retained: scan results, findings, and the review comments posted. Not your source code.
- Removing it: uninstalling from GitHub stops everything immediately.
