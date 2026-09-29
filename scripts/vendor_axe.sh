#!/usr/bin/env bash
# Vendor a pinned axe-core build into worker/vendor/.
#
# Why vendor instead of `pip install axe-playwright-python` or fetching at
# runtime:
#   * Rule ids and tags change between axe releases. Fingerprints and the WCAG
#     mapping depend on them, so the version must be pinned and reviewed, not
#     floated by a transitive dependency.
#   * No npm and no network in the container. The file is ~600KB, MPL-2.0
#     licensed; redistribution requires shipping the LICENSE alongside, which
#     this script does.
#   * The sha256 is written next to the file so a tampered or partial download
#     fails loudly at import time (see worker/axe.py).
set -euo pipefail

VERSION="${1:-4.13.0}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/worker/vendor"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "vendoring axe-core@${VERSION} -> ${DEST}"
curl -sSfL "https://registry.npmjs.org/axe-core/-/axe-core-${VERSION}.tgz" -o "$TMP/axe.tgz"
tar -xzf "$TMP/axe.tgz" -C "$TMP" package/axe.min.js package/LICENSE package/package.json

cp "$TMP/package/axe.min.js" "$DEST/axe.min.js"
cp "$TMP/package/LICENSE"    "$DEST/LICENSE-axe-core"
python - "$DEST" "$VERSION" <<'PY'
import hashlib, json, pathlib, sys
dest, version = pathlib.Path(sys.argv[1]), sys.argv[2]
digest = hashlib.sha256((dest / "axe.min.js").read_bytes()).hexdigest()
(dest / "axe.json").write_text(json.dumps({"version": version, "sha256": digest}, indent=2) + "\n")
print(f"axe-core {version} sha256={digest}")
PY
