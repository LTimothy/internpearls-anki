# internpearls/ai_cli.py
"""Agent-CLI backends: detection, probes, and streaming subprocess runs.

No aqt imports: callers hand in paths and callbacks, so this stays testable
with a fake CLI script. The security posture lives in build_argv: no backend
ever gets write, shell, or edit tools; thorough mode allowlists web tools
only; claude additionally gets Read scoped to the scratch dir when images
are attached (that is how it views them).
"""
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import warnings

from .ai_logic import parse_stream_event

BACKENDS = {
    "claude": {"label": "Claude Code", "exe": "claude",
               "subscription": "Claude Pro or Max",
               "safety": "Tools fully restricted (strongest)",
               # Truthful per-backend: only claude's build_argv actually caps turns
               # and gates web tools by mode (--max-turns, --tools). See build_argv.
               "modes": {
                   "thorough": "Thorough: drafts, may search the web to verify "
                               "facts, then self-reviews (up to 15 turns, 1 to 3 min)",
                   "quick": "Quick draft: exactly one turn, still no web access -- "
                           "but if you attach files, it can read the scratch copy "
                           "of exactly those files, to view them (15 to 30 s)"}},
    "codex": {"label": "Codex CLI", "exe": "codex",
              "subscription": "ChatGPT Plus or Pro",
              "safety": "Sandboxed read-only; no writes or network",
              # Codex's build_argv always sets --sandbox read-only regardless of
              # mode, so neither mode can verify anything online, and neither sets
              # a turn cap; only the prompt's stated workflow differs by mode.
              "modes": {
                  "thorough": "Thorough: asked to draft, verify, then self-review; "
                              "always sandboxed read-only with no network, so it "
                              "cannot actually verify anything online (1 to 3 min)",
                  "quick": "Quick draft: asked for a single pass with no "
                          "verification; always sandboxed read-only with no "
                          "network either way (15 to 30 s)"}},
    "agy": {"label": "Antigravity CLI", "exe": "agy",
            "subscription": "Google account (free tier, throttled)",
            "safety": "Relies on the assistant's own approval defaults",
            # agy's build_argv never restricts tools or turns by mode, so nothing
            # here enforces "no web access" even in Quick; only the prompt's
            # stated workflow differs by mode.
            "modes": {
                "thorough": "Thorough: asked to draft, verify, then self-review, "
                            "but nothing here restricts its tools or turns -- it "
                            "runs under Antigravity's own approval defaults, which "
                            "may include web access (1 to 3 min)",
                "quick": "Quick draft: asked for a single pass with no "
                        "verification, but nothing here restricts its tools or "
                        "turns either, so it may still use the web and may still "
                        "take a while (15 to 30 s)"}},
}
_COMMON_DIRS = ("/opt/homebrew/bin", "/usr/local/bin",
                os.path.expanduser("~/.local/bin"),
                os.path.expanduser("~/.npm-global/bin"))
_TIMEOUTS = {"quick": 120, "thorough": 360}
_MAX_TURNS = {"quick": 1, "thorough": 15}
# Image-input support per backend. agy's headless mode accepts text blocks
# only (documented); flips to a live probe if Google adds media there.
_IMAGE_CAPABLE = {"claude": True, "codex": True, "agy": False}


class GenerationError(RuntimeError):
    pass


class GenerationCancelled(RuntimeError):
    pass


def image_capable(kind):
    return _IMAGE_CAPABLE.get(kind, False)


def find_cli(kind, override=""):
    if override:
        return override if (os.path.isfile(override)
                            and os.access(override, os.X_OK)) else None
    exe = BACKENDS[kind]["exe"]
    found = shutil.which(exe)
    if found:
        return found
    for d in _COMMON_DIRS:
        p = os.path.join(d, exe)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_flag_support_cache = {}


def supports_flag(path, flag):
    """True iff `path --help` mentions flag. agy has no documented tool-
    restriction switch (an upstream read-only request is open), so we probe
    rather than assume: passing an unsupported flag would hard-fail every
    run, which is worse than the safety gap it would close. Never raise --
    a missing/broken binary just means "flag not detected", the safe
    default. Do not replace this with an unconditional --sandbox append."""
    key = (path, flag)
    if key in _flag_support_cache:
        return _flag_support_cache[key]
    try:
        r = subprocess.run([path, "--help"], capture_output=True,
                           text=True, timeout=10)
        result = flag in ((r.stdout or "") + (r.stderr or ""))
    except Exception:
        result = False
    _flag_support_cache[key] = result
    return result


def probe(kind, path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return {"ok": r.returncode == 0,
            "detail": out[0] if out else f"exit {r.returncode}"}


def build_argv(kind, path, mode, scratch, image_paths):
    """Returns (argv, prompt_via_stdin). The prompt always goes via stdin:
    codex exec freezes reading stdin when the prompt is an argument, and one
    consistent channel keeps the runner simple."""
    if kind == "claude":
        argv = [path, "-p", "--output-format", "stream-json", "--verbose",
                "--max-turns", str(_MAX_TURNS[mode]),
                "--setting-sources", ""]
        tools = []
        if mode == "thorough":
            tools += ["WebSearch", "WebFetch"]
        if image_paths:
            tools.append("Read")
            argv += ["--add-dir", scratch]
        # --tools is an allowlist of what's even available to the model, not just
        # what's auto-approved: naming this small a set here (never Bash/Edit/
        # Write/NotebookEdit/Task) is what makes "worst case: bad card text"
        # true regardless of anything the model tries to do.
        tool_list = ",".join(tools)
        argv += ["--tools", tool_list, "--allowedTools", tool_list]
        return argv, True
    if kind == "codex":
        argv = [path, "exec", "--json", "--sandbox", "read-only",
                "--skip-git-repo-check", "-C", scratch]
        for p in image_paths:
            argv += ["--image", p]
        return argv, True
    if kind == "agy":
        argv = [path, "-p", "--output-format", "stream-json"]
        if supports_flag(path, "--sandbox"):
            argv += ["--sandbox"]
        return argv, True
    raise ValueError(kind)


def _run_argv(argv, kind, prompt, on_event=None, cancel=None, timeout=120,
              cwd=None):
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=cwd,
                                start_new_session=True)
    except OSError as e:
        raise GenerationError(f"could not start the assistant: {e}") from e
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass
    result, tokens, rate_limits, error_msg = None, 0, None, None

    lines = []
    done = threading.Event()

    def _reader():
        for line in proc.stdout:
            lines.append(line)
        done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    seen = 0
    err_text = ""
    try:
        while True:
            while seen < len(lines):
                evt = parse_stream_event(kind, lines[seen])
                seen += 1
                if not evt:
                    continue
                if evt["type"] == "result":
                    result = evt["text"]
                    tokens = max(tokens, evt.get("tokens", 0))
                elif evt["type"] == "error":
                    # An is_error result -- never assigned to `result`, so it
                    # can't flow onward as if it were the model's reply.
                    error_msg = evt["text"] or error_msg
                elif evt["type"] == "usage":
                    tokens = max(tokens, evt["tokens"])
                elif evt["type"] == "rate_limits":
                    rate_limits = evt
                if on_event:
                    on_event(evt)
            if done.is_set() and proc.poll() is not None:
                break
            if cancel and cancel():
                raise GenerationCancelled("cancelled")
            if time.monotonic() - start > timeout:
                raise GenerationError(
                    "the assistant timed out; try Quick draft or a shorter source")
            time.sleep(0.05)
        # stderr is read here, before the finally block below closes it, so
        # a failure message survives the cleanup.
        err_text = (proc.stderr.read() or "").strip()
    except BaseException:
        # Cancel, timeout, a bad event shape, or a broken on_event callback
        # can all land here with the child still running. Kill it before the
        # finally block's proc.wait() below, or that wait blocks for up to 5s
        # on a live process and can raise TimeoutExpired in its place, which
        # would both mask the real exception and skip the rest of cleanup.
        if proc.poll() is None:
            _kill(proc)
        raise
    finally:
        # A killed process still has to be waited on, or it leaks as a zombie;
        # the reader thread and the pipes need to be cleaned up too, or a
        # generation loop leaks file descriptors one run at a time. None of
        # this may raise, or it replaces whatever exception is propagating.
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        t.join(timeout=2)
        if t.is_alive():
            warnings.warn("ai_cli: reader thread outlived cleanup; a pipe "
                          "may be leaking", ResourceWarning)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
    if error_msg:
        # The CLI's own explanation beats stderr and beats the bare exit code,
        # regardless of returncode -- an is_error result is always fatal.
        raise GenerationError(error_msg)
    if proc.returncode != 0:
        raise GenerationError(err_text or f"assistant exited {proc.returncode}")
    if not result:
        raise GenerationError("the assistant produced no usable reply")
    return {"text": result, "tokens": tokens, "rate_limits": rate_limits,
            "duration_s": round(time.monotonic() - start, 1)}


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_generation(kind, path, prompt, mode, scratch, image_paths=(),
                   on_event=None, cancel=None, timeout=None):
    argv, _ = build_argv(kind, path, mode, scratch, list(image_paths))
    return _run_argv(argv, kind, prompt, on_event=on_event, cancel=cancel,
                     timeout=timeout or _TIMEOUTS[mode], cwd=scratch)


_TEST_PROMPT = "Reply with exactly one word: ok"
_AUTH_HINTS = ("not logged in", "not authenticated", "unauthoriz", "auth error",
              "please sign in", "please log in", "log in with", "no credentials",
              "authentication required", "run `claude login`", "run `codex login`")


def _readable_cli_error(raw):
    """Turn a CLI's raw stderr into one short, human sentence. Never shown
    verbatim: a not-signed-in CLI's stderr is often a multi-line stack of its
    own auth-library noise, which is exactly what a user waiting on "Test
    connection" should not have to parse to learn "go sign in"."""
    text = (raw or "").strip()
    low = text.lower()
    if any(h in low for h in _AUTH_HINTS):
        return "not signed in -- run it once in a terminal and sign in there"
    first_line = text.splitlines()[0] if text else "no output from the assistant"
    return first_line[:200]


def test_connection(kind, path, timeout=45):
    """Run a trivial prompt through the real backend and report whether it
    actually works, not just whether the binary executes (that's probe()'s
    job, and --version succeeding is not proof of a working, signed-in
    backend -- a CLI that has never been signed into still answers
    --version). Costs one real, billed model turn, so callers must only run
    this on demand, never automatically.

    Returns {"state": "working"|"not_working", "detail": <short message>}.
    Never raises: a backend that can't even start comes back as
    "not_working" with its own readable detail, same as an auth failure.
    """
    scratch = tempfile.mkdtemp(prefix="ip-aigen-test-")
    try:
        run_generation(kind, path, _TEST_PROMPT, "quick", scratch, timeout=timeout)
        return {"state": "working", "detail": "connected and responding"}
    except GenerationError as e:
        return {"state": "not_working", "detail": _readable_cli_error(str(e))}
    except Exception as e:
        return {"state": "not_working", "detail": _readable_cli_error(str(e))}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
