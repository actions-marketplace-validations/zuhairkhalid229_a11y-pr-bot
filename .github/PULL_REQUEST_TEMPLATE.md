## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How you verified it

<!-- "Tests pass" is less useful than what you actually checked. -->

- [ ] `pytest -q`
- [ ] `ruff check . && ruff format --check .`
- [ ] `cd web && npx tsc --noEmit && npm run build` (if the dashboard changed)

## If you touched the source mapper

Re-run the evaluation and paste the numbers. A change that raises recall **and**
raises the false-suggestion rate is a regression.

```
GEMINI_API_KEY=... python scripts/eval_mapper.py
```

| Metric | Before | After | Target |
|---|---|---|---|
| False-suggestion rate | | | < 2% |
| Location precision | | | ≥ 95% |
| Recall | | | ≥ 70% |

## Anything reviewers should look at closely

<!-- Trade-offs you made, things you were unsure about, paths that reach real PRs. -->
