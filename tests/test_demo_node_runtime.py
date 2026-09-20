import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from tools import demo_npm, fetch_demo_node


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PINS = {
    "darwin-arm64": (
        "node-v22.14.0-darwin-arm64.tar.gz",
        "e9404633bc02a5162c5c573b1e2490f5fb44648345d64a958b17e325729a5e42",
    ),
    "darwin-x64": (
        "node-v22.14.0-darwin-x64.tar.gz",
        "6698587713ab565a94a360e091df9f6d91c8fadda6d00f0cf6526e9b40bed250",
    ),
    "linux-x64": (
        "node-v22.14.0-linux-x64.tar.gz",
        "9d942932535988091034dc94cc5f42b6dc8784d6366df3a36c4c9ccb3996f0c2",
    ),
}


class _Response(io.BytesIO):
    def __init__(self, payload, url):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._url


def _archive(entries, top="node-v22.14.0-linux-x64"):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, contents, kind in entries:
            member = tarfile.TarInfo(name.format(top=top))
            if kind == "file":
                member.size = len(contents)
                member.mode = 0o755 if name.endswith("/bin/node") else 0o644
                archive.addfile(member, io.BytesIO(contents))
            elif kind == "dir":
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = contents.decode()
                archive.addfile(member)
            else:
                raise AssertionError(kind)
    return payload.getvalue()


def _valid_archive():
    return _archive([
        ("{top}", b"", "dir"),
        ("{top}/bin", b"", "dir"),
        ("{top}/bin/node", b"synthetic node", "file"),
        ("{top}/lib/node_modules/npm/bin/npm-cli.js", b"synthetic npm", "file"),
    ])


def _write_lock(tmp_path, payload, *, digest=None, length=None):
    filename = "node-v22.14.0-linux-x64.tar.gz"
    lock = {
        "version": "22.14.0",
        "archives": {
            "linux-x64": {
                "url": "https://nodejs.org/dist/v22.14.0/" + filename,
                "sha256": digest or hashlib.sha256(payload).hexdigest(),
                "length": len(payload) if length is None else length,
            }
        },
    }
    path = tmp_path / "node-lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path, lock["archives"]["linux-x64"]["url"]


def _install(tmp_path, payload, **kwargs):
    response_url = kwargs.pop("response_url", None)
    lock_path, url = _write_lock(tmp_path, payload, **kwargs)
    response_url = response_url or url
    return fetch_demo_node.install_node(
        tmp_path / "node",
        lock_path=lock_path,
        system="Linux",
        machine="x86_64",
        opener=lambda request, timeout: _Response(payload, response_url),
        version_probe=lambda node: "v22.14.0",
    )


def test_lock_pins_the_supported_official_archives():
    lock = json.loads((ROOT / "demo" / "node-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == "22.14.0"
    assert set(lock["archives"]) == set(EXPECTED_PINS)
    for platform_key, (filename, digest) in EXPECTED_PINS.items():
        entry = lock["archives"][platform_key]
        assert entry["url"] == "https://nodejs.org/dist/v22.14.0/" + filename
        assert entry["sha256"] == digest
        assert isinstance(entry["length"], int) and entry["length"] > 0


def test_unsupported_platform_is_rejected(tmp_path):
    payload = _valid_archive()
    lock_path, _url = _write_lock(tmp_path, payload)
    with pytest.raises(RuntimeError, match="unsupported platform"):
        fetch_demo_node.select_archive(
            lock_path=lock_path, system="Windows", machine="AMD64")


def test_wrong_archive_digest_is_rejected(tmp_path):
    payload = _valid_archive()
    with pytest.raises(RuntimeError, match="SHA-256"):
        _install(tmp_path, payload, digest="0" * 64)
    assert not (tmp_path / "node").exists()


def test_wrong_archive_length_is_rejected(tmp_path):
    payload = _valid_archive()
    with pytest.raises(RuntimeError, match="length"):
        _install(tmp_path, payload, length=len(payload) + 1)
    assert not (tmp_path / "node").exists()


@pytest.mark.parametrize("entries, phrase", [
    ([
        ("{top}/bin/node", b"node", "file"),
        ("{top}/../escape", b"escape", "file"),
    ], "unsafe archive path"),
    ([
        ("{top}/bin/node", b"node", "file"),
        ("{top}/bin/npm", b"../../outside", "symlink"),
    ], "link member"),
])
def test_traversal_and_link_members_are_rejected(tmp_path, entries, phrase):
    with pytest.raises(RuntimeError, match=phrase):
        _install(tmp_path, _archive(entries))
    assert not (tmp_path / "node").exists()


def test_internal_symlink_members_are_rejected(tmp_path):
    payload = _archive([
        ("{top}", b"", "dir"),
        ("{top}/bin", b"", "dir"),
        ("{top}/bin/node", b"node", "file"),
        ("{top}/lib", b"", "dir"),
        ("{top}/lib/npm-cli.js", b"npm", "file"),
        ("{top}/bin/npm", b"../lib/npm-cli.js", "symlink"),
    ])
    with pytest.raises(RuntimeError, match="link member"):
        _install(tmp_path, payload)


def test_unexpected_top_level_directory_is_rejected(tmp_path):
    payload = _archive([
        ("other/bin/node", b"node", "file"),
    ])
    with pytest.raises(RuntimeError, match="top-level directory"):
        _install(tmp_path, payload)


def test_redirect_outside_nodejs_is_rejected(tmp_path):
    payload = _valid_archive()
    with pytest.raises(RuntimeError, match="redirect"):
        _install(tmp_path, payload, response_url="https://example.invalid/node.tar.gz")


def test_mismatched_node_version_is_rejected(tmp_path):
    payload = _valid_archive()
    lock_path, url = _write_lock(tmp_path, payload)
    with pytest.raises(RuntimeError, match="expected v22.14.0"):
        fetch_demo_node.install_node(
            tmp_path / "node",
            lock_path=lock_path,
            system="Linux",
            machine="x86_64",
            opener=lambda request, timeout: _Response(payload, url),
            version_probe=lambda node: "v22.13.1",
        )
    assert not (tmp_path / "node").exists()


def test_synthetic_archive_installs_without_downloading_node(tmp_path):
    payload = _valid_archive()
    installed = _install(tmp_path, payload)
    assert installed == tmp_path / "node"
    assert (installed / "bin" / "node").read_bytes() == b"synthetic node"
    assert os.access(installed / "bin" / "node", os.X_OK)


def test_npm_never_falls_back_to_a_global_binary(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / ".demo-tools/node/bin").mkdir(parents=True)
    (root / ".demo-tools/node/bin/node").write_bytes(b"node")
    global_bin = tmp_path / "global-bin"
    global_bin.mkdir()
    (global_bin / "npm").write_bytes(b"npm")
    monkeypatch.setenv("PATH", str(global_bin))

    with pytest.raises(FileNotFoundError, match="bundled npm CLI"):
        demo_npm.run_npm(["--version"], root=root)


def test_npm_cli_runs_through_the_bundled_node(tmp_path):
    root = tmp_path / "project"
    node = root / ".demo-tools/node/bin/node"
    npm_cli = root / ".demo-tools/node/lib/node_modules/npm/bin/npm-cli.js"
    node.parent.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    npm_cli.write_bytes(b"npm")
    seen = []

    def runner(command, cwd):
        seen.append((command, cwd))
        return 7

    assert demo_npm.run_npm(["ci", "--ignore-scripts"], root=root,
                            runner=runner) == 7
    assert seen == [([str(node), str(npm_cli), "ci", "--ignore-scripts"], root)]


def test_npm_default_runner_uses_a_project_local_cache(tmp_path, monkeypatch):
    root = tmp_path / "project"
    node = root / ".demo-tools/node/bin/node"
    npm_cli = root / ".demo-tools/node/lib/node_modules/npm/bin/npm-cli.js"
    node.parent.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    npm_cli.write_bytes(b"npm")
    seen = []

    def call(command, *, cwd, env):
        seen.append((command, cwd, env))
        return 0

    monkeypatch.setattr(demo_npm.subprocess, "call", call)
    assert demo_npm.run_npm(["--version"], root=root) == 0
    assert seen[0][2]["npm_config_cache"] == str(root / ".demo-tools/npm-cache")
    assert seen[0][2]["PLAYWRIGHT_BROWSERS_PATH"] == str(
        root / ".demo-tools/playwright-browsers")
