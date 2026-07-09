#!/usr/bin/env bash
# Repackage the add-on into internpearls.ankiaddon (files at zip root).
set -e
cd "$(dirname "$0")/internpearls"
rm -rf __pycache__ notes_snapshot.json installed.json
# Remove the previous archive first: zip otherwise appends into it, so a module
# deleted from the source tree would silently live on inside the package.
rm -f ../internpearls.ankiaddon
zip -j ../internpearls.ankiaddon ./*.py manifest.json config.json config.md >/dev/null
echo "built internpearls.ankiaddon"
