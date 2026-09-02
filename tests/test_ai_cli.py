# tests/test_ai_cli.py
import os
import sys
import threading
import time
import pytest

from internpearls import ai_cli, ai_logic

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


def test_run_error_result_raises_with_the_clis_own_message_not_exit_code():
    # Real shape from a v2.1.251 claude with an expired login: subtype
    # "success", is_error true, empty stderr, exit 1. The raised message must
    # be the CLI's own explanation, not the generic "assistant exited 1".
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("error_result")
    msg = str(e.value)
    assert "OAuth session expired" in msg
    assert "exited" not in msg


def test_run_error_result_text_never_reaches_parse_cards_json():
    # The is_error message must never be treated as the model's reply: it
    # has to raise, not come back as a successful result carrying that text.
    events = []
    try:
        _run("error_result", on_event=events.append)
        pytest.fail("expected GenerationError")
    except ai_cli.GenerationError:
        pass
    assert not any(e["type"] == "result" for e in events)
    cards, errors = ai_logic.parse_cards_json(
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        {"Study Deck - Basic"}, {"Study Deck - Basic": ["Front", "Back"]})
    assert cards == [] and errors   # confirms this text was never valid card JSON


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


# === I9: end-to-end evidence for all three backends' result parsing, not just
# claude. Neither codex nor agy is installed on this machine, so these drive
# the fake CLI through run_generation()'s real path (build_argv included):
# they prove the parsing/plumbing works for a given event shape, not that the
# shape matches a real vendor binary, which remains unverified.
def test_run_generation_parses_codex_top_level_text_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["codex_top"], True))
    res = ai_cli.run_generation("codex", "/usr/bin/codex", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


def test_run_generation_parses_codex_nested_item_text_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["codex_nested"], True))
    res = ai_cli.run_generation("codex", "/usr/bin/codex", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


def test_run_generation_parses_antigravity_result_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["agy_ok"], True))
    res = ai_cli.run_generation("agy", "/usr/bin/agy", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


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


def test_build_argv_claude_defaults_to_sonnet_medium():
    # The owner-chosen default: without any config override, claude must never
    # fall through to the account's own default model (the top model for a Max
    # subscriber), which burns credits fast across Thorough's up-to-15-turn loop.
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick", "/tmp/s", [])
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "medium"


def test_build_argv_claude_thorough_also_defaults_to_sonnet_medium():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "thorough", "/tmp/s", [])
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "medium"


def test_build_argv_claude_honours_config_overrides():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick", "/tmp/s", [],
                                model="opus", effort="high")
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"


def test_build_argv_codex_omits_model_flag_when_unset(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [])
    assert "--model" not in argv


def test_build_argv_codex_emits_model_flag_when_set_and_supported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                                model="o3")
    assert "--model" in argv and argv[argv.index("--model") + 1] == "o3"


def test_build_argv_codex_omits_model_flag_when_unsupported(monkeypatch):
    # An older codex without a documented --model flag must not be hard-broken
    # by passing it anyway: same guard mechanism as agy's --sandbox.
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: False)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                                model="o3")
    assert "--model" not in argv


def test_build_argv_codex_probes_the_exec_subcommand_for_model_support(monkeypatch):
    # The original probe never named a subcommand, so it only ever read the
    # top-level `codex --help`, not `codex exec --help` where exec's own flags
    # (including -m/--model) are actually documented.
    seen = []

    def fake_supports_flag(path, flag, subcommand=None):
        seen.append((flag, subcommand))
        return True
    monkeypatch.setattr(ai_cli, "supports_flag", fake_supports_flag)
    ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                      model="o3")
    assert ("--model", "exec") in seen


def test_build_argv_codex_never_gets_an_effort_flag(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                                model="o3", effort="high")
    assert "--effort" not in argv


def test_build_argv_agy_sends_model_and_effort_only_when_set_and_supported(
        monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [],
                                model="gemini-3.8-flash-medium", effort="high")
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert argv[argv.index("--effort") + 1] == "high"
    # Nothing set: agy's own default, no flag at all.
    bare, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--model" not in bare and "--effort" not in bare
    # Set, but this binary does not document them: passing either would hard
    # fail every run, so neither is sent.
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: False)
    unsupported, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s",
                                       [], model="gemini-3.8-flash-medium",
                                       effort="high")
    assert "--model" not in unsupported and "--effort" not in unsupported


def test_build_argv_agy_puts_the_prompt_in_argv_after_dash_p_and_not_on_stdin(
        monkeypatch):
    # agy never reads stdin: --print takes the prompt as its VALUE. Anything
    # after it would be read as the prompt instead, so -p goes last.
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, via_stdin = ai_cli.build_argv("agy", "/usr/bin/agy", "quick",
                                        "/tmp/s", [], prompt="DRAFT CARDS")
    assert via_stdin is False
    assert argv[-2:] == ["-p", "DRAFT CARDS"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--add-dir") + 1] == "/tmp/s"
    assert "--disable-slash-commands" in argv


def test_build_argv_agy_omits_disable_slash_commands_when_unsupported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag",
                        lambda path, flag, **kw: flag != "--disable-slash-commands")
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--disable-slash-commands" not in argv


def test_build_argv_agy_refuses_an_oversized_prompt(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    with pytest.raises(ai_cli.GenerationError) as e:
        ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [],
                          prompt="x" * (ai_cli._MAX_ARG_PROMPT + 1))
    assert "too long" in str(e.value)


def test_backends_carry_model_hint_for_the_ui():
    for kind, info in ai_cli.BACKENDS.items():
        assert "model_hint" in info and info["model_hint"]


def test_image_capable_table():
    # All three read an attached image: agy does it with view_file against the
    # scratch dir build_argv hands it as --add-dir.
    for kind in ("claude", "codex", "agy"):
        assert ai_cli.image_capable(kind)


def test_build_argv_agy_adds_sandbox_when_supported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--sandbox" in argv


def test_build_argv_agy_omits_sandbox_when_unsupported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: False)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--sandbox" not in argv


def test_build_argv_agy_never_skips_permissions(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
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


FAKE_HELP = os.path.join(os.path.dirname(__file__), "fake_help_cli.py")


def test_supports_flag_rejects_substring_match(monkeypatch):
    # The original bug: a literal "-m" substring check is satisfied by
    # "--max-turns", so it was effectively always True and never protected
    # anything. Help text that documents an unrelated flag containing this
    # one as a substring must not count as support.
    monkeypatch.setenv("FAKE_HELP_TOP", "--max-turns <N>  cap the turn count")
    ai_cli._flag_support_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "-m") is False


def test_supports_flag_rejects_longer_flag_that_contains_this_one(monkeypatch):
    # "--model-provider" documented, but not "--model" itself.
    monkeypatch.setenv("FAKE_HELP_TOP", "--model-provider <name>  set the provider")
    ai_cli._flag_support_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is False


def test_supports_flag_accepts_word_boundary_match(monkeypatch):
    monkeypatch.setenv("FAKE_HELP_TOP", "-m, --model <MODEL>  the model to use")
    ai_cli._flag_support_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is True


def test_supports_flag_probes_named_subcommands_help(monkeypatch):
    # The top-level help documents nothing; only `exec --help` does, which is
    # where a subcommand's own options actually live for a real codex-style CLI.
    monkeypatch.setenv("FAKE_HELP_TOP", "a generic top-level help screen")
    monkeypatch.setenv("FAKE_HELP_SUBCOMMAND", "exec")
    monkeypatch.setenv("FAKE_HELP_SUB", "-m, --model <MODEL>  the model to use")
    ai_cli._flag_support_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is False
    assert ai_cli.supports_flag(FAKE_HELP, "--model", subcommand="exec") is True


def test_supports_flag_still_detects_agy_sandbox_with_tightened_matching(monkeypatch):
    monkeypatch.setenv("FAKE_HELP_TOP", "--sandbox  run in a restricted sandbox")
    ai_cli._flag_support_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--sandbox") is True


def test_resolve_claude_effort_passes_through_known_level():
    assert ai_cli.resolve_claude_effort("high") == "high"


def test_resolve_claude_effort_falls_back_on_typo():
    # A hand-edited config typo must resolve to something valid, not reach
    # `claude --effort <typo>` and die with an opaque CLI error.
    assert (ai_cli.resolve_claude_effort("hihg")
            == ai_cli.BACKENDS["claude"]["default_effort"])


def test_resolve_claude_effort_falls_back_on_empty():
    assert (ai_cli.resolve_claude_effort("")
            == ai_cli.BACKENDS["claude"]["default_effort"])


def test_build_argv_claude_falls_back_to_default_effort_on_typo():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick", "/tmp/s", [],
                                effort="hihg")
    assert (argv[argv.index("--effort") + 1]
            == ai_cli.BACKENDS["claude"]["default_effort"])


def test_backends_all_have_safety_posture():
    for kind, info in ai_cli.BACKENDS.items():
        assert info.get("safety"), f"{kind} is missing a safety posture string"


# === C1: per-backend mode text must be truthful, not one claim copy-pasted for
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
    # equivalent at all), so "Quick draft: ... no web access" (the label
    # every mode used to share) would be false here.
    thorough_argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert thorough_argv == quick_argv
    quick_text = ai_cli.BACKENDS["agy"]["modes"]["quick"].lower()
    assert "no web access" not in quick_text
    assert "no tool" not in quick_text


def test_claude_quick_mode_text_matches_build_argv_with_attachments():
    # Finding 2: build_argv grants Read (scoped to the scratch dir) whenever
    # images are attached, in EVERY mode: so quick's text must not claim
    # "no tools at all" once an image is in play, only that it still has no
    # web access. Pinned against the WITH-attachments call specifically,
    # since the no-attachment case above can't catch this drift.
    quick_argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                      "/tmp/s", ["/tmp/s/photo.png"])
    tools = quick_argv[quick_argv.index("--tools") + 1]
    assert "Read" in tools
    assert "WebFetch" not in tools and "WebSearch" not in tools
    quick_text = ai_cli.BACKENDS["claude"]["modes"]["quick"].lower()
    assert "no tools at all" not in quick_text
    assert "no web access" in quick_text


# === I7: Test connection ===================================================

def _stub_build_argv(monkeypatch, mode_arg):
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs, **kw: (FAKE + [mode_arg], True))


def test_connection_working_for_a_real_reply(monkeypatch):
    # "badjson" is just a fake CLI that exits 0 with a non-empty result text:
    # test_connection only needs proof the backend answered, not that the reply
    # happens to be card JSON (the trivial test prompt never asks for that).
    _stub_build_argv(monkeypatch, "badjson")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "working"


def test_connection_not_signed_in_gives_a_readable_message_not_raw_stderr(monkeypatch):
    _stub_build_argv(monkeypatch, "not_signed_in")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "not_working"
    assert "sign in" in res["detail"].lower()
    assert "Run `claude login`" not in res["detail"]   # not the raw stderr


def test_connection_expired_login_reports_the_clis_own_message(monkeypatch):
    # The real defect this guards: an expired login used to come back as
    # "assistant exited 1", defeating the whole point of Test connection.
    _stub_build_argv(monkeypatch, "error_result")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "not_working"
    assert "OAuth session expired" in res["detail"]
    assert "exited" not in res["detail"]


def test_connection_other_failure_falls_back_to_first_stderr_line(monkeypatch):
    _stub_build_argv(monkeypatch, "fail")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "not_working"
    assert res["detail"] == "boom"


def test_connection_cleans_up_its_scratch_dir(monkeypatch, tmp_path):
    seen = {}
    real_mkdtemp = ai_cli.tempfile.mkdtemp

    def spy_mkdtemp(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        seen["path"] = p
        return p

    monkeypatch.setattr(ai_cli.tempfile, "mkdtemp", spy_mkdtemp)
    _stub_build_argv(monkeypatch, "badjson")
    ai_cli.test_connection("claude", "/usr/bin/claude")
    assert seen["path"] and not os.path.exists(seen["path"])


def test_readable_cli_error_recognizes_common_auth_phrasing():
    assert "sign in" in ai_cli._readable_cli_error(
        "Error: not authenticated, please log in").lower()


def test_readable_cli_error_falls_back_to_trimmed_first_line():
    assert ai_cli._readable_cli_error("boom\nsome traceback\nmore junk") == "boom"


def test_readable_cli_error_explains_an_antigravity_auto_denied_tool():
    raw = ('jetski: no output produced ... a tool required the "write_file" '
           "permission that headless mode cannot prompt for, so it was "
           "auto-denied ...")
    assert ai_cli._readable_cli_error(raw) == (
        "Antigravity refused a file write; the add-on never enables writes, "
        "so this run used a tool it should not have.")


def test_readable_cli_error_explains_an_antigravity_empty_prompt():
    raw = 'Error: empty prompt. Usage: agy --print "your prompt here"'
    assert ai_cli._readable_cli_error(raw) == (
        "Antigravity received an empty prompt, so it had nothing to work from.")


def test_readable_cli_error_handles_empty_input():
    assert ai_cli._readable_cli_error("") == "no output from the assistant"


def test_detect_backends_skips_disabled_and_honours_preference(monkeypatch):
    from internpearls import ai_cli
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": f"/bin/{kind}")
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "1.0"})
    cfg = {"ai_backend": "codex",
           "ai_backend_enabled": {"claude": True, "codex": True, "agy": False},
           "ai_cli_path": {"claude": "", "codex": "", "agy": ""}}
    res = ai_cli.detect_backends(cfg)
    assert res["chosen"] == "codex"
    assert res["backends"]["agy"]["path"] is None and res["backends"]["agy"]["enabled"] is False
    cfg["ai_backend_enabled"]["codex"] = False
    assert ai_cli.detect_backends(cfg)["chosen"] == "claude"


def test_detect_backends_uses_per_backend_override(monkeypatch):
    from internpearls import ai_cli
    seen = {}
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": seen.setdefault(kind, override) or None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "x"})
    cfg = {"ai_backend": "", "ai_backend_enabled": {"claude": True, "codex": True, "agy": True},
           "ai_cli_path": {"claude": "/opt/claude", "codex": "", "agy": ""}}
    ai_cli.detect_backends(cfg)
    assert seen["claude"] == "/opt/claude" and seen["codex"] == ""
