#!/usr/bin/env python3
"""Install the exact Node.js runtime used by the browser contract tests."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "demo" / "node-lock.json"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def _platform_key(system, machine):
    normalized_machine = machine.lower()
    if system == "Darwin" and normalized_machine in {"arm64", "aarch64"}:
        return "darwin-arm64"
    if system == "Darwin" and normalized_machine in {"x86_64", "amd64"}:
        return "darwin-x64"
    if system == "Linux" and normalized_machine in {"x86_64", "amd64"}:
        return "linux-x64"
    raise RuntimeError(f"unsupported platform: {system} {machine}")


def _load_lock(lock_path):
    with Path(lock_path).open(encoding="utf-8") as handle:
        lock = json.load(handle)
    if not isinstance(lock.get("version"), str):
        raise RuntimeError("Node lock is missing a version")
    if not isinstance(lock.get("archives"), dict):
        raise RuntimeError("Node lock is missing archives")
    return lock


def select_archive(*, lock_path=DEFAULT_LOCK, system=None, machine=None):
    lock = _load_lock(lock_path)
    key = _platform_key(system or platform.system(), machine or platform.machine())
    try:
        archive = lock["archives"][key]
    except KeyError as exc:
        raise RuntimeError(f"unsupported platform: {key}") from exc

    parsed = urlparse(archive.get("url", ""))
    if parsed.scheme != "https" or parsed.hostname != "nodejs.org":
        raise RuntimeError("Node archive URL must use https://nodejs.org")
    if not isinstance(archive.get("length"), int) or archive["length"] <= 0:
        raise RuntimeError("Node archive length must be a positive integer")
    digest = archive.get("sha256", "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RuntimeError("Node archive SHA-256 must be lowercase hexadecimal")
    return lock["version"], archive


def _download_archive(archive, destination, opener):
    request = Request(archive["url"], headers={"User-Agent": "anki-demo-bootstrap/1"})
    with opener(request, timeout=30) as response:
        final_url = urlparse(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != "nodejs.org":
            raise RuntimeError("Node archive redirect left nodejs.org")

        content_length = response.headers.get("Content-Length")
        if content_length is None or int(content_length) != archive["length"]:
            raise RuntimeError("Node archive length does not match the lock")

        digest = hashlib.sha256()
        received = 0
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if received > archive["length"]:
                    raise RuntimeError("Node archive length exceeds the lock")
                digest.update(chunk)
                handle.write(chunk)

    if received != archive["length"]:
        raise RuntimeError("Node archive length does not match the lock")
    if digest.hexdigest() != archive["sha256"]:
        raise RuntimeError("Node archive SHA-256 does not match the lock")


def _expected_top_level(archive_url):
    filename = PurePosixPath(urlparse(archive_url).path).name
    suffix = ".tar.gz"
    if not filename.endswith(suffix):
        raise RuntimeError("Node archive URL must end in .tar.gz")
    return filename[:-len(suffix)]


def _validate_members(members, expected_top):
    if not members:
        raise RuntimeError("Node archive is empty")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise RuntimeError(f"unsafe archive path: {member.name}")
        if path.parts[0] != expected_top:
            raise RuntimeError(f"unexpected top-level directory: {path.parts[0]}")
        if member.islnk():
            raise RuntimeError(f"link member is not allowed: {member.name}")
        if member.issym():
            link = PurePosixPath(member.linkname)
            target = PurePosixPath(posixpath.normpath(str(path.parent / link)))
            if link.is_absolute() or not target.parts or target.parts[0] != expected_top:
                raise RuntimeError(f"link member escapes archive root: {member.name}")
            continue
        if not member.isfile() and not member.isdir():
            raise RuntimeError(f"unsupported archive member: {member.name}")


def _extract_archive(archive_path, destination, archive_url):
    expected_top = _expected_top_level(archive_url)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        _validate_members(members, expected_top)
        archive.extractall(destination, members=members)
    extracted = destination / expected_top
    if not extracted.is_dir():
        raise RuntimeError(f"unexpected top-level directory: {expected_top}")
    return extracted


def _probe_version(node_path):
    return subprocess.check_output(
        [str(node_path), "--version"], text=True, stderr=subprocess.STDOUT
    ).strip()


def _verify_version(install_path, version, version_probe):
    node_path = install_path / "bin" / "node"
    if not node_path.is_file():
        raise RuntimeError("Node archive is missing bin/node")
    actual = version_probe(node_path).strip()
    expected = f"v{version}"
    if actual != expected:
        raise RuntimeError(f"Node version is {actual!r}, expected {expected}")


def _replace_install(staged, install_path):
    backup = install_path.with_name(f".{install_path.name}.backup-{uuid.uuid4().hex}")
    had_existing = install_path.exists()
    if had_existing:
        os.replace(install_path, backup)
    try:
        os.replace(staged, install_path)
    except BaseException:
        if had_existing:
            os.replace(backup, install_path)
        raise
    if had_existing:
        shutil.rmtree(backup)


def install_node(
        install_path, *, lock_path=DEFAULT_LOCK, system=None, machine=None,
        opener=urlopen, version_probe=_probe_version):
    install_path = Path(install_path).resolve()
    version, archive = select_archive(
        lock_path=lock_path, system=system, machine=machine)
    install_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
            prefix=".node-install-", dir=install_path.parent) as temporary:
        workspace = Path(temporary)
        archive_path = workspace / "node.tar.gz"
        _download_archive(archive, archive_path, opener)
        staged = _extract_archive(archive_path, workspace, archive["url"])
        _verify_version(staged, version, version_probe)
        _replace_install(staged, install_path)
    return install_path


def check_node(install_path, *, lock_path=DEFAULT_LOCK, version_probe=_probe_version):
    version = _load_lock(lock_path)["version"]
    install_path = Path(install_path).resolve()
    _verify_version(install_path, version, version_probe)
    return install_path


def main(argv=None):
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", type=Path)
    action.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    if args.install is not None:
        install_node(args.install)
    else:
        check_node(args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
