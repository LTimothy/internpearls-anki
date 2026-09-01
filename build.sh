#!/usr/bin/env bash
# Repackage the add-on into internpearls.ankiaddon (files at zip root).
set -e
cd "$(dirname "$0")/internpearls"
rm -rf __pycache__ notes_snapshot.json installed.json
find vendor -name "__pycache__" -exec rm -rf {} +
# Remove the previous archive first: zip otherwise appends into it, so a module
# deleted from the source tree would silently live on inside the package.
rm -f ../internpearls.ankiaddon
zip -j ../internpearls.ankiaddon ./*.py manifest.json config.json config.md >/dev/null
# -j junks paths, so the bundled skill and the vendored pypdf (which must keep
# their directory structure) are added separately, without -j.
zip -r ../internpearls.ankiaddon skills vendor >/dev/null
echo "built internpearls.ankiaddon"

# Mirror the add-on source (plus the shared mock-Anki harness) into docs/addon/
# for the GitHub Pages live demo, which executes these exact files under
# Pyodide. tests/test_demo_parity.py fails if these copies go stale.
mkdir -p ../docs/addon
rm -f ../docs/addon/*.py
cp ./*.py ../docs/addon/
cp ../tests/mock_anki.py ../docs/addon/mock_anki.py
(cd ../docs/addon && ls ./*.py | sed 's|^\./||' | \
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split()))' \
  > files.json)
echo "mirrored add-on source into docs/addon/ for the live demo"
