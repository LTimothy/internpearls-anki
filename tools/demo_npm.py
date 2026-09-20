#!/usr/bin/env python3
"""Run the npm CLI bundled with the pinned demo Node.js runtime."""

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_npm(arguments, *, root=ROOT, runner=None):
    root = Path(root)
    node = root / ".demo-tools" / "node" / "bin" / "node"
    npm_cli = (
        root / ".demo-tools" / "node" / "lib" / "node_modules" /
        "npm" / "bin" / "npm-cli.js"
    )
    if not node.is_file():
        raise FileNotFoundError(f"pinned Node binary not found: {node}")
    if not npm_cli.is_file():
        raise FileNotFoundError(f"bundled npm CLI not found: {npm_cli}")
    command = [str(node), str(npm_cli), *arguments]
    if runner is not None:
        return runner(command, cwd=root)
    environment = os.environ.copy()
    environment["npm_config_cache"] = str(root / ".demo-tools" / "npm-cache")
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(
        root / ".demo-tools" / "playwright-browsers")
    return subprocess.call(command, cwd=root, env=environment)


def main(argv=None):
    return run_npm(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
