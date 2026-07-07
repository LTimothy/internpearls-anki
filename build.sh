#!/usr/bin/env bash
# Repackage the add-on into internpearls.ankiaddon (files at zip root).
set -e
cd "$(dirname "$0")/internpearls"
rm -rf __pycache__ notes_snapshot.json installed.json
zip -j ../internpearls.ankiaddon __init__.py manifest.json config.json config.md >/dev/null
echo "built internpearls.ankiaddon"
