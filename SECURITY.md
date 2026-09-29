# Security policy

## Reporting a vulnerability

Please report privately, not as a public issue:

- [open a private advisory](https://github.com/zuhairkhalid229/a11y-pr-bot/security/advisories/new), or
- email zuhairkhalid229@gmail.com.

Include what you did, what happened, and what you expected. A proof of concept
against your own installation is ideal. Expect an acknowledgement within a few
days; this is a small project, not a vendor with a SOC.

Please do not test against repositories or installations you do not own.

## What this app can reach

Worth knowing before you audit it. With the five permissions it requests, an
installation grants the app the ability to:

- **read the content of files changed in an open pull request** (`Contents:
  read`) — this is the broadest permission and the one most worth scrutinising;
- **write pull request review comments** (`Pull requests: write`);
- **create and update check runs** (`Checks: write`);
- read deployment status and repository metadata.

Source files are read from the diff of an open PR, held in memory to generate a
patch, and are **not persisted**. What *is* persisted is scan results, findings
(including the rendered HTML of failing elements and the source file path and
line), and the review comments posted.

## Where the interesting boundaries are

If you are looking for something to break, these are the load-bearing checks:

| Boundary | Code | Property it protects |
|---|---|---|
| Webhook authenticity | `app/webhooks/security.py` | Only GitHub can trigger a scan. HMAC over the **raw** body, constant-time compare. |
| Webhook replay | `app/store/firestore.py` `claim_delivery` | A redelivered event cannot double-post. |
| Credential scope | `app/github/auth.py` | Installation tokens are minted per installation, cached in-process only, never written to Firestore. |
| Dashboard authorisation | `firestore.rules` + `app/api/router.py` | A signed-in user sees only installations GitHub says they administer. Rules cannot call GitHub, so membership is materialised by `POST /api/link`. |
| Secret at rest | `app/crypto.py` | Vercel bypass tokens are Fernet-encrypted; the key lives in Secret Manager and never reaches a browser. Clients have **no** write path to repo config. |
| Worker exposure | Cloud Run IAM | The scanner is `--no-allow-unauthenticated`; only Cloud Tasks' OIDC identity may invoke it. |
| Patch safety | `worker/mapper/verify.py`, `decide.py` | A one-click suggestion requires the patch text to be verified against the real file, inside the diff, and structurally balanced. |

## Threat model, briefly

**The scanner loads untrusted web pages.** It navigates to a preview deployment
chosen by the repository owner and runs axe-core there. Chromium runs headless
and sandboxed in a container with no credentials mounted; the installation token
is never in the browser process. A malicious preview can at most see the
scanner's own request headers — which is why the Vercel bypass token is
resolved per repo at scan time and scoped to that repo.

**The model output is untrusted input.** Gemini's response is treated as a
suggestion to be verified, never as an instruction. It cannot name a file
outside the candidate set (it returns an index, not a path), and it cannot cause
a commit — only a human clicking "Commit suggestion" does that.

**Prompt injection is in scope.** A repository could contain JSX crafted to
manipulate the mapper. The blast radius is bounded by the same checks: the
worst case is a bad patch proposal on that same repository's own PR, which a
human must still accept. If you find a way to escape that bound — to affect
another repository, to exfiltrate a token, or to get a suggestion posted that
was not verified against the file — that is a genuine vulnerability and we want
to hear about it.

## Supported versions

Pre-1.0. Only the latest `main` receives fixes.
