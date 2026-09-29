"""Load the vendored axe-core and verify it against the recorded digest."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

_VENDOR = Path(__file__).parent / "vendor"


class AxeIntegrityError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_axe() -> tuple[str, str]:
    """Return (source, version). Raises if the file does not match axe.json."""
    meta = json.loads((_VENDOR / "axe.json").read_text(encoding="utf-8"))
    source_bytes = (_VENDOR / "axe.min.js").read_bytes()

    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != meta["sha256"]:
        raise AxeIntegrityError(
            f"axe.min.js sha256 {digest[:12]}… does not match axe.json "
            f"{meta['sha256'][:12]}…; re-run scripts/vendor_axe.sh"
        )
    return source_bytes.decode("utf-8"), meta["version"]
