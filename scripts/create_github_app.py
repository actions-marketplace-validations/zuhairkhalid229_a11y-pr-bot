"""Create the GitHub App from a manifest, in one browser click.

    python scripts/create_github_app.py --domain a11y.example.com --api-url https://a11y-api-xxx.run.app

Registering a GitHub App by hand means filling a long form and ticking exactly
five permissions and three events. Getting one wrong produces failures that look
like bugs -- a missing Deployments permission, for instance, means every scan
silently times out into `no_preview` with nothing in the logs that reads as an
error.

GitHub's app-manifest flow removes that entirely: we hand GitHub the complete
configuration, you press one button, and GitHub hands back the App ID, a freshly
generated private key and the webhook secret. Those are written straight to .env.

The flow, for the curious:
  1. this script starts a local server on 127.0.0.1:<port>
  2. your browser posts the manifest to github.com/settings/apps/new
  3. you review and click "Create GitHub App"
  4. GitHub redirects back here with a temporary code
  5. we exchange the code for the credentials, once, and write them to .env

The code is single-use and expires in an hour. Nothing is sent anywhere except
github.com.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The five permissions and three events the app actually needs. Anything more
# costs installs; anything less breaks a stage.
PERMISSIONS = {
    "checks": "write",  # create and update the check run
    "pull_requests": "write",  # post the review with suggestions
    "contents": "read",  # read JSX from the diff to locate the element
    "deployments": "read",  # REQUIRED for deployment_status webhooks
    "metadata": "read",  # mandatory for every GitHub App
}
EVENTS = ["pull_request", "deployment_status", "check_run"]


def build_manifest(name: str, domain: str, api_url: str, redirect_url: str) -> dict:
    homepage = f"https://{domain}"
    return {
        "name": name,
        "url": homepage,
        "hook_attributes": {"url": f"{api_url.rstrip('/')}/webhooks/github", "active": True},
        "redirect_url": redirect_url,
        "setup_url": f"{homepage}/setup",
        "setup_on_update": True,
        "callback_urls": [f"{homepage}/api/auth/github/callback"],
        # Off on purpose: it adds a consent screen to every install.
        "request_oauth_on_install": False,
        "public": True,
        "default_permissions": PERMISSIONS,
        "default_events": EVENTS,
        "description": (
            "Scans your preview deployment with axe-core, traces each WCAG 2.2 "
            "violation back to the JSX that produced it, and posts a fix you can "
            "commit in one click."
        ),
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Create the GitHub App</title>
<style>
 body{{font:16px/1.6 system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px;color:#16181d}}
 @media(prefers-color-scheme:dark){{body{{background:#0f1115;color:#e8eaed}}}}
 code{{background:#8881;padding:2px 6px;border-radius:4px}}
 button{{font:inherit;padding:12px 22px;border-radius:8px;border:0;background:#1f6feb;color:#fff;cursor:pointer}}
 ul{{padding-left:20px}} li{{margin:4px 0}}
</style></head>
<body>
<h1>Create the GitHub App</h1>
<p>Pressing the button sends this configuration to GitHub. You will see a review
screen before anything is created.</p>
<ul>
 <li>Name: <code>{name}</code></li>
 <li>Webhook: <code>{hook}</code></li>
 <li>Permissions: checks&nbsp;write, pull&nbsp;requests&nbsp;write, contents&nbsp;read,
     deployments&nbsp;read, metadata&nbsp;read</li>
 <li>Events: pull_request, deployment_status, check_run</li>
</ul>
<form action="https://github.com/settings/apps/new?state={state}" method="post" id="f">
  <input type="hidden" name="manifest" id="manifest">
  <button type="submit">Create GitHub App</button>
</form>
<p style="color:#888;font-size:14px">After you approve, GitHub sends the credentials
back here and this page will tell you it is done. You can close it then.</p>
<script>
  document.getElementById('manifest').value = {manifest_json};
</script>
</body></html>
"""

DONE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px}}
@media(prefers-color-scheme:dark){{body{{background:#0f1115;color:#e8eaed}}}}</style></head>
<body><h1>{title}</h1><p>{body}</p></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    state: str = ""
    page: str = ""
    result: dict | None = None
    error: str | None = None
    done = threading.Event()

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            self._send(200, Handler.page)
            return

        if parsed.path != "/callback":
            self._send(404, DONE.format(title="Not found", body="Nothing here."))
            return

        # CSRF: the state we generated must come back unchanged.
        if params.get("state", [""])[0] != Handler.state:
            Handler.error = "state mismatch — refusing to continue"
            self._send(400, DONE.format(title="Rejected", body=Handler.error))
            Handler.done.set()
            return

        code = params.get("code", [""])[0]
        if not code:
            Handler.error = "GitHub did not return a code (was the app creation cancelled?)"
            self._send(400, DONE.format(title="Cancelled", body=Handler.error))
            Handler.done.set()
            return

        try:
            Handler.result = convert(code)
            self._send(
                200,
                DONE.format(
                    title="App created",
                    body=f"<strong>{Handler.result['name']}</strong> is registered and the "
                    "credentials were written to <code>.env</code>. You can close this tab.",
                ),
            )
        except Exception as exc:
            Handler.error = f"{type(exc).__name__}: {exc}"
            self._send(500, DONE.format(title="Conversion failed", body=Handler.error))
        Handler.done.set()


def convert(code: str) -> dict:
    """Exchange the one-time code for the app's real credentials."""
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "a11y-pr-bot-setup",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def write_env(app: dict, env_path: Path) -> list[str]:
    """Merge credentials into .env without clobbering anything already there."""
    import base64

    values = {
        "GITHUB_APP_ID": str(app["id"]),
        "GITHUB_WEBHOOK_SECRET": app["webhook_secret"],
        # base64 so the PEM survives a single-line env var everywhere.
        "GITHUB_PRIVATE_KEY": base64.b64encode(app["pem"].encode()).decode(),
    }

    existing: list[str] = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen, out = set(), []
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # The raw PEM as well: gcloud secrets create wants a file.
    pem_path = env_path.parent / f"{app['slug']}.private-key.pem"
    pem_path.write_text(app["pem"], encoding="utf-8")
    return [str(env_path), str(pem_path)]


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default="a11y-pr-bot", help="App name; must be globally unique on GitHub")
    parser.add_argument("--domain", required=True, help="Dashboard domain, e.g. a11y.example.com")
    parser.add_argument("--api-url", required=True, help="Cloud Run URL of the api service")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--env", default=str(ROOT / ".env"))
    args = parser.parse_args()

    port = free_port(args.port)
    redirect = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(24)
    manifest = build_manifest(args.name, args.domain, args.api_url, redirect)

    Handler.state = state
    Handler.page = PAGE.format(
        name=args.name,
        hook=manifest["hook_attributes"]["url"],
        state=state,
        manifest_json=json.dumps(json.dumps(manifest)),
    )

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Opening {url}")
    print("  Press the button, review GitHub's screen, and approve.\n")
    try:
        webbrowser.open(url)
    except Exception:
        print(f"  (could not open a browser automatically — visit {url})")

    if not Handler.done.wait(timeout=600):
        print("  Timed out after 10 minutes.")
        return 1
    server.shutdown()

    if Handler.error or not Handler.result:
        print(f"  Failed: {Handler.error}")
        return 1

    app = Handler.result
    written = write_env(app, Path(args.env))
    print(f"  Created: {app['name']}  (id {app['id']}, slug {app['slug']})")
    print(f"  Install it at: {app['html_url']}/installations/new")
    for path in written:
        print(f"  Wrote: {path}")
    print("\n  .env now has GITHUB_APP_ID, GITHUB_WEBHOOK_SECRET and GITHUB_PRIVATE_KEY.")
    print("  Keep the .pem out of git — .gitignore already covers *.pem.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
