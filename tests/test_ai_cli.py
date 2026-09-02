# tests/test_ai_cli.py
import os
import sys
import threading
import time
import pytest

from internpearls import ai_cli

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_cli.py")]


def _run(mode="ok", **kw):
    return ai_cli._run_argv(FAKE + [mode], "claude", "PROMPT", **kw)


class _CallbackBoom(Exception):
    pass


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


def test_run_other_exception_kills_process_and_is_not_masked(monkeypatch):
    """A callback exception (or any non-cancel/timeout failure) mid-run must
    still kill the child and propagate the real exception, not a
    subprocess.TimeoutExpired from an un-killed process.wait()."""
    procs = []
    real_popen = ai_cli.subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(ai_cli.subprocess, "Popen", spying_popen)

    def bad_event(evt):
        raise _CallbackBoom("callback exploded")

    start = time.monotonic()
    with pytest.raises(_CallbackBoom):
        _run("event_then_slow", on_event=bad_event, timeout=30)
    elapsed = time.monotonic() - start

    # The fake CLI sleeps 30s after its one event; a correct kill path exits
    # in well under a second. Bounding this proves proc.wait() didn't block
    # for its full 5s timeout (which is what masks the exception).
    assert elapsed < 5

    assert len(procs) == 1
    proc = procs[0]
    for _ in range(30):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None, "child process is still running"
    # poll() only reflects our own wait(); confirm the pid itself is gone.
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


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


def test_build_argv_agy_adds_sandbox_when_supported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--sandbox" in argv


def test_build_argv_agy_omits_sandbox_when_unsupported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag: False)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--sandbox" not in argv


def test_build_argv_agy_never_skips_permissions(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--dangerously-skip-permissions" not in argv


def test_supports_flag_missing_binary_returns_false():
    assert ai_cli.supports_flag("/no/such/binary-xyz", "--sandbox") is False


def test_supports_flag_caches_per_path_and_flag(monkeypatch):
    ai_cli._flag_support_cache.clear()
    calls = []
    real_run = ai_cli.subprocess.run

    def spying_run(argv, *a, **kw):
        calls.append(tuple(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(ai_cli.subprocess, "run", spying_run)
    ai_cli.supports_flag("/no/such/binary-xyz", "--sandbox")
    ai_cli.supports_flag("/no/such/binary-xyz", "--sandbox")
    assert len(calls) == 1


def test_backends_all_have_safety_posture():
    for kind, info in ai_cli.BACKENDS.items():
        assert info.get("safety"), f"{kind} is missing a safety posture string"


# -- C1: per-backend mode text must be truthful, not one claim copy-pasted for
# all three backends regardless of what build_argv actually enforces for them.
def test_backends_all_have_mode_text_for_both_modes():
    for kind, info in ai_cli.BACKENDS.items():
        modes = info.get("modes")
        assert modes and modes.get("thorough") and modes.get("quick"), (
            f"{kind} is missing per-mode text")


def test_claude_mode_text_matches_what_build_argv_actually_restricts():
    thorough_argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                      "/tmp/s", [])
    modes = ai_cli.BACKENDS["claude"]["modes"]
    # Thorough really does get web tools here, so claiming it may verify online
    # is true; quick really does get none, so claiming no web access is true.
    assert "WebSearch" in " ".join(thorough_argv)
    assert "web" in modes["thorough"].lower()
    assert "WebSearch" not in " ".join(quick_argv)
    assert "no web access" in modes["quick"].lower()


def test_codex_argv_is_mode_invariant_so_its_text_must_not_claim_mode_enforcement():
    # codex's build_argv never branches on mode (always --sandbox read-only, no
    # turn cap set), so neither mode's text may claim online verification
    # actually happens, or that a turn limit is enforced.
    thorough_argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick",
                                      "/tmp/s", [])
    assert thorough_argv == quick_argv
    assert "read-only" in thorough_argv
    modes = ai_cli.BACKENDS["codex"]["modes"]
    for text in modes.values():
        assert "verifies facts online" not in text.lower()
    # Thorough is the only one that even mentions verification, so it's the
    # one that must disclaim actually doing it online.
    assert "cannot" in modes["thorough"].lower() and "online" in modes["thorough"].lower()


def test_agy_argv_is_mode_invariant_so_quick_must_not_claim_no_web_access():
    # agy's build_argv never branches on mode either (no --tools/--max-turns
    # equivalent at all), so "Quick draft: ... no web access" -- the label
    # every mode used to share -- would be false here.
    thorough_argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert thorough_argv == quick_argv
    quick_text = ai_cli.BACKENDS["agy"]["modes"]["quick"].lower()
    assert "no web access" not in quick_text
    assert "no tool" not in quick_text
