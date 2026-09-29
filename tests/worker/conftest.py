"""A real HTTP server for the fixtures, so page.goto exercises the same code
path as a preview deployment -- not page.set_content."""

import http.server
import os
import socketserver
import threading
import time
from pathlib import Path

import pytest

# Tight timeouts so the transient-failure test does not take 30 seconds.
os.environ.setdefault("WORKER_NAVIGATION_TIMEOUT_MS", "4000")
os.environ.setdefault("WORKER_SETTLE_TIMEOUT_MS", "500")

FIXTURES = Path(__file__).parent / "fixtures"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def do_GET(self):
        # /hang never answers: drives navigation into a timeout (transient).
        if self.path == "/hang":
            time.sleep(10)
            return
        super().do_GET()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="session")
def fixture_server():
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler) as httpd:
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
