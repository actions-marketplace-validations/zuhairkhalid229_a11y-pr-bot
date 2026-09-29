# The action

`action.yml` at the repository root; the entry point is `scan.py` here.

It reuses `worker/scanner.py` unchanged — the same engine the hosted app runs —
so a scan result from the action and from the app are directly comparable.
`action/requirements.txt` is deliberately a subset of the service requirements:
no Firestore, no GitHub client, no Gemini.

What the action does not do is map findings back to source. That needs the pull
request diff and a model pass, and it is what the hosted app adds. Keeping that
line honest matters more than making the free tier look bigger than it is.

Self-tested on every push by `.github/workflows/action-selftest.yml`, which
serves the two fixtures from `tests/worker/fixtures/` and asserts the action
finds violations in one and none in the other.
