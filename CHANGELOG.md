# Changelog

Notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

First public release candidate. Not yet deployed to production.

### Added
- GitHub App webhook service: HMAC verification, delivery dedupe, sub-second
  202, queued check run on every pull request.
- Scan state machine merging the `pull_request` and `deployment_status` halves
  transactionally, with a 15-minute no-preview fallback.
- Playwright + vendored axe-core 4.13.0 scanner reporting WCAG 2.2 AA and
  EN 301 549 clauses, with `incomplete` results surfaced as "needs review"
  rather than dropped.
- Source mapper: deterministic literal search over the PR diff, then a model
  pass that returns a candidate index, then verification against the real file.
- Poster: one batched review per push, ` ```suggestion ` blocks for verified
  patches, fixed-thread replies and resolution, dedupe across pushes.
- Quota enforcement; public repositories are never metered.
- Next.js dashboard with Firebase GitHub auth and direct Firestore reads under
  `firestore.rules`.
- `scripts/eval_mapper.py` — 12-case evaluation harness gating on zero wrong
  suggestions.
- `scripts/smoke_test.py` — nine-stage end-to-end check against a real install.

### Known issues
- Only the preview root is scanned; multi-page crawling is not implemented.
- Netlify password-protected sites are unsupported (cookie-based, not a header).
- Structured output via the Gemini API's server-side response schema hangs; the
  schema is enforced in-prompt and validated locally instead. Re-enable with
  `GEMINI_USE_RESPONSE_SCHEMA=1` if upstream is fixed.
