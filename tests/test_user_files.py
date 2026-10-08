"""Persistent state stays in temporary folders throughout the plain suite."""
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

for module in ("ai_dialog", "background", "collection", "config", "dialogs",
               "review", "sync", "updates"):
    importlib.import_module("internpearls." + module)

_USER_FILES = Path(__file__).resolve().parents[1] / "internpearls" / "user_files"
_PATH_BINDINGS = [
    (module, name)
    for module, mod in sorted(sys.modules.copy().items())
    if module.startswith("internpearls.") and mod is not None
    for name, value in sorted(vars(mod).items())
    if isinstance(value, (str, os.PathLike))
    and (Path(value) == _USER_FILES or _USER_FILES in Path(value).parents)
]

@pytest.mark.parametrize("module, name", _PATH_BINDINGS)
def test_persistent_paths_write_only_under_tmp_path(anki, tmp_path, module, name):
    mod = importlib.import_module(module)
    from internpearls import config

    path = Path(getattr(mod, name))
    assert tmp_path in path.parents, (module, name, path)
    if name == "_USER_FILES":
        assert path.is_dir()
        return
    if name == "AI_LAST_RUN_LOG":
        from internpearls import ai_cli
        ai_cli._write_run_log(str(path), ["assistant"], "", ["done"], "", 0, 1.0)
        assert "exit code: 0" in path.read_text(encoding="utf8")
        return
    config._save_json(str(path), {"isolated": True})
    written = Path(config._collection_state_path(str(path)))
    assert tmp_path in written.parents
    assert config._load_json(str(path), {}) == {"isolated": True}


@pytest.mark.parametrize("folder", ["deck_backups", "feedback_digests", "ai_draft_images"])
def test_derived_state_folders_stay_under_tmp_path(anki, tmp_path, folder):
    from internpearls import ai_draft, collection, config, review

    paths = {
        "deck_backups": collection._deck_backup_folder,
        "feedback_digests": review._feedback_digest_folder,
        "ai_draft_images": lambda: ai_draft._image_folder(ai_draft.draft_path()),
    }
    assert tmp_path in Path(config._USER_FILES).parents
    path = Path(paths[folder]())
    assert tmp_path in path.parents
    assert path.name == folder


@pytest.mark.parametrize("change", ["none", "added", "modified", "removed", "nested"])
def test_session_guard_detects_user_files_changes(tmp_path, monkeypatch, change):
    import conftest

    start = getattr(conftest, "pytest_sessionstart", None)
    finish = getattr(conftest, "pytest_sessionfinish", None)
    assert callable(start) and callable(finish), "user_files needs a session guard"
    folder = tmp_path / "guard"
    folder.mkdir()
    existing = folder / "existing.json"
    existing.write_text("{}", encoding="utf8")
    messages = []
    reporter = SimpleNamespace(write_sep=lambda *args: messages.append(args))
    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: reporter)),
        exitstatus=pytest.ExitCode.OK)
    monkeypatch.setattr(conftest, "_USER_FILES_DIR", folder)
    start(session)
    if change == "added":
        (folder / "added.log").write_text("log", encoding="utf8")
    elif change == "modified":
        stamp = existing.stat().st_mtime_ns
        os.utime(existing, ns=(stamp, stamp + 1_000_000))
    elif change == "removed":
        existing.unlink()
    elif change == "nested":
        nested = folder / "collections" / "source" / "deck_backups"
        nested.mkdir(parents=True)
        (nested / "backup.apkg").write_bytes(b"backup")
    finish(session, session.exitstatus)
    assert session.exitstatus == (pytest.ExitCode.OK if change == "none"
                                  else pytest.ExitCode.TESTS_FAILED)
    assert bool(messages) == (change != "none")


def test_persistent_state_is_isolated_without_requesting_anki(tmp_path):
    from internpearls import config

    path = Path(config.INSTALLED)
    assert tmp_path in path.parents
    config._save_json(str(path), {"isolated": True})
    assert config._load_json(str(path), {}) == {"isolated": True}
