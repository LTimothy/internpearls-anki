# tests/test_ai_cli.py
import os
import sys
import threading
import pytest

from internpearls import ai_cli

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_cli.py")]


def _run(mode="ok", **kw):
    return ai_cli._run_argv(FAKE + [mode], "claude", "PROMPT", **kw)


def test_run_happy_path_parses_result_and_phase():
    events = []
    res = _run(on_event=events.append)
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15
    assert any(e.get("phase") == "Verify online" for e in events)


def test_run_failure_raises_generation_error_with_stderr():
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("fail")
    assert "boom" in str(e.value)


def test_run_garbage_output_raises():
    with pytest.raises(ai_cli.GenerationError):
        _run("garbage")


def test_run_timeout_kills_process():
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("slow", timeout=2)
    assert "time" in str(e.value).lower()


def test_run_cancel_kills_process():
    flag = threading.Event()
    flag.set()
    with pytest.raises(ai_cli.GenerationCancelled):
        _run("slow", cancel=flag.is_set, timeout=30)


def test_find_cli_prefers_override(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert ai_cli.find_cli("claude", override=str(exe)) == str(exe)
    assert ai_cli.find_cli("claude", override=str(tmp_path / "nope")) is None


def test_build_argv_claude_thorough_allows_web_only():
    argv, stdin = ai_cli.build_argv("claude", "/usr/bin/claude", "thorough",
                                    "/tmp/scratch", [])
    joined = " ".join(argv)
    assert stdin is True
    assert "--output-format" in joined and "stream-json" in joined
    assert "WebSearch" in joined and "Bash" not in joined
    assert "--max-turns" in joined


def test_build_argv_claude_quick_denies_all_tools():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                "/tmp/scratch", [])
    assert "WebSearch" not in " ".join(argv)


def test_build_argv_claude_images_adds_read_and_dir():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                "/tmp/s", ["/tmp/s/a.png"])
    joined = " ".join(argv)
    assert "Read" in joined and "--add-dir" in joined


def test_build_argv_codex_images_flag():
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick",
                                "/tmp/s", ["/tmp/s/a.png"])
    joined = " ".join(argv)
    assert "exec" in argv and "--image" in joined and "a.png" in joined


def test_image_capable_table():
    assert ai_cli.image_capable("claude") and ai_cli.image_capable("codex")
    assert not ai_cli.image_capable("agy")
